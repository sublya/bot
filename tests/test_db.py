import pytest

from sablya.bot.db import Busy, Db, QuotaExceeded


class Clock:
    def __init__(self) -> None:
        self.t = 1_790_000_000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
async def db(tmp_path):
    clock = Clock()
    d = await Db.open(tmp_path / "sablya.db", clock=clock)
    d.clock_ = clock
    yield d
    await d.close()


async def submit(db: Db, user: int, parent_id: int | None = None, limit: int = 10) -> int:
    return await db.submit(
        user_id=user, chat_id=user, input_path="in.mp4", text=None, style="classic",
        lang=None, parent_id=parent_id, daily_limit=limit,
    )


async def test_one_active_job_per_user(db):
    job = await submit(db, 1)
    with pytest.raises(Busy):
        await submit(db, 1)
    await submit(db, 2)  # other users are not affected
    await db.finish(job, "done")
    await submit(db, 1)


async def test_running_job_also_blocks(db):
    await submit(db, 1)
    await db.next_job()
    with pytest.raises(Busy):
        await submit(db, 1)


async def test_daily_quota_excludes_rerenders(db):
    first = None
    for _ in range(3):
        job = await submit(db, 1, limit=3)
        first = first or job
        await db.finish(job, "done")
    with pytest.raises(QuotaExceeded):
        await submit(db, 1, limit=3)
    rerender = await submit(db, 1, parent_id=first, limit=3)
    assert (await db.get_job(rerender)).parent_id == first
    assert await db.videos_today(1) == 3


async def test_quota_resets_next_day(db):
    job = await submit(db, 1, limit=1)
    await db.finish(job, "done")
    with pytest.raises(QuotaExceeded):
        await submit(db, 1, limit=1)
    db.clock_.t += 86400
    await submit(db, 1, limit=1)


async def test_next_job_takes_oldest_queued(db):
    a, b = await submit(db, 1), await submit(db, 2)
    job = await db.next_job()
    assert job.id == a and job.status == "running"
    assert (await db.next_job()).id == b
    assert await db.next_job() is None


async def test_requeue_running_after_restart(db):
    a = await submit(db, 1)
    await db.next_job()
    assert await db.requeue_running() == 1
    assert (await db.get_job(a)).status == "queued"
    assert (await db.next_job()).id == a


async def test_queue_position(db):
    a, b, c = await submit(db, 1), await submit(db, 2), await submit(db, 3)
    assert [await db.position(j) for j in (a, b, c)] == [1, 2, 3]
    await db.next_job()
    assert [await db.position(j) for j in (b, c)] == [1, 2]


async def test_stt_seconds_are_counted_per_day(db):
    await db.add_stt_seconds(1, 30)
    await db.add_stt_seconds(2, 45.5)
    assert await db.stt_seconds_today() == pytest.approx(75.5)
    db.clock_.t += 86400
    assert await db.stt_seconds_today() == 0


async def test_user_settings(db):
    user = await db.get_user(1)
    assert (user.style, user.lang) == ("classic", None)
    await db.set_user(1, style="big")
    await db.set_user(1, lang="ru")
    user = await db.get_user(1)
    assert (user.style, user.lang) == ("big", "ru")


async def test_finish_records_error(db):
    job = await submit(db, 1)
    await db.finish(job, "failed", error="boom")
    j = await db.get_job(job)
    assert (j.status, j.error) == ("failed", "boom")
    assert j.finished_at is not None


async def test_stats(db):
    job = await submit(db, 1)
    await db.finish(job, "failed", error="boom")
    await submit(db, 2)
    await db.add_stt_seconds(2, 90)
    stats = await db.stats()
    assert stats["videos_today"] == 2 and stats["queued"] == 1 and stats["failed_today"] == 1
    assert stats["stt_minutes_today"] == 1.5
