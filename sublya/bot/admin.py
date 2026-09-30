"""Admin Mini App: one page and one JSON endpoint, served from the bot process.

Telegram signs the Mini App's initData with the bot token, so the signature is the whole
authentication: no sessions, cookies or separate secrets."""

import hashlib
import hmac
import json
import logging
import time
from pathlib import Path
from urllib.parse import parse_qsl

import httpx
from aiohttp import web

from .config import Settings
from .db import Db

log = logging.getLogger(__name__)

PAGE = Path(__file__).with_name("admin.html")
# how long a signed initData stays valid: the panel is reopened, not kept open for days
MAX_AGE = 24 * 3600
MAX_INIT_DATA = 8192
BALANCE_TTL = 60


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


def make_app(db: Db, settings: Settings, openrouter: OpenRouter) -> web.Application:
    async def page(request: web.Request) -> web.FileResponse:
        return web.FileResponse(PAGE, headers={"Cache-Control": "no-cache"})

    async def stats(request: web.Request) -> web.Response:
        header = request.headers.get("Authorization", "")
        init_data = header.removeprefix("tma ") if header.startswith("tma ") else ""
        user_id = verify_init_data(init_data, settings.bot_token)
        if user_id is None:
            return web.json_response({"error": "unauthorized"}, status=401)
        if user_id not in settings.admin_ids:
            return web.json_response({"error": "forbidden"}, status=403)
        data = await db.dashboard()
        data["openrouter"] = await openrouter.balance()
        data["stt_limit"] = settings.stt_daily_minutes
        data["daily_videos"] = settings.daily_videos
        return web.json_response(data, headers={"Cache-Control": "no-store"})

    app = web.Application()
    app.router.add_get("/admin", page)
    app.router.add_get("/admin/", page)
    app.router.add_get("/admin/api/stats", stats)
    return app


async def serve(db: Db, settings: Settings) -> web.AppRunner:
    runner = web.AppRunner(make_app(db, settings, OpenRouter(settings.stt.url, settings.stt.key)),
                           access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", settings.admin_port).start()
    log.info("admin panel on port %d", settings.admin_port)
    return runner
