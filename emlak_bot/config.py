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
    group_chat_id: str = ""
    chat_id: str = ""
    setup_code: str = ""
    brand_name: str = "Gold Nicomedia"
    admin_username: str = ""
    admin_password: str = ""
    whatsapp_token: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_api_url: str = ""
    whatsapp_target_phone: str = ""
    evds_api_key: str = ""
    evds_series: str = "TP.FG.J0"
    tufe_refresh_hour: int = 11
    brand_address: str = "İzmit / Kocaeli"
    brand_phone: str = ""
    brand_license_no: str = ""


def load_config() -> Config:
    load_dotenv()
    raw_owners = [x for x in os.getenv("OWNER_TELEGRAM_IDS", "").replace(" ", "").split(",") if x]
    bad = [x for x in raw_owners if not x.lstrip("-").isdigit()]
    if bad:
        raise SystemExit(
            f"OWNER_TELEGRAM_IDS sadece SAYISAL Telegram ID içermeli, kullanıcı adı olmaz: {bad}. "
            "@userinfobot'a /start yazıp 'Id' değerini alın (örn. 123456789)."
        )
    owners = {int(x) for x in raw_owners}
    target_chat = (
        os.getenv("TELEGRAM_CHAT_ID")
        or os.getenv("CHAT_ID")
        or os.getenv("GROUP_CHAT_ID")
        or ""
    ).strip()
    if target_chat and not target_chat.lstrip("-").isdigit():
        raise SystemExit(
            f"CHAT_ID sayısal olmalı (bireysel ID veya gruplar için -100...): {target_chat!r}."
        )
    secret = os.getenv("SECRET_KEY", "")
    if not secret:
        log.warning("SECRET_KEY tanımlı değil; geçici anahtar üretildi (yeniden başlatınca web oturumları kapanır).")
        secret = secrets.token_hex(32)
    on_railway = bool(os.getenv("RAILWAY_ENVIRONMENT"))
    return Config(
        bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
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
        group_chat_id=target_chat,
        chat_id=target_chat,
        setup_code=os.getenv("SETUP_CODE", "").strip(),
        brand_name=os.getenv("BRAND_NAME", "Gold Nicomedia").strip() or "Gold Nicomedia",
        admin_username=os.getenv("ADMIN_USERNAME", "").strip().lower(),
        admin_password=os.getenv("ADMIN_PASSWORD", "").strip(),
        whatsapp_token=os.getenv("WHATSAPP_TOKEN", "").strip(),
        whatsapp_phone_number_id=os.getenv("WHATSAPP_PHONE_NUMBER_ID", "").strip(),
        whatsapp_api_url=os.getenv("WHATSAPP_API_URL", "").strip(),
        whatsapp_target_phone=os.getenv("WHATSAPP_TARGET_PHONE", os.getenv("WHATSAPP_PHONE", "")).strip(),
        evds_api_key=os.getenv("EVDS_API_KEY", "").strip(),
        evds_series=os.getenv("EVDS_TUFE_SERIES", "TP.FG.J0").strip() or "TP.FG.J0",
        tufe_refresh_hour=int(os.getenv("TUFE_REFRESH_HOUR", "11")),
        brand_address=os.getenv("BRAND_ADDRESS", "İzmit / Kocaeli").strip() or "İzmit / Kocaeli",
        brand_phone=os.getenv("BRAND_PHONE", "").strip(),
        brand_license_no=os.getenv("BRAND_LICENSE_NO", "").strip(),
    )
