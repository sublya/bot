import os
from dataclasses import dataclass, field
from pathlib import Path

from sublya.core.transcribe import SttConfig

MB = 1024 * 1024
# the public Bot API refuses to hand out files bigger than this
CLOUD_DOWNLOAD_LIMIT = 20 * MB


def _ids(value: str) -> frozenset[int]:
    return frozenset(int(x) for x in value.replace(",", " ").split())


@dataclass(frozen=True)
class Settings:
    bot_token: str
    api_url: str | None = None  # local telegram-bot-api server, e.g. http://telegram-bot-api:8081
    data_dir: Path = Path("data")
    admin_ids: frozenset[int] = frozenset()
    # forum group where every user gets a topic with their dialogue; None turns support off
    support_chat_id: int | None = None
    daily_videos: int = 10
    stt_daily_minutes: float = 300
    stt_lag: float | None = None  # None: the default for the transcript's timing kind
    # recognition language for users who never picked one; None means autodetect
    default_lang: str | None = "ru"
    max_duration: int = 180
    max_size: int = 200 * MB
    job_ttl: int = 24 * 3600
    stt: SttConfig = field(default_factory=SttConfig)

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ
        token = env.get("BOT_TOKEN")
        if not token:
            raise SystemExit("BOT_TOKEN is not set")
        stt = SttConfig.from_env()
        if not stt.key:
            raise SystemExit("STT_API_KEY is not set")
        return cls(
            bot_token=token,
            api_url=env.get("TELEGRAM_API_URL") or None,
            data_dir=Path(env.get("DATA_DIR", "data")),
            admin_ids=_ids(env.get("ADMIN_IDS", "")),
            support_chat_id=int(env["SUPPORT_CHAT_ID"]) if env.get("SUPPORT_CHAT_ID") else None,
            daily_videos=int(env.get("DAILY_VIDEOS", 10)),
            stt_daily_minutes=float(env.get("STT_DAILY_MINUTES", 300)),
            stt_lag=float(env["STT_LAG"]) if env.get("STT_LAG") else None,
            default_lang=env.get("DEFAULT_LANG", "ru") or None,
            stt=stt,
        )

    @property
    def download_limit(self) -> int:
        return self.max_size if self.api_url else min(self.max_size, CLOUD_DOWNLOAD_LIMIT)

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"
