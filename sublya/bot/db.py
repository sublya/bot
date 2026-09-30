"""SQLite state: the job queue, per-user settings and daily usage."""

import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite

from sublya.core.models import DEFAULT_STYLE

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    parent_id INTEGER REFERENCES jobs(id),
    status TEXT NOT NULL,
    input_path TEXT,
    text TEXT,
    style TEXT NOT NULL,
    lang TEXT,
    progress_msg_id INTEGER,
    error TEXT,
    created_at REAL NOT NULL,
    finished_at REAL
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, id);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    style TEXT NOT NULL,
    lang TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS usage (
    day TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    videos INTEGER NOT NULL DEFAULT 0,
    stt_seconds REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (day, user_id)
);
CREATE TABLE IF NOT EXISTS support_topics (
    user_id INTEGER PRIMARY KEY,
    topic_id INTEGER NOT NULL UNIQUE,
    created_at REAL NOT NULL
);
"""

ACTIVE = ("pending", "queued", "running")


class Busy(Exception):
    pass


class QuotaExceeded(Exception):
    pass


@dataclass
class Job:
    id: int
    user_id: int
    chat_id: int
    parent_id: int | None
    status: str
    input_path: str | None
    text: str | None
    style: str
    lang: str | None
    progress_msg_id: int | None
    error: str | None
    created_at: float
    finished_at: float | None

    @property
    def root_id(self) -> int:
        """Re-renders reuse the files of the job that received the video."""
        return self.parent_id or self.id


@dataclass
class User:
    id: int
    style: str
    lang: str | None
    created_at: float
    name: str | None = None
    username: str | None = None


# columns added after the first release: CREATE TABLE IF NOT EXISTS won't add them
MIGRATIONS = {"users": {"name": "TEXT", "username": "TEXT"}}


class Db:
    def __init__(self, conn: aiosqlite.Connection, clock: Callable[[], float] = time.time):
        self.conn = conn
        self.clock = clock

    @classmethod
    async def open(cls, path: Path, clock: Callable[[], float] = time.time) -> "Db":
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(path, isolation_level=None)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.executescript(SCHEMA)
        for table, columns in MIGRATIONS.items():
            async with conn.execute(f"PRAGMA table_info({table})") as cur:
                have = {row["name"] for row in await cur.fetchall()}
            for column, kind in columns.items():
                if column not in have:
                    await conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
        return cls(conn, clock)

    async def close(self) -> None:
        await self.conn.close()

    def today(self) -> str:
        return datetime.fromtimestamp(self.clock(), UTC).date().isoformat()

    async def _one(self, sql: str, *args) -> aiosqlite.Row | None:
        async with self.conn.execute(sql, args) as cur:
            return await cur.fetchone()

    async def get_user(self, user_id: int) -> User:
        await self.conn.execute(
            "INSERT OR IGNORE INTO users (id, style, created_at) VALUES (?, ?, ?)",
            (user_id, DEFAULT_STYLE, self.clock()),
        )
        return User(**dict(await self._one("SELECT * FROM users WHERE id = ?", user_id)))

    async def set_user(self, user_id: int, **fields: str | None) -> None:
        await self.get_user(user_id)
        for name, value in fields.items():
            if name not in ("style", "lang", "name", "username"):
                raise ValueError(name)
            await self.conn.execute(f"UPDATE users SET {name} = ? WHERE id = ?", (value, user_id))

    async def get_job(self, job_id: int) -> Job | None:
        row = await self._one("SELECT * FROM jobs WHERE id = ?", job_id)
        return Job(**dict(row)) if row else None

    async def videos_today(self, user_id: int) -> int:
        row = await self._one(
            "SELECT videos FROM usage WHERE day = ? AND user_id = ?", self.today(), user_id
        )
        return row["videos"] if row else 0

    async def submit(
        self, *, user_id: int, chat_id: int, input_path: str | None, text: str | None, style: str,
        lang: str | None, parent_id: int | None, daily_limit: int,
    ) -> int:
        """Queues a job. Re-renders (with parent_id) don't count against the daily quota.
        Without input_path the job stays pending until ready(), so the worker never sees a
        job whose video is still downloading."""
        async with self._transaction():
            active = await self._one(
                "SELECT 1 FROM jobs WHERE user_id = ? AND status IN (?, ?, ?)", user_id, *ACTIVE
            )
            if active:
                raise Busy
            if parent_id is None:
                if await self.videos_today(user_id) >= daily_limit:
                    raise QuotaExceeded
                await self.conn.execute(
                    "INSERT INTO usage (day, user_id, videos) VALUES (?, ?, 1) "
                    "ON CONFLICT (day, user_id) DO UPDATE SET videos = videos + 1",
                    (self.today(), user_id),
                )
            cur = await self.conn.execute(
                "INSERT INTO jobs (user_id, chat_id, parent_id, status, input_path, text, style,"
                " lang, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, chat_id, parent_id, "queued" if input_path else "pending", input_path, text, style, lang, self.clock()),
            )
            return cur.lastrowid

    async def next_job(self) -> Job | None:
        async with self._transaction():
            row = await self._one("SELECT id FROM jobs WHERE status = 'queued' ORDER BY id LIMIT 1")
            if not row:
                return None
            await self.conn.execute("UPDATE jobs SET status = 'running' WHERE id = ?", (row["id"],))
        return await self.get_job(row["id"])

    async def position(self, job_id: int) -> int:
        row = await self._one(
            "SELECT COUNT(*) AS n FROM jobs WHERE status = 'queued' AND id <= ?", job_id
        )
        return row["n"]

    async def set_progress_msg(self, job_id: int, msg_id: int) -> None:
        await self.conn.execute("UPDATE jobs SET progress_msg_id = ? WHERE id = ?", (msg_id, job_id))

    async def finish(self, job_id: int, status: str, error: str | None = None) -> None:
        await self.conn.execute(
            "UPDATE jobs SET status = ?, error = ?, finished_at = ? WHERE id = ?",
            (status, error, self.clock(), job_id),
        )

    async def ready(self, job_id: int, input_path: str) -> None:
        await self.conn.execute(
            "UPDATE jobs SET status = 'queued', input_path = ? WHERE id = ? AND status = 'pending'",
            (input_path, job_id),
        )

    async def requeue_running(self) -> int:
        """After a restart: interrupted jobs go back to the queue, unfinished downloads fail."""
        await self.conn.execute(
            "UPDATE jobs SET status = 'failed', error = 'restarted during download',"
            " finished_at = ? WHERE status = 'pending'",
            (self.clock(),),
        )
        cur = await self.conn.execute("UPDATE jobs SET status = 'queued' WHERE status = 'running'")
        return cur.rowcount

    async def add_stt_seconds(self, user_id: int, seconds: float) -> None:
        await self.conn.execute(
            "INSERT INTO usage (day, user_id, stt_seconds) VALUES (?, ?, ?) "
            "ON CONFLICT (day, user_id) DO UPDATE SET stt_seconds = stt_seconds + excluded.stt_seconds",
            (self.today(), user_id, seconds),
        )

    async def stt_seconds_today(self) -> float:
        row = await self._one(
            "SELECT COALESCE(SUM(stt_seconds), 0) AS s FROM usage WHERE day = ?", self.today()
        )
        return row["s"]

    async def stats(self) -> dict[str, float]:
        day = self.today()
        usage = await self._one(
            "SELECT COUNT(*) AS users, COALESCE(SUM(videos), 0) AS videos,"
            " COALESCE(SUM(stt_seconds), 0) AS stt FROM usage WHERE day = ?", day
        )
        jobs = await self._one(
            "SELECT SUM(status = 'queued') AS queued, SUM(status = 'failed') AS failed"
            " FROM jobs WHERE date(created_at, 'unixepoch') = ? OR status = 'queued'", day
        )
        total = await self._one("SELECT COUNT(*) AS n FROM users")
        return {
            "users_total": total["n"], "users_today": usage["users"], "videos_today": usage["videos"],
            "stt_minutes_today": round(usage["stt"] / 60, 1),
            "queued": jobs["queued"] or 0, "failed_today": jobs["failed"] or 0,
        }

    async def _all(self, sql: str, *args) -> list[dict]:
        async with self.conn.execute(sql, args) as cur:
            return [dict(row) for row in await cur.fetchall()]

    async def dashboard(self, days: int = 14) -> dict:
        """Everything the admin panel shows. A video is a job that received one, re-renders
        are counted apart. Days are UTC, like the daily quotas."""
        date = datetime.fromtimestamp(self.clock(), UTC).date()
        today = date.isoformat()
        week = (date - timedelta(days=6)).isoformat()
        first = (date - timedelta(days=days - 1)).isoformat()
        day_of = "date(created_at, 'unixepoch')"

        users = await self._one(
            f"SELECT COUNT(*) AS total, COALESCE(SUM({day_of} = ?), 0) AS today,"
            f" COALESCE(SUM({day_of} >= ?), 0) AS week FROM users", today, week,
        )
        active = await self._one(
            "SELECT COUNT(DISTINCT CASE WHEN day = ? THEN user_id END) AS today,"
            " COUNT(DISTINCT user_id) AS week FROM usage WHERE videos > 0 AND day >= ?",
            today, week,
        )
        videos = await self._one(
            "SELECT COUNT(*) AS total, COALESCE(SUM(status = 'done'), 0) AS done,"
            " COALESCE(SUM(status = 'failed'), 0) AS failed,"
            f" COALESCE(SUM({day_of} = ?), 0) AS today, COALESCE(SUM({day_of} >= ?), 0) AS week"
            " FROM jobs WHERE parent_id IS NULL", today, week,
        )
        rerenders = await self._one("SELECT COUNT(*) AS n FROM jobs WHERE parent_id IS NOT NULL")
        stt = await self._one(
            "SELECT COALESCE(SUM(stt_seconds), 0) AS total,"
            " COALESCE(SUM(CASE WHEN day = ? THEN stt_seconds END), 0) AS today FROM usage", today,
        )
        queue = await self._one(
            "SELECT COALESCE(SUM(status IN ('pending', 'queued')), 0) AS queued,"
            " COALESCE(SUM(status = 'running'), 0) AS running FROM jobs"
        )

        by_day = {
            (date - timedelta(days=i)).isoformat(): {"videos": 0, "done": 0, "failed": 0, "new_users": 0}
            for i in range(days - 1, -1, -1)
        }
        for row in await self._all(
            f"SELECT {day_of} AS day, COUNT(*) AS videos, SUM(status = 'done') AS done,"
            f" SUM(status = 'failed') AS failed FROM jobs"
            f" WHERE parent_id IS NULL AND {day_of} >= ? GROUP BY day", first,
        ):
            by_day[row.pop("day")].update(row)
        for row in await self._all(
            f"SELECT {day_of} AS day, COUNT(*) AS n FROM users WHERE {day_of} >= ? GROUP BY day",
            first,
        ):
            by_day[row["day"]]["new_users"] = row["n"]

        top = await self._all(
            "SELECT u.id, u.name, u.username, COUNT(j.id) AS videos,"
            " SUM(j.status = 'done') AS done, MAX(j.created_at) AS last"
            " FROM users u JOIN jobs j ON j.user_id = u.id AND j.parent_id IS NULL"
            " GROUP BY u.id ORDER BY videos DESC, last DESC LIMIT 10"
        )
        recent = await self._all(
            "SELECT j.id, j.user_id, u.name, u.username, j.status, j.style,"
            " j.parent_id IS NOT NULL AS rerender, j.created_at, j.finished_at,"
            " substr(j.error, 1, 300) AS error"
            " FROM jobs j LEFT JOIN users u ON u.id = j.user_id ORDER BY j.id DESC LIMIT 20"
        )
        return {
            "users": {**dict(users), "active_today": active["today"], "active_week": active["week"]},
            "videos": {**dict(videos), "rerenders": rerenders["n"]},
            "stt_minutes": {"total": round(stt["total"] / 60, 1), "today": round(stt["today"] / 60, 1)},
            "queue": dict(queue),
            "days": [{"day": day, **row} for day, row in by_day.items()],
            "top": top,
            "recent": recent,
        }

    async def get_topic(self, user_id: int) -> int | None:
        row = await self._one("SELECT topic_id FROM support_topics WHERE user_id = ?", user_id)
        return row["topic_id"] if row else None

    async def set_topic(self, user_id: int, topic_id: int | None) -> None:
        """None forgets the topic, so the next message opens a new one."""
        if topic_id is None:
            await self.conn.execute("DELETE FROM support_topics WHERE user_id = ?", (user_id,))
            return
        await self.conn.execute(
            "INSERT OR REPLACE INTO support_topics (user_id, topic_id, created_at) VALUES (?, ?, ?)",
            (user_id, topic_id, self.clock()),
        )

    async def user_by_topic(self, topic_id: int) -> int | None:
        row = await self._one("SELECT user_id FROM support_topics WHERE topic_id = ?", topic_id)
        return row["user_id"] if row else None

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[None]:
        # BEGIN IMMEDIATE takes the write lock up front, so check-then-insert can't race
        await self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            await self.conn.execute("ROLLBACK")
            raise
        await self.conn.execute("COMMIT")
