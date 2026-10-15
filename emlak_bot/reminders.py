"""Günlük hatırlatma, sorun tespiti ve ofis sahibi özeti."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from . import rent as R
from . import texts
from .db import Database
from .messenger import Messenger

log = logging.getLogger(__name__)


def get_tufe(db: Database) -> float | None:
    v = db.get_setting("tufe")
    return float(v) if v is not None else None


def _ack_buttons(p) -> list[list[tuple[str, str]]]:
    return [[("✅ Bildirildi / ilgilendim", f"ack:{p['id']}:{p['renewal_date']}")]]


async def run_reminders(db: Database, messenger: Messenger, today: date | None = None) -> int:
    """Yenilemesi yaklaşan her mülk için emlakçıya + ofis sahibine bir kez mesaj atar."""
    today = today or date.today()
    tufe = get_tufe(db)
    owners = db.list_users("owner")
    sent = 0
    for p in db.list_properties():
        stage = R.pick_stage(texts.days_left(p, today))
        if stage is None or db.reminder_exists(p["id"], p["renewal_date"], stage):
            continue
        agent = db.get_user(p["agent_id"])
        text = texts.reminder_message(p, agent["name"], stage, today, tufe)
        recipients = {u["id"]: u for u in owners}
        if agent["active"]:
            recipients[agent["id"]] = agent
        delivered = False
        for u in recipients.values():
            delivered |= await messenger.send(u["chat_id"], text, _ack_buttons(p))
        if delivered:  # hiç ulaşmadıysa yarın tekrar denensin
            db.mark_reminder(p["id"], p["renewal_date"], stage)
            sent += 1
    return sent


def collect_issues(db: Database, today: date, inactivity_days: int) -> dict[str, list]:
    props = db.list_properties()
    overdue = [p for p in props if texts.days_left(p, today) < 0]
    missing_pct = [
        p for p in props if texts.days_left(p, today) <= 15 and p["planned_rent"] is None
    ]
    cutoff = (datetime.combine(today, datetime.min.time()) - timedelta(days=inactivity_days))
    inactive = []
    for u in db.list_users("agent"):
        last = datetime.fromisoformat(u["last_active"]) if u["last_active"] else None
        if last is None or last < cutoff:
            inactive.append(u)
    unacked = []
    for p in props:
        d = texts.days_left(p, today)
        if d > 7:
            continue
        row = db.one(
            "SELECT MIN(sent_at) AS first_sent, SUM(acked) AS a FROM reminder_log"
            " WHERE property_id=? AND due_date=?",
            (p["id"], p["renewal_date"]),
        )
        if row and row["first_sent"] and not row["a"]:
            if datetime.fromisoformat(row["first_sent"]) < datetime.combine(
                today, datetime.min.time()
            ) - timedelta(days=2):
                unacked.append(p)
    return {"overdue": overdue, "missing_pct": missing_pct, "inactive": inactive, "unacked": unacked}


def build_owner_report(db: Database, today: date, inactivity_days: int, full: bool) -> str | None:
    issues = collect_issues(db, today, inactivity_days)
    has_issues = any(issues.values())
    if not full and not has_issues:
        return None

    lines = [f"📊 <b>Ofis özeti — {R.fmt_date(today)}</b>", ""]
    if has_issues:
        lines.append("🚨 <b>Dikkat gerektirenler</b>")
        for p in issues["overdue"]:
            lines.append(f"🔴 Yenileme gecikti: {texts.short_line(p, today)}")
        for p in issues["missing_pct"]:
            if p not in issues["overdue"]:
                lines.append(f"⚠️ Zam oranı girilmedi: {texts.short_line(p, today)}")
        for p in issues["unacked"]:
            lines.append(f"🔕 Hatırlatma onaylanmadı: {texts.short_line(p, today)}")
        for u in issues["inactive"]:
            last = u["last_active"][:10] if u["last_active"] else "hiç"
            lines.append(f"😴 {texts.esc(u['name'])} {inactivity_days}+ gündür botu kullanmadı (son: {last})")
        lines.append("")

    if full:
        week_ago = (datetime.combine(today, datetime.min.time()) - timedelta(days=7)).isoformat()
        lines.append("👥 <b>Emlakçı bazında</b>")
        for u in db.list_users():
            mine = db.list_properties(u["id"])
            soon = sum(1 for p in mine if 0 <= texts.days_left(p, today) <= 30)
            n7 = db.activity_count_since(u["id"], week_ago)
            lines.append(
                f"• {texts.esc(u['name'])} (id {u['id']}): {len(mine)} mülk, "
                f"30 gün içinde {soon} yenileme, son 7 günde {n7} işlem"
            )
        total = db.list_properties()
        lines.append(f"\nToplam portföy: <b>{len(total)}</b> mülk, "
                     f"<b>{R.money(sum(p['rent'] for p in total))}</b> aylık kira")
    return "\n".join(lines)


async def run_owner_digest(
    db: Database, messenger: Messenger, inactivity_days: int, today: date | None = None
) -> None:
    today = today or date.today()
    report = build_owner_report(db, today, inactivity_days, full=today.weekday() == 0)
    if not report:
        return
    for o in db.list_users("owner"):
        await messenger.send(o["chat_id"], report)


async def daily_job(db: Database, messenger: Messenger, inactivity_days: int) -> None:
    n = await run_reminders(db, messenger)
    log.info("Günlük iş: %d hatırlatma gönderildi", n)
    await run_owner_digest(db, messenger, inactivity_days)
