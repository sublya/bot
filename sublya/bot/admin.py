"""Admin Mini App: one page and a small JSON API, served from the bot process.

Telegram signs the Mini App's initData with the bot token, so the signature is the whole
authentication: no sessions, cookies or separate secrets."""

import asyncio
import hashlib
import hmac
import json
import logging
import shutil
import time
from pathlib import Path
from urllib.parse import parse_qsl

import httpx
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiohttp import web

from .config import Settings
from .db import Db

log = logging.getLogger(__name__)

PAGE = Path(__file__).with_name("admin.html")
# how long a signed initData stays valid: the panel is reopened, not kept open for days
MAX_AGE = 24 * 3600
MAX_INIT_DATA = 8192
BALANCE_TTL = 60
AVATAR_TTL = 24 * 3600
AVATAR_SIZE = 160


def verify_init_data(init_data: str, bot_token: str, now: float | None = None) -> int | None:
    """The Telegram user id behind a signed initData, or None if the signature doesn't hold
    or is too old."""
    if not init_data or len(init_data) > MAX_INIT_DATA:
        return None
    fields = dict(parse_qsl(init_data, keep_blank_values=True))
    given = fields.pop("hash", "")
    # every field but hash takes part, the Ed25519 signature included
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(given, expected):
        return None
    try:
        if (now or time.time()) - int(fields["auth_date"]) > MAX_AGE:
            return None
        user_id = json.loads(fields["user"])["id"]
    except (KeyError, ValueError, TypeError):
        return None
    return user_id if isinstance(user_id, int) else None


class OpenRouter:
    """Balance and spending of the STT key. Cached: the panel may be refreshed often, and
    OpenRouter's own numbers lag behind anyway."""

    def __init__(self, base_url: str, key: str, client: httpx.AsyncClient | None = None):
        self.base_url = base_url.rstrip("/")
        self.key = key
        self.client = client
        self._cached: tuple[float, dict | None] | None = None

    async def _get(self, client: httpx.AsyncClient, path: str) -> dict:
        r = await client.get(f"{self.base_url}/{path}",
                             headers={"Authorization": f"Bearer {self.key}"})
        r.raise_for_status()
        return r.json()["data"]

    async def fetch(self) -> dict | None:
        async with httpx.AsyncClient(timeout=10) as own:
            client = self.client or own
            credits = await self._get(client, "credits")
            key = await self._get(client, "key")
        return {
            "balance": round(credits["total_credits"] - credits["total_usage"], 4),
            "credits": credits["total_credits"],
            "usage": credits["total_usage"],
            "usage_daily": key.get("usage_daily"),
            "usage_weekly": key.get("usage_weekly"),
            "usage_monthly": key.get("usage_monthly"),
        }

    async def balance(self) -> dict | None:
        """None when OpenRouter doesn't answer or the STT endpoint isn't OpenRouter at all."""
        now = time.monotonic()
        if self._cached and now - self._cached[0] < BALANCE_TTL:
            return self._cached[1]
        try:
            value = await self.fetch()
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            log.warning("could not get the OpenRouter balance", exc_info=True)
            value = None
        self._cached = (now, value)
        return value


class Profiles:
    """Names and avatars of the users the panel lists. Users who wrote before the bot kept
    names get theirs from getChat once; avatars live on disk for AVATAR_TTL."""

    def __init__(self, bot: Bot, db: Db, settings: Settings):
        self.bot = bot
        self.db = db
        self.settings = settings
        self.dir = settings.data_dir / "avatars"
        # getChat fails for someone who blocked the bot; asking again every refresh is useless
        self._no_name: set[int] = set()

    async def fill_names(self, rows: list[dict]) -> None:
        missing = {r["user_id"] for r in rows if not r["name"]} - self._no_name
        found = {}
        for user_id in missing:
            try:
                chat = await self.bot.get_chat(user_id)
            except TelegramAPIError:
                self._no_name.add(user_id)
                continue
            name = " ".join(filter(None, [chat.first_name, chat.last_name])) or None
            await self.db.set_user(user_id, name=name, username=chat.username)
            found[user_id] = (name, chat.username)
        for r in rows:
            if r["user_id"] in found:
                r["name"], r["username"] = found[r["user_id"]]

    async def avatar(self, user_id: int) -> Path | None:
        path = self.dir / f"{user_id}.jpg"
        none = self.dir / f"{user_id}.none"
        for cached in (path, none):
            if cached.exists() and time.time() - cached.stat().st_mtime < AVATAR_TTL:
                return path if cached is path else None
        self.dir.mkdir(parents=True, exist_ok=True)
        try:
            photos = await self.bot.get_user_profile_photos(user_id, limit=1)
            if not photos.photos:
                # no photo, or it is hidden from the bot by privacy settings
                none.touch()
                path.unlink(missing_ok=True)
                return None
            sizes = photos.photos[0]
            size = next((s for s in sizes if s.width >= AVATAR_SIZE), sizes[-1])
            file = await self.bot.get_file(size.file_id)
            if self.settings.api_url:
                # a local Bot API server hands out a path on the shared volume; copied, not
                # moved, as it keeps profile photos for itself too
                await asyncio.to_thread(shutil.copyfile, file.file_path, path)
            else:
                await self.bot.download_file(file.file_path, path)
        except (TelegramAPIError, OSError):
            log.warning("no avatar for user %d", user_id, exc_info=True)
            return None
        none.unlink(missing_ok=True)
        return path


def admin_of(request: web.Request, settings: Settings) -> web.Response | None:
    """None if the request comes from an admin, otherwise the refusal to send back."""
    header = request.headers.get("Authorization", "")
    init_data = header.removeprefix("tma ") if header.startswith("tma ") else ""
    user_id = verify_init_data(init_data, settings.bot_token)
    if user_id is None:
        return web.json_response({"error": "unauthorized"}, status=401)
    if user_id not in settings.admin_ids:
        return web.json_response({"error": "forbidden"}, status=403)
    return None


def make_app(
    db: Db, settings: Settings, openrouter: OpenRouter, profiles: Profiles
) -> web.Application:
    async def page(request: web.Request) -> web.FileResponse:
        return web.FileResponse(PAGE, headers={"Cache-Control": "no-cache"})

    async def stats(request: web.Request) -> web.Response:
        if refusal := admin_of(request, settings):
            return refusal
        data = await db.dashboard()
        await profiles.fill_names(data["top"] + data["recent"])
        data["openrouter"] = await openrouter.balance()
        data["stt_limit"] = settings.stt_daily_minutes
        data["daily_videos"] = settings.daily_videos
        data["support_chat_id"] = settings.support_chat_id
        return web.json_response(data, headers={"Cache-Control": "no-store"})

    async def avatar(request: web.Request) -> web.StreamResponse:
        if refusal := admin_of(request, settings):
            return refusal
        try:
            user_id = int(request.match_info["user_id"])
        except ValueError:
            raise web.HTTPNotFound() from None
        path = await profiles.avatar(user_id)
        if path is None:
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Cache-Control": "private, max-age=3600"})

    app = web.Application()
    app.router.add_get("/admin", page)
    app.router.add_get("/admin/", page)
    app.router.add_get("/admin/api/stats", stats)
    app.router.add_get("/admin/api/avatar/{user_id}", avatar)
    return app


async def serve(bot: Bot, db: Db, settings: Settings) -> web.AppRunner:
    openrouter = OpenRouter(settings.stt.url, settings.stt.key)
    app = make_app(db, settings, openrouter, Profiles(bot, db, settings))
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", settings.admin_port).start()
    log.info("admin panel on port %d", settings.admin_port)
    return runner
