import hashlib
import hmac
import json
from urllib.parse import urlencode

import pytest
from aiohttp.test_utils import TestClient, TestServer

from sublya.bot.admin import OpenRouter, make_app, verify_init_data
from sublya.bot.config import Settings
from sublya.bot.db import Db

TOKEN = "123:secret"
NOW = 1_790_000_000.0
ADMIN = 7


def signed(user_id: int = ADMIN, auth_date: float = NOW, token: str = TOKEN) -> str:
    fields = {"auth_date": str(int(auth_date)), "query_id": "q",
              "user": json.dumps({"id": user_id, "first_name": "A"}), "signature": "sig"}
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def test_valid_signature_gives_the_user():
    assert verify_init_data(signed(), TOKEN, now=NOW + 60) == ADMIN


@pytest.mark.parametrize("init_data", [
    "",
    signed(token="999:other"),
    signed().replace("query_id=q", "query_id=x"),
    signed(auth_date=NOW - 2 * 24 * 3600),
    "hash=abc&user=%7B%22id%22%3A7%7D",
])
def test_bad_init_data_is_refused(init_data):
    assert verify_init_data(init_data, TOKEN, now=NOW) is None


class Clock:
    t = NOW

    def __call__(self) -> float:
        return self.t


@pytest.fixture
async def db(tmp_path):
    d = await Db.open(tmp_path / "sublya.db", clock=Clock())
    yield d
    await d.close()


async def submit(db: Db, user: int, parent: int | None = None) -> int:
    return await db.submit(user_id=user, chat_id=user, input_path="in.mp4", text=None,
                           style="classic", lang=None, parent_id=parent, daily_limit=10)


async def test_dashboard_counts_videos_apart_from_rerenders(db):
    await db.set_user(1, name="Alice", username="alice")
    first = await submit(db, 1)
    await db.finish(first, "done")
    rerender = await submit(db, 1, parent=first)
    await db.finish(rerender, "done")
    await db.get_user(2)
    failed = await submit(db, 2)
    await db.finish(failed, "failed", error="boom")
    await db.add_stt_seconds(1, 90)

    d = await db.dashboard(days=3)

    assert d["users"]["total"] == 2 and d["users"]["today"] == 2
    assert d["users"]["active_today"] == 2
    assert d["videos"] == {"total": 2, "done": 1, "failed": 1, "today": 2, "week": 2, "rerenders": 1}
    assert d["stt_minutes"] == {"total": 1.5, "today": 1.5}
    assert [day["videos"] for day in d["days"]] == [0, 0, 2]
    assert d["days"][-1]["new_users"] == 2
    assert d["top"][0]["name"] == "Alice" and d["top"][0]["videos"] == 1
    assert [j["id"] for j in d["recent"]] == [failed, rerender, first]
    assert d["recent"][0]["error"] == "boom"


async def test_old_databases_get_the_name_columns(tmp_path):
    import aiosqlite
    path = tmp_path / "old.db"
    async with aiosqlite.connect(path) as conn:
        await conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, style TEXT NOT NULL,"
                           " lang TEXT, created_at REAL NOT NULL)")
        await conn.commit()
    db = await Db.open(path)
    await db.set_user(1, name="Alice")
    assert (await db.get_user(1)).name == "Alice"
    await db.close()


class FakeOpenRouter(OpenRouter):
    def __init__(self):
        super().__init__("https://openrouter.ai/api/v1", "key")

    async def fetch(self):
        return {"balance": 7.93}


@pytest.fixture
async def client(db):
    settings = Settings(bot_token=TOKEN, admin_ids=frozenset({ADMIN}))
    c = TestClient(TestServer(make_app(db, settings, FakeOpenRouter())))
    await c.start_server()
    yield c
    await c.close()


async def test_api_needs_a_signature(client):
    assert (await client.get("/admin/api/stats")).status == 401


async def test_api_is_for_admins_only(client):
    # the real clock is used here, so the signature is made fresh
    import time
    r = await client.get("/admin/api/stats", headers={"Authorization": "tma " + signed(8, time.time())})
    assert r.status == 403


async def test_admin_gets_the_dashboard(client):
    import time
    r = await client.get("/admin/api/stats", headers={"Authorization": "tma " + signed(auth_date=time.time())})
    assert r.status == 200
    data = await r.json()
    assert data["openrouter"] == {"balance": 7.93} and "users" in data


async def test_page_is_served(client):
    r = await client.get("/admin")
    assert r.status == 200 and "telegram-web-app.js" in await r.text()
