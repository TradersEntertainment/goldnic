"""Zamanlayıcının çalıştırdığı senkron arka plan görevleri."""
from __future__ import annotations

import logging
from typing import Any

from . import law as L
from . import rent as R
from .messenger import send_telegram_sync
from .reminders import local_today

log = logging.getLogger(__name__)


def owner_targets(db, cfg) -> set[str]:
    """Telegram bildirim hedefleri: ofis sahiplerinin Telegram sohbetleri + tanımlıysa ofis grubu/sohbeti."""
    targets = {u["chat_id"] for u in db.list_users("owner") if u["channel"] == "telegram"}
    if getattr(cfg, "chat_id", ""):
        targets.add(cfg.chat_id)
    elif getattr(cfg, "group_chat_id", ""):
        targets.add(cfg.group_chat_id)
    return targets


def tufe_job(db, cfg: Any) -> list[str]:
    """Beklenen TÜFE ayı eksikse TÜİK/EVDS'den çeker; yeni oran gelirse ofise duyurur."""
    from . import tufe

    today = local_today(cfg)
    if not tufe.needs_refresh(db, today):
        return []
    res = tufe.refresh(db, today, evds_key=getattr(cfg, "evds_api_key", ""),
                       evds_series=getattr(cfg, "evds_series", "TP.FG.J0"))
    if res.periods and getattr(cfg, "bot_token", ""):
        rates = tufe.load_rates(db)
        newest = max(res.periods)
        rate = rates[newest]
        text = (f"📈 <b>Yeni kira artış tavanı açıklandı</b>\n"
                f"{L.period_label(L.shift_period(newest, 1))} yenilemeleri için yasal sınır: <b>{R.pct2(rate.avg12)}</b>\n"
                f"(TÜİK {L.period_label(newest)} TÜFE 12 aylık ortalama · {tufe.SOURCE_LABELS.get(rate.source, rate.source)})")
        for target in owner_targets(db, cfg):
            send_telegram_sync(cfg.bot_token, target, text)
    return res.periods or []
