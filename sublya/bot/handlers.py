import logging
import shutil
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)

from sublya.core import STYLES, Transcript

from . import texts
from .config import MB, Settings
from .db import Busy, Db, Job, QuotaExceeded
from .support import Support, quiet
from .worker import Worker, job_dir, result_keyboard

log = logging.getLogger(__name__)
router = Router()
# groups belong to support.router; here the fallback would answer every message there
router.message.filter(F.chat.type == "private")


class FixText(StatesGroup):
    waiting = State()


AUTO = "auto"


def stt_lang(chosen: str | None, default: str | None) -> str | None:
    """The language hint for recognition. users.lang is NULL until the user picks one in
    /lang, and "auto" when they picked autodetection on purpose."""
    if chosen == AUTO:
        return None
    return chosen or default


def lang_name(chosen: str | None, default: str | None) -> str:
    lang = stt_lang(chosen, default)
    return texts.LANG_NAMES.get(lang, lang) if lang else texts.LANG_AUTO


@router.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(texts.START)


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(texts.CANCELLED)


@router.message(Command("style"))
async def style_menu(message: Message, state: FSMContext, db: Db) -> None:
    await state.clear()
    user = await db.get_user(message.from_user.id)
    buttons = [InlineKeyboardButton(text=texts.STYLE_NAMES[s], callback_data=f"defstyle:{s}")
               for s in STYLES]
    await message.answer(
        texts.CHOOSE_STYLE.format(current=texts.STYLE_NAMES[user.style]),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[buttons[:2], buttons[2:]]),
    )


@router.callback_query(F.data.startswith("defstyle:"))
async def set_style(query: CallbackQuery, db: Db) -> None:
    style = query.data.split(":", 1)[1]
    if style not in STYLES:
        return await query.answer()
    await db.set_user(query.from_user.id, style=style)
    await query.message.edit_text(texts.STYLE_SET.format(name=texts.STYLE_NAMES[style]))
    await query.answer()


@router.message(Command("lang"))
async def lang_menu(message: Message, state: FSMContext, db: Db, settings: Settings) -> None:
    await state.clear()
    user = await db.get_user(message.from_user.id)
    buttons = [InlineKeyboardButton(text=texts.BTN_AUTO, callback_data=f"lang:{AUTO}")] + [
        InlineKeyboardButton(text=name, callback_data=f"lang:{code}")
        for code, name in texts.LANG_NAMES.items()
    ]
    await message.answer(
        texts.CHOOSE_LANG.format(current=lang_name(user.lang, settings.default_lang)),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[buttons[:3], buttons[3:]]),
    )


@router.callback_query(F.data.startswith("lang:"))
async def set_lang(query: CallbackQuery, db: Db, settings: Settings) -> None:
    code = query.data.split(":", 1)[1]
    if code != AUTO and code not in texts.LANG_NAMES:
        return await query.answer()
    await db.set_user(query.from_user.id, lang=code)
    await query.message.edit_text(
        texts.LANG_SET.format(name=lang_name(code, settings.default_lang))
    )
    await query.answer()


@router.message(Command("stats"))
async def stats(message: Message, db: Db, settings: Settings) -> None:
    if message.from_user.id not in settings.admin_ids:
        return
    await message.answer(texts.STATS.format(**await db.stats(), stt_limit=settings.stt_daily_minutes))


@router.message(Command("admin"))
async def admin(message: Message, settings: Settings) -> None:
    if message.from_user.id not in settings.admin_ids or not settings.admin_url:
        return
    button = InlineKeyboardButton(text=texts.BTN_ADMIN, web_app=WebAppInfo(url=settings.admin_url))
    await message.answer(texts.ADMIN_PANEL,
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[[button]]))


async def enqueue(
    message: Message, db: Db, worker: Worker, settings: Settings, *, user_id: int,
    text: str | None, style: str, lang: str | None, parent: Job | None = None,
    note: bool = False,
) -> int | None:
    """Queues a job and posts the progress message the worker will keep editing.
    Re-renders keep the parent's shape: a video note comes back as a video note."""
    try:
        job_id = await db.submit(
            user_id=user_id, chat_id=message.chat.id,
            input_path=parent.input_path if parent else None,
            text=text, style=style, lang=lang,
            parent_id=parent.root_id if parent else None, daily_limit=settings.daily_videos,
            note=bool(parent.note) if parent else note,
        )
    except Busy:
        await message.answer(texts.BUSY)
        return None
    except QuotaExceeded:
        await message.answer(texts.QUOTA.format(daily=settings.daily_videos))
        return None
    position = await db.position(job_id) or 1
    with quiet():
        progress = await message.answer(texts.QUEUED.format(position=position))
    await db.set_progress_msg(job_id, progress.message_id)
    worker.wake.set()
    return job_id


async def download(bot: Bot, file_id: str, dest: Path, settings: Settings) -> None:
    file = await bot.get_file(file_id)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if settings.api_url:
        # a local Bot API server hands out a path on the shared volume; moving it keeps the
        # server's cache from growing
        shutil.move(file.file_path, dest)
    else:
        await bot.download_file(file.file_path, dest)


@router.message(StateFilter(None), F.video | F.video_note | F.document)
async def receive_video(
    message: Message, bot: Bot, db: Db, worker: Worker, settings: Settings
) -> None:
    media = message.video or message.video_note or message.document
    if message.document and not (message.document.mime_type or "").startswith("video/"):
        await message.answer(texts.NOT_VIDEO)
        return
    if (getattr(media, "duration", None) or 0) > settings.max_duration:
        await message.answer(texts.TOO_LONG.format(minutes=settings.max_duration // 60))
        return
    if (media.file_size or 0) > settings.download_limit:
        await message.answer(texts.TOO_BIG.format(mb=settings.download_limit // MB))
        return

    user = await db.get_user(message.from_user.id)
    # names are only for the admin panel, so they are refreshed when a video comes
    await db.set_user(user.id, name=message.from_user.full_name,
                      username=message.from_user.username)
    text = (message.caption or "").strip() or None
    job_id = await enqueue(message, db, worker, settings, user_id=user.id, text=text,
                           style=user.style, lang=stt_lang(user.lang, settings.default_lang),
                           note=message.video_note is not None)
    if job_id is None:
        return

    name = getattr(media, "file_name", None) or ""
    ext = Path(name).suffix.lower() or ".mp4"
    dest = settings.jobs_dir / str(job_id) / f"input{ext}"
    try:
        await download(bot, media.file_id, dest, settings)
    except Exception as e:
        log.exception("download of job %d failed", job_id)
        await db.finish(job_id, "failed", error=f"download: {e!r}")
        await message.answer(texts.DOWNLOAD_FAILED)
        return
    await db.ready(job_id, str(dest))
    worker.wake.set()


async def owned_job(query: CallbackQuery, db: Db, settings: Settings, job_id: int) -> Job | None:
    job = await db.get_job(job_id)
    if job is None or job.user_id != query.from_user.id:
        await query.answer(texts.NOT_YOURS, show_alert=True)
        return None
    if not (job_dir(settings, job) / "transcript.json").exists():
        await query.answer(texts.EXPIRED, show_alert=True)
        return None
    return job


@router.callback_query(F.data.startswith("fix:"))
async def fix_text(query: CallbackQuery, state: FSMContext, db: Db, settings: Settings) -> None:
    job = await owned_job(query, db, settings, int(query.data.split(":")[1]))
    if job is None:
        return
    transcript = Transcript.from_json(
        (job_dir(settings, job) / "transcript.json").read_text(encoding="utf-8")
    )
    await state.set_state(FixText.waiting)
    await state.update_data(job_id=job.id)
    await query.message.answer(texts.FIX_PROMPT)
    await query.message.answer(transcript.text[:4000])
    await query.answer()


@router.message(FixText.waiting, F.text, ~F.text.startswith("/"))
async def fixed_text(
    message: Message, state: FSMContext, db: Db, worker: Worker, settings: Settings
) -> None:
    job = await db.get_job((await state.get_data())["job_id"])
    await state.clear()
    if job is None or not (job_dir(settings, job) / "transcript.json").exists():
        await message.answer(texts.EXPIRED)
        return
    await enqueue(message, db, worker, settings, user_id=job.user_id, text=message.text,
                  style=job.style, lang=job.lang, parent=job)


@router.message(FixText.waiting)
async def still_waiting(message: Message) -> None:
    await message.answer(texts.FIX_WAITING)


@router.callback_query(F.data.startswith("restyle:"))
async def restyle_menu(query: CallbackQuery, db: Db, settings: Settings) -> None:
    job = await owned_job(query, db, settings, int(query.data.split(":")[1]))
    if job is None:
        return
    buttons = [InlineKeyboardButton(text=texts.STYLE_NAMES[s], callback_data=f"st:{job.id}:{s}")
               for s in STYLES]
    back = InlineKeyboardButton(text=texts.BTN_BACK, callback_data=f"back:{job.id}")
    await query.message.edit_reply_markup(
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[buttons[:2], buttons[2:], [back]])
    )
    await query.answer()


@router.callback_query(F.data.startswith("back:"))
async def restyle_back(query: CallbackQuery) -> None:
    await query.message.edit_reply_markup(reply_markup=result_keyboard(int(query.data.split(":")[1])))
    await query.answer()


@router.callback_query(F.data.startswith("st:"))
async def restyle(query: CallbackQuery, db: Db, worker: Worker, settings: Settings) -> None:
    _, job_id, style = query.data.split(":")
    job = await owned_job(query, db, settings, int(job_id))
    if job is None or style not in STYLES:
        return
    await query.message.edit_reply_markup(reply_markup=result_keyboard(job.id))
    await query.answer()
    await enqueue(query.message, db, worker, settings, user_id=job.user_id, text=job.text,
                  style=style, lang=job.lang, parent=job)


@router.message()
async def fallback(message: Message, support: Support) -> None:
    # with a support chat anything but a video is a message for the team, and it is already
    # in the user's topic
    if not support.enabled:
        await message.answer(texts.NOT_VIDEO)
    elif support.ack_due(message.from_user.id):
        await message.answer(texts.FORWARDED)
