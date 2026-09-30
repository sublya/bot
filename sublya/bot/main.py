import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

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
    tasks = [asyncio.create_task(worker.run()), asyncio.create_task(cleanup_loop(settings))]
    for task in tasks:
        task.add_done_callback(crashed)
    try:
        await dp.start_polling(bot, db=db, worker=worker, settings=settings, support=support)
    finally:
        for task in tasks:
            task.cancel()
        await db.close()
        await bot.session.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(run())


if __name__ == "__main__":
    main()
