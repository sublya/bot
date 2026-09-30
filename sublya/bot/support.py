"""Support chat: a forum group where every user has a topic with their whole dialogue, and
whatever the team writes in that topic goes back to the user from the bot."""

import asyncio
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from html import escape
from typing import Any

from aiogram import Bot, F, Router
from aiogram.client.session.middlewares.base import (
    BaseRequestMiddleware,
    NextRequestMiddlewareType,
)
from aiogram.enums import ContentType
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.methods import SendMessage, SendVideo
from aiogram.methods.base import Response, TelegramMethod, TelegramType
from aiogram.types import CallbackQuery, Message, TelegramObject, User

from . import texts
from .db import Db

log = logging.getLogger(__name__)
router = Router()

TOPIC_NAME_LIMIT = 128
# what the team may send to a user; service messages of the topic itself stay in the group
REPLY_TYPES = {
    ContentType.TEXT, ContentType.PHOTO, ContentType.VIDEO, ContentType.ANIMATION,
    ContentType.DOCUMENT, ContentType.AUDIO, ContentType.VOICE, ContentType.VIDEO_NOTE,
    ContentType.STICKER,
}

_quiet: ContextVar[bool] = ContextVar("support_quiet", default=False)


@contextmanager
def quiet() -> Iterator[None]:
    """Bot messages sent inside stay out of the support chat: the progress message that is
    edited and then deleted, alerts to admins."""
    token = _quiet.set(True)
    try:
        yield
    finally:
        _quiet.reset(token)


def thread_gone(error: TelegramBadRequest) -> bool:
    """The topic was deleted or closed in the group: a reason to open a new one, not a failure."""
    text = error.message.lower()
    return any(s in text for s in ("thread not found", "topic_deleted", "topic deleted", "topic_closed"))


def pressed(query: CallbackQuery) -> str:
    """The label of the button the user pressed; callback data if the message is gone."""
    markup = getattr(query.message, "reply_markup", None)
    for row in markup.inline_keyboard if markup else []:
        for button in row:
            if button.callback_data == query.data:
                return button.text
    return query.data or ""


class Support:
    def __init__(self, bot: Bot, db: Db, chat_id: int | None):
        self.bot = bot
        self.db = db
        self.chat_id = chat_id
        # two quick messages from a new user must not open two topics
        self._locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def topic(self, user_id: int, who: User | None = None) -> int | None:
        async with self._locks[user_id]:
            topic_id = await self.db.get_topic(user_id)
            if topic_id is not None:
                return topic_id
            if who is None:
                # the bot wrote first (a result after a restart), so there is no User at hand
                chat = await self.bot.get_chat(user_id)
                who = User(id=user_id, is_bot=False, first_name=chat.first_name or "",
                           last_name=chat.last_name, username=chat.username)
            name = who.full_name.strip() or str(user_id)
            title = f"{name} @{who.username}" if who.username else name
            try:
                topic = await self.bot.create_forum_topic(self.chat_id, title[:TOPIC_NAME_LIMIT])
            except TelegramAPIError:
                log.exception("could not open a support topic for user %d", user_id)
                return None
            await self.db.set_topic(user_id, topic.message_thread_id)
            card = texts.SUPPORT_CARD.format(
                name=escape(name), id=user_id, tg_lang=who.language_code or "?",
                username=f"@{escape(who.username)}, " if who.username else "",
            )
            try:
                await self.bot.send_message(self.chat_id, card, parse_mode="HTML",
                                            message_thread_id=topic.message_thread_id)
            except TelegramAPIError:
                # the card is a courtesy, the dialogue goes on without it
                log.exception("could not post the card of topic %d", topic.message_thread_id)
            return topic.message_thread_id

    async def _post(
        self, user_id: int, send: Callable[[int], Awaitable[Any]], who: User | None = None
    ) -> None:
        """Puts something into the user's topic. Support is a side channel, so a failure here
        is logged and never reaches the user's video."""
        if self.chat_id is None:
            return
        try:
            for attempt in range(2):
                topic_id = await self.topic(user_id, who)
                if topic_id is None:
                    return
                try:
                    await send(topic_id)
                    return
                except TelegramBadRequest as e:
                    if attempt or not thread_gone(e):
                        raise
                    log.warning("support topic %d of user %d is gone, opening a new one",
                                topic_id, user_id)
                    await self.db.set_topic(user_id, None)
        except Exception:
            log.exception("could not mirror to the support topic of user %d", user_id)

    async def incoming(self, message: Message) -> None:
        await self._post(message.from_user.id, lambda topic_id: self.bot.copy_message(
            self.chat_id, message.chat.id, message.message_id, message_thread_id=topic_id,
        ), message.from_user)

    async def button(self, query: CallbackQuery) -> None:
        text = texts.SUPPORT_BUTTON.format(text=pressed(query))
        await self._post(query.from_user.id, lambda topic_id: self.bot.send_message(
            self.chat_id, text, message_thread_id=topic_id,
        ), query.from_user)

    async def outgoing(self, user_id: int, sent: Message) -> None:
        # sent again instead of copied: a copy would carry the buttons, and they only work
        # for the user
        async def send(topic_id: int) -> None:
            if sent.video:
                await self.bot.send_video(self.chat_id, sent.video.file_id,
                                          message_thread_id=topic_id)
            else:
                await self.bot.send_message(self.chat_id, texts.SUPPORT_BOT.format(text=sent.text),
                                            message_thread_id=topic_id)

        if sent.video or sent.text:
            await self._post(user_id, send)

    async def reply(self, message: Message) -> None:
        """A message of the team in a user's topic goes to that user."""
        if message.chat.id != self.chat_id or not message.is_topic_message:
            return
        # anonymous admins write as GroupAnonymousBot, but with the group as sender_chat
        if message.from_user and message.from_user.is_bot and message.sender_chat is None:
            return
        if message.content_type not in REPLY_TYPES or (message.text or "").startswith("/"):
            return
        user_id = await self.db.user_by_topic(message.message_thread_id)
        if user_id is None:
            await message.reply(texts.SUPPORT_NO_USER)
            return
        try:
            await self.bot.copy_message(user_id, message.chat.id, message.message_id)
        except TelegramForbiddenError:
            await message.reply(texts.SUPPORT_BLOCKED)
        except TelegramAPIError as e:
            await message.reply(texts.SUPPORT_FAILED.format(error=e.message))


class MirrorOutgoing(BaseRequestMiddleware):
    """Everything the bot sends to a user shows up in their topic, wherever it was sent from:
    handlers and the worker alike."""

    def __init__(self, support: Support):
        self.support = support

    async def __call__(
        self,
        make_request: NextRequestMiddlewareType[TelegramType],
        bot: Bot,
        method: TelegramMethod[TelegramType],
    ) -> Response[TelegramType]:
        response = await make_request(bot, method)
        if (
            isinstance(method, SendMessage | SendVideo)
            and isinstance(method.chat_id, int) and method.chat_id > 0
            and isinstance(response.result, Message)
            and not _quiet.get()
        ):
            await self.support.outgoing(method.chat_id, response.result)
        return response


async def mirror_incoming(
    handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
    event: TelegramObject,
    data: dict[str, Any],
) -> Any:
    """Outer middleware for the user-facing router. Mirrors before the handler, so the topic
    reads in order: the user's message, then the bot's answer."""
    support: Support = data["support"]
    if isinstance(event, Message) and event.chat.type == "private" and event.from_user:
        await support.incoming(event)
    elif isinstance(event, CallbackQuery):
        await support.button(event)
    return await handler(event, data)


@router.message(F.chat.type != "private")
async def group_message(message: Message, support: Support) -> None:
    await support.reply(message)
