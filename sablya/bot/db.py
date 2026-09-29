"""SQLite state: the job queue, per-user settings and daily usage."""

import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from sablya.core.models import DEFAULT_STYLE

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
            if name not in ("style", "lang"):
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
