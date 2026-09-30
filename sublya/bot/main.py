import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommand, MenuButtonWebApp, WebAppInfo

from . import texts
from .admin import serve
from .config import Settings
from .db import Db
from .handlers import router
from .support import MirrorOutgoing, Support, mirror_incoming
from .support import router as support_router
from .worker import Worker, cleanup_loop

COMMANDS = [
    BotCommand(command="start", description="Как пользоваться"),
    BotCommand(command="style", description="Стиль по умолчанию"),
    BotCommand(command="lang", description="Язык речи"),
]


async def admin_menu(bot: Bot, settings: Settings) -> None:
    """The button next to the input field opens the panel. Menus are set per chat, so only
    admins get it; Telegram refuses for someone who never wrote to the bot."""
    button = MenuButtonWebApp(text=texts.BTN_ADMIN, web_app=WebAppInfo(url=settings.admin_url))
    for admin in settings.admin_ids:
        try:
            await bot.set_chat_menu_button(chat_id=admin, menu_button=button)
        except TelegramAPIError:
            logging.getLogger(__name__).warning("no admin menu button for %d", admin)


def crashed(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception():
        logging.getLogger(__name__).critical("background task died", exc_info=task.exception())


async def run() -> None:
    settings = Settings.from_env()
    session = None
    if settings.api_url:
        session = AiohttpSession(api=TelegramAPIServer.from_base(settings.api_url, is_local=True))
    bot = Bot(settings.bot_token, session=session)
    db = await Db.open(settings.data_dir / "sublya.db")
    worker = Worker(bot, db, settings)
    support = Support(bot, db, settings.support_chat_id)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(support_router)
    dp.include_router(router)
    if settings.support_chat_id is not None:
        bot.session.middleware(MirrorOutgoing(support))
        router.message.outer_middleware(mirror_incoming)
        router.callback_query.outer_middleware(mirror_incoming)

    await bot.set_my_commands(COMMANDS)
    panel = None
    if settings.admin_url:
        panel = await serve(bot, db, settings)
        await admin_menu(bot, settings)
    tasks = [asyncio.create_task(worker.run()), asyncio.create_task(cleanup_loop(settings))]
    for task in tasks:
        task.add_done_callback(crashed)
    try:
        await dp.start_polling(bot, db=db, worker=worker, settings=settings, support=support)
    finally:
        for task in tasks:
            task.cancel()
        if panel:
            await panel.cleanup()
        await db.close()
        await bot.session.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(run())


if __name__ == "__main__":
    main()
