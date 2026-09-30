import hashlib
import hmac
import json
from urllib.parse import urlencode

import pytest
from aiohttp.test_utils import TestClient, TestServer

from types import SimpleNamespace

from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import GetChat

from sublya.bot.admin import OpenRouter, Profiles, make_app, verify_init_data
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
    assert d["top"][0]["name"] == "Alice" and d["top"][0]["user_id"] == 1 and d["top"][0]["videos"] == 1
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


class FakeBot:
    """getChat knows Alice; user 3 blocked the bot. Alice has a photo, nobody else does."""

    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.chats = 0

    async def get_chat(self, user_id):
        self.chats += 1
        if user_id == 3:
            raise TelegramForbiddenError(method=GetChat(chat_id=3), message="Forbidden")
        return SimpleNamespace(first_name="Alice", last_name="Smith", username="alice")

    async def get_user_profile_photos(self, user_id, limit):
        size = SimpleNamespace(file_id="F", width=160)
        return SimpleNamespace(photos=[[size]] if user_id == 1 else [])

    async def get_file(self, file_id):
        src = self.tmp / "photo.jpg"
        src.write_bytes(b"jpeg")
        return SimpleNamespace(file_path=str(src))


@pytest.fixture
def settings(tmp_path):
    # api_url: files come as local paths, like from the server's Bot API
    return Settings(bot_token=TOKEN, admin_ids=frozenset({ADMIN}), data_dir=tmp_path,
                    api_url="http://127.0.0.1:8081")


@pytest.fixture
def profiles(db, settings, tmp_path):
    return Profiles(FakeBot(tmp_path), db, settings)


async def test_missing_names_are_asked_once(profiles, db):
    rows = [{"user_id": 1, "name": None, "username": None},
            {"user_id": 3, "name": None, "username": None},
            {"user_id": 5, "name": "Known", "username": None}]
    await profiles.fill_names(rows)
    assert rows[0]["name"] == "Alice Smith" and rows[0]["username"] == "alice"
    assert rows[1]["name"] is None and rows[2]["name"] == "Known"
    assert (await db.get_user(1)).name == "Alice Smith"
    await profiles.fill_names([{"user_id": 3, "name": None, "username": None}])
    assert profiles.bot.chats == 2


async def test_avatars_are_cached_on_disk(profiles):
    path = await profiles.avatar(1)
    assert path.read_bytes() == b"jpeg"
    assert await profiles.avatar(2) is None
    assert (profiles.dir / "2.none").exists()


@pytest.fixture
async def client(db, settings, profiles):
    c = TestClient(TestServer(make_app(db, settings, FakeOpenRouter(), profiles)))
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


async def test_avatar_needs_an_admin_and_a_photo(client):
    import time
    auth = {"Authorization": "tma " + signed(auth_date=time.time())}
    assert (await client.get("/admin/api/avatar/1")).status == 401
    r = await client.get("/admin/api/avatar/1", headers=auth)
    assert r.status == 200 and await r.read() == b"jpeg"
    assert (await client.get("/admin/api/avatar/2", headers=auth)).status == 404
    assert (await client.get("/admin/api/avatar/x", headers=auth)).status == 404


async def test_page_is_served(client):
    r = await client.get("/admin")
    assert r.status == 200 and "telegram-web-app.js" in await r.text()
