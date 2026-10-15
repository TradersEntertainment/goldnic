"""Mesaj metinleri (HTML)."""
from __future__ import annotations

import html
from datetime import date

from . import rent as R


def esc(x) -> str:
    return "-" if x in (None, "") else html.escape(str(x))


def strip_html(text: str) -> str:
    """Telegram HTML metnini WhatsApp/SMS için düz metne çevirir."""
    import re

    return html.unescape(re.sub(r"<[^>]+>", "", text or ""))


def days_left(p, today: date) -> int:
    return (date.fromisoformat(p["renewal_date"]) - today).days


def increase_lines(p, tufe: float | None) -> list[str]:
    lines = []
    if p["last_increase_pct"] is not None:
        amt = f" (+{R.money(p['last_increase_amount'])})" if p["last_increase_amount"] else ""
        lines.append(f"📉 Geçen sene artış: <b>{R.pct_str(p['last_increase_pct'])}</b>{amt}")
    else:
        lines.append("📉 Geçen sene artış: kayıt yok")

    if p["planned_rent"] is not None:
        diff = p["planned_rent"] - p["rent"]
        lines.append(
            f"📈 Bu sene artış: <b>{R.pct_str(p['planned_pct'])}</b> → "
            f"<b>{R.money(p['planned_rent'])}</b> (+{R.money(diff)})"
        )
    else:
        lines.append(
            f"⚠️ Bu sene artış <b>henüz girilmedi</b> → <code>/zam {p['id']} %30</code> "
            f"veya <code>/zam {p['id']} 45000</code>"
        )
    if tufe is not None:
        lines.append(
            f"ℹ️ Yasal artış sınırı (TBK 344): {R.pct2(tufe)} → "
            f"azami <b>{R.money(R.apply_pct(p['rent'], tufe))}</b>"
        )
        if p["planned_pct"] is not None and p["planned_pct"] > tufe:
            lines.append("🚨 Planlanan artış yasal sınırı aşıyor!")
    return lines


def property_card(p, agent_name: str, today: date, tufe: float | None = None) -> str:
    d = days_left(p, today)
    when = f"{d} gün kaldı" if d >= 0 else f"{-d} gün GEÇTİ"
    head = [
        f"🏠 <b>{esc(p['title'])}</b> (#{p['id']})",
        f"👔 Sorumlu emlakçı: {esc(agent_name)}",
        f"🏷 Ev sahibi: {esc(p['landlord_name'])} {esc(p['landlord_phone']) if p['landlord_phone'] else ''}".rstrip(),
        f"👤 Kiracı: {esc(p['tenant_name'])} {esc(p['tenant_phone']) if p['tenant_phone'] else ''}".rstrip(),
        f"📅 Yenileme: <b>{R.fmt_date(p['renewal_date'])}</b> ({when})",
        f"💰 Mevcut kira: <b>{R.money(p['rent'])}</b>",
    ]
    if p["notes"]:
        head.append(f"📝 Not: {esc(p['notes'])}")
    return "\n".join(head + increase_lines(p, tufe))


def reminder_message(p, agent_name: str, stage: int, today: date, tufe: float | None) -> str:
    if stage == -1:
        title = f"🔴 <b>KİRA YENİLEME GECİKTİ</b> ({-days_left(p, today)} gün)"
    elif stage == 0:
        title = "🔔 <b>Kira yenileme BUGÜN</b>"
    else:
        title = f"🔔 <b>Kira yenileme hatırlatması</b> — {days_left(p, today)} gün kaldı"
    return f"{title}\n\n{property_card(p, agent_name, today, tufe)}"


def short_line(p, today: date) -> str:
    d = days_left(p, today)
    flag = ""
    if d < 0:
        flag = "🔴"
    elif d <= 30 and p["planned_rent"] is None:
        flag = "⚠️"
    when = f"{d} g" if d >= 0 else f"{-d} g geçti"
    return (
        f"{flag}#{p['id']} {esc(p['title'])} — {esc(p['tenant_name'])} — "
        f"{R.money(p['rent'])} — {R.fmt_date(p['renewal_date'])} ({when})"
    )
