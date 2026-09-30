from datetime import datetime
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.methods import SendMessage
from aiogram.types import Chat, Message, User

from sublya.bot import texts
from sublya.bot.db import Db
from sublya.bot.support import Support

GROUP = -1001
ALICE = User(id=42, is_bot=False, first_name="Alice", username="alice", language_code="ru")
ADMIN = User(id=7, is_bot=False, first_name="Admin")


class FakeBot:
    """Records Bot API calls; `fail` makes the next call of that method raise."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []
        self.fail: dict[str, Exception] = {}
        self.next_topic = 100
        self.id = 1

    def _record(self, name: str, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        if name in self.fail:
            raise self.fail.pop(name)

    def named(self, name: str) -> list[tuple[tuple, dict]]:
        return [(a, k) for n, a, k in self.calls if n == name]

    async def create_forum_topic(self, chat_id, name):
        self._record("create_forum_topic", chat_id, name)
        self.next_topic += 1
        return SimpleNamespace(message_thread_id=self.next_topic)

    async def send_message(self, chat_id, text, **kwargs):
        self._record("send_message", chat_id, text, **kwargs)

    async def send_video(self, chat_id, video, **kwargs):
        self._record("send_video", chat_id, video, **kwargs)

    async def copy_message(self, chat_id, from_chat_id, message_id, **kwargs):
        self._record("copy_message", chat_id, from_chat_id, message_id, **kwargs)

    async def __call__(self, method, request_timeout=None):
        # message.reply() lands here
        self._record("reply", method.text)


def bad_request(text: str) -> TelegramBadRequest:
    return TelegramBadRequest(method=SendMessage(chat_id=GROUP, text="x"), message=text)


@pytest.fixture
async def db(tmp_path):
    d = await Db.open(tmp_path / "sublya.db")
    yield d
    await d.close()


@pytest.fixture
def bot() -> FakeBot:
    return FakeBot()


@pytest.fixture
def support(bot, db) -> Support:
    return Support(bot, db, GROUP)


def private(text: str = "привет") -> Message:
    return Message(message_id=10, date=datetime.now(), chat=Chat(id=ALICE.id, type="private"),
                   from_user=ALICE, text=text)


def in_group(bot, text: str | None = "ответ", thread: int | None = 101, **extra) -> Message:
    fields = {"text": text} if text is not None else {}
    return Message(
        message_id=20, date=datetime.now(),
        chat=extra.pop("chat", Chat(id=GROUP, type="supergroup")),
        from_user=extra.pop("from_user", ADMIN), message_thread_id=thread,
        is_topic_message=thread is not None, **fields, **extra,
    ).as_(bot)


async def test_topics_are_stored_both_ways(db):
    await db.set_topic(ALICE.id, 101)
    assert await db.get_topic(ALICE.id) == 101
    assert await db.user_by_topic(101) == ALICE.id
    await db.set_topic(ALICE.id, None)
    assert await db.get_topic(ALICE.id) is None
    assert await db.user_by_topic(101) is None


async def test_first_message_opens_a_topic_with_a_card(support, bot, db):
    await support.incoming(private())
    await support.incoming(private("ещё"))

    [(args, _)] = bot.named("create_forum_topic")
    assert args == (GROUP, "Alice @alice")
    [(card, kwargs)] = bot.named("send_message")
    assert "<code>42</code>" in card[1] and kwargs["message_thread_id"] == 101
    copies = bot.named("copy_message")
    assert len(copies) == 2
    assert all(a == (GROUP, ALICE.id, 10) and k["message_thread_id"] == 101 for a, k in copies)
    assert await db.user_by_topic(101) == ALICE.id


async def test_deleted_topic_is_replaced(support, bot, db):
    await db.set_topic(ALICE.id, 55)
    bot.fail["copy_message"] = bad_request("Bad Request: message thread not found")

    await support.incoming(private())

    assert await db.get_topic(ALICE.id) == 101
    assert [k["message_thread_id"] for _, k in bot.named("copy_message")] == [55, 101]


async def test_support_failures_do_not_reach_the_user(support, bot):
    bot.fail["create_forum_topic"] = bad_request("Bad Request: the chat is not a forum")
    await support.incoming(private())  # no exception
    assert bot.named("copy_message") == []


async def test_disabled_support_does_nothing(bot, db):
    await Support(bot, db, None).incoming(private())
    assert bot.calls == []


async def test_bot_video_is_sent_again_without_buttons(support, bot, db):
    await db.set_topic(ALICE.id, 101)
    sent = SimpleNamespace(video=SimpleNamespace(file_id="VID"), text=None)
    await support.outgoing(ALICE.id, sent)
    [(args, kwargs)] = bot.named("send_video")
    assert args == (GROUP, "VID") and "reply_markup" not in kwargs


async def test_team_reply_goes_to_the_user(support, bot, db):
    await db.set_topic(ALICE.id, 101)
    await support.reply(in_group(bot))
    [(args, kwargs)] = bot.named("copy_message")
    assert args == (ALICE.id, GROUP, 20) and "message_thread_id" not in kwargs


@pytest.mark.parametrize("message", [
    lambda bot: in_group(bot, "/stats"),
    lambda bot: in_group(bot, "/stats@sublyarobot"),
    lambda bot: in_group(bot, thread=None),  # General
    lambda bot: in_group(bot, from_user=User(id=1, is_bot=True, first_name="bot")),
    lambda bot: in_group(bot, text=None, forum_topic_edited={"name": "x"}),
])
async def test_what_never_reaches_the_user(support, bot, db, message):
    await db.set_topic(ALICE.id, 101)
    await support.reply(message(bot))
    assert bot.calls == []


async def test_other_groups_are_ignored(bot, db):
    await db.set_topic(ALICE.id, 101)
    await Support(bot, db, -999).reply(in_group(bot))
    assert bot.calls == []


async def test_orphan_topic_warns_the_team(support, bot):
    await support.reply(in_group(bot, thread=555))
    assert [a for a, _ in bot.named("reply")] == [(texts.SUPPORT_NO_USER,)]


async def test_blocked_user_warns_the_team(support, bot, db):
    await db.set_topic(ALICE.id, 101)
    bot.fail["copy_message"] = TelegramForbiddenError(
        method=SendMessage(chat_id=ALICE.id, text="x"), message="Forbidden: bot was blocked"
    )
    await support.reply(in_group(bot))
    assert [a for a, _ in bot.named("reply")] == [(texts.SUPPORT_BLOCKED,)]


async def test_groups_never_get_the_not_a_video_answer(bot, db):
    from aiogram import Dispatcher
    from aiogram.types import Update

    from sublya.bot.handlers import router
    from sublya.bot.support import router as support_router

    dp = Dispatcher()
    # the routers are module globals, and a router joins one dispatcher only
    for r in (support_router, router):
        r._parent_router = None
    dp.include_routers(support_router, router)
    update = Update(update_id=1, message=in_group(bot, "что-то", chat=Chat(id=-999, type="group")))
    await dp.feed_update(bot, update, db=db, support=Support(bot, db, GROUP))
    assert bot.calls == []


async def test_outgoing_middleware_passes_the_result_through(db):
    from aiogram import Bot
    from aiogram.client.session.base import BaseSession

    from sublya.bot.support import MirrorOutgoing

    sent = Message(message_id=5, date=datetime.now(), chat=Chat(id=ALICE.id, type="private"),
                   text="Готово.")

    class Session(BaseSession):
        async def make_request(self, bot, method, timeout=None):
            return sent

        async def stream_content(self, *args, **kwargs):
            raise NotImplementedError

        async def close(self):
            pass

    mirrored = []

    class Recorder(Support):
        async def outgoing(self, user_id, message):
            mirrored.append((user_id, message))

    bot = Bot("1:token", session=Session())
    bot.session.middleware(MirrorOutgoing(Recorder(bot, db, GROUP)))
    assert await bot.send_message(ALICE.id, "Готово.") is sent
    assert mirrored == [(ALICE.id, sent)]


def test_ack_once_an_hour(support):
    assert support.ack_due(ALICE.id, now=0)
    assert not support.ack_due(ALICE.id, now=1800)
    assert support.ack_due(ALICE.id, now=3600)
    assert support.ack_due(8, now=1800)


async def private_text(bot, db, support) -> list:
    from aiogram import Dispatcher
    from aiogram.types import Update

    from sublya.bot.handlers import router
    from sublya.bot.support import router as support_router

    dp = Dispatcher()
    # the routers are module globals, and a router joins one dispatcher only
    for r in (support_router, router):
        r._parent_router = None
    dp.include_routers(support_router, router)
    for i in range(2):
        message = private("есть вопрос").model_copy(update={"message_id": 10 + i}).as_(bot)
        await dp.feed_update(bot, Update(update_id=i, message=message), db=db, support=support)
    return [a for a, _ in bot.named("reply")]


async def test_text_goes_to_support_with_one_ack(bot, db):
    assert await private_text(bot, db, Support(bot, db, GROUP)) == [(texts.FORWARDED,)]


async def test_without_support_text_is_not_a_video(bot, db):
    assert await private_text(bot, db, Support(bot, db, None)) == [(texts.NOT_VIDEO,)] * 2
