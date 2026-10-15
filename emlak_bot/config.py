from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Config:
    bot_token: str
    owner_ids: frozenset[int]
    db_path: str
    tz: ZoneInfo
    reminder_hour: int
    inactivity_days: int
    secret_key: str
    port: int
    web_enabled: bool
    cookie_secure: bool
    web_url: str


def load_config() -> Config:
    load_dotenv()
    owners = {
        int(x) for x in os.getenv("OWNER_TELEGRAM_IDS", "").replace(" ", "").split(",") if x
    }
    secret = os.getenv("SECRET_KEY", "")
    if not secret:
        log.warning("SECRET_KEY tanımlı değil; geçici anahtar üretildi (yeniden başlatınca web oturumları kapanır).")
        secret = secrets.token_hex(32)
    on_railway = bool(os.getenv("RAILWAY_ENVIRONMENT"))
    return Config(
        bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        owner_ids=frozenset(owners),
        db_path=os.getenv("DB_PATH") or ("/data/emlak.db" if os.path.isdir("/data") else "emlak.db"),
        tz=ZoneInfo(os.getenv("TIMEZONE", "Europe/Istanbul")),
        reminder_hour=int(os.getenv("REMINDER_HOUR", "9")),
        inactivity_days=int(os.getenv("INACTIVITY_DAYS", "7")),
        secret_key=secret,
        port=int(os.getenv("PORT", "8000")),
        web_enabled=os.getenv("WEB_ENABLED", "1") != "0",
        cookie_secure=os.getenv("COOKIE_SECURE", "1" if on_railway else "0") == "1",
        web_url=os.getenv("WEB_URL", "").rstrip("/"),
    )
