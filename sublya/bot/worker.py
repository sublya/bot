"""One background worker, one job at a time: parallel ffmpeg only slows everyone."""

import asyncio
import logging
import shutil
import time
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from sublya.core import NoSpeech, SttError, Transcript, align, render, transcribe
from sublya.core.audio import duration
from sublya.core.ffmpeg import FFmpegError

from . import texts
from .config import Settings
from .db import Db, Job
from .support import quiet

log = logging.getLogger(__name__)

IDLE_POLL = 5
CLEANUP_EVERY = 3600


class UserError(Exception):
    """A failure the user can understand; the message goes to them as is."""


def result_keyboard(job_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=texts.BTN_FIX, callback_data=f"fix:{job_id}"),
        InlineKeyboardButton(text=texts.BTN_STYLE, callback_data=f"restyle:{job_id}"),
    ]])


def job_dir(settings: Settings, job: Job) -> Path:
    return settings.jobs_dir / str(job.root_id)


class Worker:
    def __init__(self, bot: Bot, db: Db, settings: Settings):
        self.bot = bot
        self.db = db
        self.settings = settings
        self.wake = asyncio.Event()

    async def run(self) -> None:
        requeued = await self.db.requeue_running()
        if requeued:
            log.info("requeued %d interrupted jobs", requeued)
        while True:
            job = await self.db.next_job()
            if job is None:
                self.wake.clear()
                try:
                    await asyncio.wait_for(self.wake.wait(), IDLE_POLL)
                except TimeoutError:
                    pass
                continue
            await self.process(job)

    async def process(self, job: Job) -> None:
        try:
            await self.handle(job)
        except UserError as e:
            await self.fail(job, str(e), str(e))
        except NoSpeech as e:
            await self.fail(job, texts.NO_SPEECH, str(e))
        except SttError as e:
            await self.fail(job, texts.STT_FAILED, str(e), notify_admins=True)
        except FFmpegError as e:
            await self.fail(job, texts.RENDER_FAILED, f"{e}\n{e.stderr}", notify_admins=True)
        except Exception as e:
            log.exception("job %d failed", job.id)
            await self.fail(job, texts.FAILED, repr(e), notify_admins=True)

    async def handle(self, job: Job) -> None:
        work = job_dir(self.settings, job)
        video = Path(job.input_path)
        if not video.exists():
            raise UserError(texts.EXPIRED)
        cached = work / "transcript.json"

        if cached.exists():
            transcript = Transcript.from_json(cached.read_text(encoding="utf-8"))
        else:
            # documents come without a duration, so the limit is checked again here
            if await asyncio.to_thread(duration, video) > self.settings.max_duration:
                raise UserError(texts.TOO_LONG.format(minutes=self.settings.max_duration // 60))
            if await self.db.stt_seconds_today() >= self.settings.stt_daily_minutes * 60:
                raise UserError(texts.STT_LIMIT)
            await self.progress(job, texts.RECOGNIZING)
            transcript = await transcribe(video, work, self.settings.stt, job.lang)
            cached.write_text(transcript.to_json(), encoding="utf-8")
            await self.db.add_stt_seconds(job.user_id, transcript.duration)

        await self.progress(job, texts.RENDERING)
        words = align(transcript, job.text, self.settings.stt_lag)
        out = await asyncio.to_thread(
            render, video, words, transcript.size, work, job.style, bool(job.note)
        )
        try:
            await self.send(job, out, transcript)
        finally:
            out.unlink(missing_ok=True)
        await self.db.finish(job.id, "done")
        await self.drop_progress(job)

    async def send(self, job: Job, out: Path, transcript: Transcript) -> None:
        w, h = transcript.size
        duration = round(transcript.duration)
        if job.note:
            try:
                await self.bot.send_video_note(
                    job.chat_id, FSInputFile(out, filename="sublya.mp4"), length=w,
                    duration=duration, reply_markup=result_keyboard(job.id),
                )
                return
            except TelegramBadRequest:
                # users who closed voice messages in privacy settings can't get video notes
                log.warning("video note for job %d refused, sending a video", job.id)
        await self.bot.send_video(
            job.chat_id, FSInputFile(out, filename="sublya.mp4"),
            width=w, height=h, duration=duration,
            supports_streaming=True, reply_markup=result_keyboard(job.id),
        )

    async def fail(self, job: Job, user_text: str, error: str, notify_admins: bool = False) -> None:
        await self.db.finish(job.id, "failed", error=error)
        await self.drop_progress(job)
        try:
            await self.bot.send_message(job.chat_id, user_text)
        except Exception:
            log.exception("could not tell user %d about job %d", job.user_id, job.id)
        if notify_admins:
            await self.notify_admins(
                texts.ADMIN_ERROR.format(job_id=job.id, user_id=job.user_id, error=error)
            )

    async def notify_admins(self, text: str) -> None:
        for admin in self.settings.admin_ids:
            try:
                with quiet():
                    await self.bot.send_message(admin, text[-4000:])
            except Exception:
                log.exception("could not notify admin %d", admin)

    async def progress(self, job: Job, text: str) -> None:
        msg_id = await self.progress_msg(job)
        if msg_id is None:
            return
        try:
            await self.bot.edit_message_text(text, chat_id=job.chat_id, message_id=msg_id)
        except TelegramBadRequest:
            pass  # the same text again, or the user deleted the message

    async def drop_progress(self, job: Job) -> None:
        msg_id = await self.progress_msg(job)
        if msg_id is None:
            return
        try:
            await self.bot.delete_message(job.chat_id, msg_id)
        except TelegramBadRequest:
            pass

    async def progress_msg(self, job: Job) -> int | None:
        # the handler records the message right after queueing, so it may land after we start
        fresh = await self.db.get_job(job.id)
        return fresh.progress_msg_id if fresh else None


def cleanup(jobs_dir: Path, ttl: float, now: float | None = None) -> int:
    """Removes job folders idle for ttl seconds. Re-renders touch them, so they live on."""
    now = now or time.time()
    removed = 0
    for path in jobs_dir.glob("*"):
        if path.is_dir() and now - path.stat().st_mtime > ttl:
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
    return removed


async def cleanup_loop(settings: Settings) -> None:
    while True:
        removed = await asyncio.to_thread(cleanup, settings.jobs_dir, settings.job_ttl)
        if removed:
            log.info("removed %d old job folders", removed)
        await asyncio.sleep(CLEANUP_EVERY)
