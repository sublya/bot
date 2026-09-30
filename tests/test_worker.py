import os
from pathlib import Path

import pytest
from aiogram.exceptions import TelegramBadRequest

from sublya.bot import texts
from sublya.bot.config import Settings
from sublya.bot.db import Db, Job
from sublya.bot.worker import Worker, cleanup
from sublya.core.ffmpeg import has_filter, run, tool
from sublya.core.transcribe import SttConfig


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[str, int, object]] = []
        self.notes_forbidden = False

    async def send_video(self, chat_id, video, **kw):
        self.sent.append(("video", chat_id, Path(video.path).stat().st_size))

    async def send_video_note(self, chat_id, video_note, **kw):
        if self.notes_forbidden:
            raise TelegramBadRequest(method=None, message="VOICE_MESSAGES_FORBIDDEN")
        self.sent.append(("video_note", chat_id, kw["length"]))

    async def send_message(self, chat_id, text, **kw):
        self.sent.append(("message", chat_id, text))

    async def edit_message_text(self, text, chat_id, message_id, **kw):
        self.sent.append(("edit", chat_id, text))

    async def delete_message(self, chat_id, message_id):
        self.sent.append(("delete", chat_id, message_id))


@pytest.fixture
async def env(tmp_path):
    settings = Settings(bot_token="t", data_dir=tmp_path, admin_ids=frozenset({99}),
                        stt=SttConfig(key="k"))
    db = await Db.open(tmp_path / "sublya.db")
    bot = FakeBot()
    yield settings, db, bot, Worker(bot, db, settings)
    await db.close()


async def queue(db: Db, input_path: str, parent_id: int | None = None) -> int:
    job = await db.submit(user_id=1, chat_id=1, input_path=input_path, text=None,
                          style="classic", lang=None, parent_id=parent_id, daily_limit=10)
    await db.set_progress_msg(job, 500)
    return job


async def test_missing_video_expires(env):
    settings, db, bot, worker = env
    job = await queue(db, str(settings.jobs_dir / "1" / "input.mp4"))
    await worker.process(await db.next_job())
    assert (await db.get_job(job)).status == "failed"
    assert ("message", 1, texts.EXPIRED) in bot.sent
    assert ("delete", 1, 500) in bot.sent
    assert not any(chat == 99 for _, chat, _ in bot.sent), "users' mistakes don't page admins"


async def test_rerender_uses_cached_transcript(env, transcript, poem):
    if not has_filter("ass"):
        pytest.skip("ffmpeg without libass (set SUBLYA_FFMPEG)")
    settings, db, bot, worker = env
    work = settings.jobs_dir / "1"
    work.mkdir(parents=True)
    video = work / "input.mp4"
    run(tool("ffmpeg"), "-v", "error", "-f", "lavfi", "-i", "color=c=gray:s=180x320:d=55:r=2",
        "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono", "-t", "55", "-shortest",
        "-c:v", "libx264", "-c:a", "aac", str(video))
    transcript.size = (180, 320)
    (work / "transcript.json").write_text(transcript.to_json(), encoding="utf-8")

    root = await queue(db, str(video))
    await db.finish(root, "done")
    rerender = await queue(db, str(video), parent_id=root)
    await worker.process(await db.next_job())

    assert (await db.get_job(rerender)).status == "done"
    assert [kind for kind, _, _ in bot.sent] == ["edit", "video", "delete"]
    assert await db.stt_seconds_today() == 0, "a re-render must not call STT"
    assert not list(work.glob("out-*.mp4")), "the rendered file is removed after sending"


async def note_job(db: Db, parent_id: int | None = None) -> Job:
    job = await db.submit(user_id=1, chat_id=1, input_path="in.mp4", text=None, style="classic",
                          lang=None, parent_id=parent_id, daily_limit=10, note=True)
    return await db.get_job(job)


async def test_video_note_goes_back_as_video_note(env, tmp_path, transcript):
    _, db, bot, worker = env
    out = tmp_path / "out.mp4"
    out.write_bytes(b"x")
    transcript.size = (384, 384)
    await worker.send(await note_job(db), out, transcript)
    assert bot.sent == [("video_note", 1, 384)]


async def test_forbidden_video_note_falls_back_to_video(env, tmp_path, transcript):
    _, db, bot, worker = env
    bot.notes_forbidden = True
    out = tmp_path / "out.mp4"
    out.write_bytes(b"x")
    await worker.send(await note_job(db), out, transcript)
    assert [kind for kind, _, _ in bot.sent] == ["video"]


def test_cleanup_removes_only_stale_job_dirs(tmp_path):
    old, fresh = tmp_path / "1", tmp_path / "2"
    for d in (old, fresh):
        d.mkdir()
        (d / "input.mp4").write_bytes(b"x")
    os.utime(old, (1000, 1000))
    assert cleanup(tmp_path, ttl=3600, now=1000 + 3601) == 1
    assert not old.exists() and fresh.exists()
