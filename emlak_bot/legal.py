"""Hukuki radar servisleri (veritabanı tarafı).

Mülk bazında hukuki durum (TÜFE tavanı, 5. yıl tespit, 10. yıl tahliye, tahliye taahhütleri),
ofis geneli radar, onay ("ilgilenildi") kayıtları, emsal kira tahmini ve kademeli bildirimler.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from .db import now_iso
from . import features as F
from . import law as L
from . import rent as R
from . import texts
from . import tufe

log = logging.getLogger(__name__)

PROPERTY_KINDS = {"konut": "Konut", "isyeri": "Çatılı işyeri"}
KIND_TITLES = {"tufe": "TÜFE tavanı", "taahhut": "Tahliye taahhüdü", "tespit5": "5. yıl kira tespiti",
               "tahliye10": "10. yıl tahliye hakkı", "veri": "Eksik sözleşme verisi"}
ACK_ACTIONS = {
    "tespit5": {"dava": "Tespit davası açıldı", "bildirim": "Yazılı artış bildirimi gönderildi",
                "anlasma": "Kiracıyla yeni kirada anlaşıldı", "vazgec": "Bu dönem için vazgeçildi"},
    "tahliye10": {"ihtar": "İhtarname tebliğ edildi", "vazgec": "Hak bu yıl kullanılmayacak"},
    "tufe": {"duzeltildi": "Artış yasal sınıra çekildi"},
}


@dataclass
class MarketEstimate:
    median: float
    low: float
    high: float
    count: int
    gap_pct: float | None
    comps: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class PropertyLegal:
    kind: str
    term_months: int
    start: date | None
    delivery: date | None
    cap: L.RentCap
    cap_rent: Decimal | None
    over_cap: bool
    five: L.FiveYear | None
    ten: L.TenYear | None
    commitments: list[dict[str, Any]]
    alerts: list[L.LegalAlert]
    market: MarketEstimate | None


def _d(v: str | None) -> date | None:
    try:
        return date.fromisoformat(v[:10]) if v else None
    except ValueError:
        return None


def prop_kind(p) -> str:
    k = (dict(p).get("property_kind") or "konut").strip()
    return k if k in PROPERTY_KINDS else "konut"


def term_of(p) -> int:
    return int(dict(p).get("lease_term_months") or 12)


# ---------------------------------------------------------------- onaylar
def acked_refs(db) -> set[tuple[int, str, str]]:
    return {(r["property_id"], r["kind"], r["ref"]) for r in db.query("SELECT property_id, kind, ref FROM legal_acks")}


def ack(db, user_id: int, pid: int, kind: str, ref: str, action: str, notes: str = "") -> None:
    db.execute(
        "INSERT INTO legal_acks (property_id, kind, ref, action, notes, user_id, created_at) VALUES (?,?,?,?,?,?,?)"
        " ON CONFLICT(property_id, kind, ref) DO UPDATE SET action=excluded.action, notes=excluded.notes,"
        " user_id=excluded.user_id, created_at=excluded.created_at",
        (pid, kind, ref, action, notes or None, user_id, now_iso()),
    )


def acks_for(db, pid: int) -> list:
    return db.query("SELECT a.*, u.name AS user_name FROM legal_acks a LEFT JOIN users u ON u.id=a.user_id"
                    " WHERE property_id=? ORDER BY created_at DESC", (pid,))


# ---------------------------------------------------------------- tahliye taahhütleri
def commitments_for(db, pid: int) -> list:
    return db.query("SELECT * FROM eviction_commitments WHERE property_id=? ORDER BY eviction_date DESC", (pid,))


def add_commitment(db, user_id: int, p, signed: date, eviction: date, tenant_name: str = "",
                   notes: str = "") -> int:
    if eviction <= signed:
        raise ValueError("Tahliye tarihi, taahhütnamenin imza tarihinden sonra olmalı.")
    cid = db.execute(
        "INSERT INTO eviction_commitments (property_id, tenant_name, signed_date, eviction_date, status, notes,"
        " created_by, created_at) VALUES (?,?,?,?, 'active', ?,?,?)",
        (p["id"], tenant_name or p["tenant_name"], signed.isoformat(), eviction.isoformat(), notes or None, user_id,
         now_iso()),
    )
    db.log(user_id, "taahhut_ekledi", p["id"], f"İmza {R.fmt_date(signed)}, tahliye {R.fmt_date(eviction)}")
    return cid


def update_commitment(db, user_id: int, cid: int, *, status: str | None = None, notice_date: date | None = None,
                      action_date: date | None = None, notes: str | None = None) -> None:
    row = db.one("SELECT * FROM eviction_commitments WHERE id=?", (cid,))
    if not row:
        raise ValueError("Taahhütname bulunamadı.")
    sets, params = [], []
    if status:
        if status not in L.COMMITMENT_STATUSES:
            raise ValueError("Geçersiz durum.")
        sets.append("status=?")
        params.append(status)
    if notice_date:
        if notice_date >= date.fromisoformat(row["eviction_date"]):
            raise ValueError("TBK 353 bildirimi tahliye tarihinden ÖNCE yapılmalı; bu tarihle süre uzamaz.")
        sets.append("notice_date=?")
        params.append(notice_date.isoformat())
    if action_date:
        sets.append("action_date=?")
        params.append(action_date.isoformat())
    if notes:
        sets.append("notes=COALESCE(notes || char(10), '') || ?")
        params.append(notes)
    if sets:
        db.execute(f"UPDATE eviction_commitments SET {', '.join(sets)} WHERE id=?", (*params, cid))
        db.log(user_id, "taahhut_guncelledi", row["property_id"],
               ", ".join(x for x in (status and L.COMMITMENT_STATUSES[status],
                                     notice_date and f"ihtar {R.fmt_date(notice_date)}") if x))


# ---------------------------------------------------------------- emsal kira (rayiç)
def market_estimate(db, p) -> MarketEstimate | None:
    f = F.extract(p["title"], "")
    scored = []
    for it in db.list_sahibinden_listings(listing_type="kiralik", status="all"):
        if it["status"] not in ("active", "rented"):
            continue
        price = it["deal_rent"] if it["status"] == "rented" and it["deal_rent"] else it["price"]
        if not price or price <= 0:
            continue
        g = F.extract(it["title"], it["location"] or "")
        if g.category != f.category:
            continue
        if f.rooms and g.rooms and f.rooms != g.rooms:
            continue
        if f.district and g.district and f.district != g.district:
            continue
        score = (3 if f.rooms and f.rooms == g.rooms else 0) + (2 if f.district and f.district == g.district else 0)
        score += 3 * len(f.hoods & g.hoods) + len(f.amenities & g.amenities)
        scored.append((score, float(price), it))
    if len(scored) < 3:
        return None
    scored.sort(key=lambda x: -x[0])
    top = scored[:8]
    prices = [x[1] for x in top]
    mid = F.median(prices) or 0
    kept = [x for x in top if mid / 3 <= x[1] <= mid * 3]
    prices = sorted(x[1] for x in kept)
    if len(prices) < 3:
        return None
    med = F.median(prices) or 0
    gap = round((med - p["rent"]) / p["rent"] * 100, 1) if p["rent"] else None
    comps = [{"title": it["title"], "price": price, "url": it["url"], "status": it["status"]} for _, price, it in kept[:5]]
    return MarketEstimate(median=med, low=prices[0], high=prices[-1], count=len(prices), gap_pct=gap, comps=comps)


# ---------------------------------------------------------------- mülk bazında hukuki durum
def property_legal(db, p, today: date, rates: dict | None = None, with_market: bool = True,
                   acked: set | None = None) -> PropertyLegal:
    rates = rates if rates is not None else tufe.load_rates(db)
    acked = acked if acked is not None else acked_refs(db)
    kind, term = prop_kind(p), term_of(p)
    start, delivery = _d(dict(p).get("lease_start_date")), _d(dict(p).get("delivery_date"))
    renewal = date.fromisoformat(p["renewal_date"])
    cap = L.rent_cap(renewal, rates, kind)
    cap_rent = cap.max_rent(p["rent"]) if cap.cap_pct is not None else None
    over = bool(p["planned_pct"] is not None and cap.cap_pct is not None and p["planned_pct"] > cap.cap_pct + 0.005)
    alerts: list[L.LegalAlert] = []
    is_active = p["status"] == "active"

    if is_active and over:
        alerts.append(L.LegalAlert(
            "tufe", "critical", "Planlanan artış yasal TÜFE tavanını aşıyor",
            f"Planlanan {R.pct2(p['planned_pct'])} > yasal {R.pct2(cap.cap_pct)} ({cap.label}). "
            f"Azami yeni kira {R.money(float(cap_rent))}; aşan kısım TBK 344 uyarınca geçersizdir.",
            renewal, p["renewal_date"], "TBK 344/1", stage="over"))

    five = ten = None
    if start and is_active:
        five = L.five_year_status(start, today)
        ten = L.ten_year_status(start, term, today)
        for a in (L.five_year_alert(start, today), L.ten_year_alert(start, term, today)):
            if a:
                alerts.append(a)
    elif is_active:
        alerts.append(L.LegalAlert(
            "veri", "info", "Sözleşme başlangıç tarihi eksik",
            "5. yıl kira tespiti ve 10. yıl tahliye hakkı hesaplanamıyor. Mevcut kiracıyla ilk sözleşme tarihini girin.",
            None, "lease_start_date", "TBK 344, 347", stage="missing"))

    commitments = []
    for c in commitments_for(db, p["id"]):
        check = L.check_commitment(
            date.fromisoformat(c["signed_date"]), date.fromisoformat(c["eviction_date"]), today,
            delivery or start, _d(c["notice_date"]), c["status"])
        commitments.append({**dict(c), "check": check})
        if is_active or p["status"] == "vacant":
            a = L.commitment_alert(check, date.fromisoformat(c["eviction_date"]))
            if a:
                a.ref = f"{c['id']}:{a.ref}"
                alerts.append(a)

    alerts = [a for a in alerts if (p["id"], a.kind, a.ref) not in acked]
    market = market_estimate(db, p) if with_market else None
    return PropertyLegal(kind=kind, term_months=term, start=start, delivery=delivery, cap=cap, cap_rent=cap_rent,
                         over_cap=over, five=five, ten=ten, commitments=commitments, alerts=alerts, market=market)


def radar(db, today: date, agent_id: int | None = None) -> list[tuple[Any, L.LegalAlert]]:
    """Ofis (veya danışman) geneli hukuki uyarılar: [(mülk, uyarı)], önceliğe göre sıralı."""
    rates, acked = tufe.load_rates(db), acked_refs(db)
    out = []
    for status in ("active", "vacant"):
        for p in db.list_properties(agent_id, status=status):
            for a in property_legal(db, p, today, rates, with_market=False, acked=acked).alerts:
                out.append((p, a))
    rank = {"critical": 0, "warning": 1, "info": 2}
    out.sort(key=lambda x: (rank.get(x[1].severity, 3), x[1].due or date.max))
    return out


def cpi_missing_since(db, today: date) -> date | None:
    """Beklenen TÜFE ayı (açıklanmasından 1 günden fazla süre geçmesine rağmen) sistemde yoksa açıklanma günü."""
    expected = tufe.expected_latest_period(today)
    if not tufe.needs_refresh(db, today):
        return None
    ann = tufe.announcement_for(expected)
    return ann if (today - ann).days >= 1 else None


# ---------------------------------------------------------------- bildirimler
def alert_message(p, a: L.LegalAlert, agent_name: str, web_url: str = "") -> str:
    icon = {"critical": "🚨", "warning": "⚖️", "info": "📌"}.get(a.severity, "⚖️")
    lines = [
        f"{icon} <b>{texts.esc(a.title)}</b>",
        f"🏠 {texts.esc(p['title'])} (#{p['id']}) — Kiracı: {texts.esc(p['tenant_name'])}",
        f"👔 Sorumlu: {texts.esc(agent_name)}",
        f"📅 {texts.esc(a.detail)}",
        f"📚 Dayanak: {a.law_ref}",
    ]
    if web_url:
        lines.append(f"🔗 {web_url.rstrip('/')}/mulk/{p['id']}")
    return "\n".join(lines)


async def run_legal_alerts(db, messenger=None, today: date | None = None, group_chat_id: str | None = None,
                           cfg: Any = None) -> int:
    """Yeni aşamaya giren hukuki uyarıları bir kez gönderir (danışman + ofis sahibi; fırsatlar sadece sahibe)."""
    from .whatsapp import send_whatsapp_notification

    today = today or date.today()
    owners = db.list_users("owner")
    web_url = getattr(cfg, "web_url", "") if cfg else ""
    wa_target = getattr(cfg, "whatsapp_target_phone", "") if cfg else ""
    sent = 0
    for p, a in radar(db, today):
        if a.kind == "veri":
            continue
        if db.one("SELECT 1 FROM legal_alert_log WHERE property_id=? AND kind=? AND ref=? AND stage=?",
                  (p["id"], a.kind, a.ref, a.stage)):
            continue
        agent = db.get_user(p["agent_id"])
        text = alert_message(p, a, agent["name"] if agent else "?", web_url)
        owner_only = a.severity == "info"  # fırsat/bilgi uyarıları "Diren Doğan'a özel"
        delivered = False
        if messenger:
            recipients = {u["id"]: u for u in owners}
            if agent and agent["active"] and not owner_only:
                recipients[agent["id"]] = agent
            for u in recipients.values():
                if u["channel"] == "telegram":
                    delivered |= await messenger.send(u["chat_id"], text)
            if group_chat_id and not owner_only:
                delivered |= await messenger.send(group_chat_id, text)
        if wa_target and a.severity == "critical":
            plain = texts.strip_html(text)
            delivered = send_whatsapp_notification(wa_target, plain, cfg) or delivered
        if delivered or (messenger is None and not wa_target):
            db.execute("INSERT OR IGNORE INTO legal_alert_log (property_id, kind, ref, stage, sent_at) VALUES (?,?,?,?,?)",
                       (p["id"], a.kind, a.ref, a.stage, now_iso()))
            sent += 1
    return sent


def summary_text(db, today: date, agent_id: int | None = None) -> str:
    items = radar(db, today, agent_id)
    urgent = [(p, a) for p, a in items if a.severity in ("critical", "warning")]
    rates = tufe.load_rates(db)
    latest = L.latest_rate(rates, today)
    lines = ["⚖️ <b>Hukuk Radarı</b>"]
    if latest:
        lines.append(f"📈 TÜFE 12 aylık ort.: <b>{R.pct2(latest.avg12)}</b> ({L.period_label(latest.period)}, "
                     f"{tufe.SOURCE_LABELS.get(latest.source, latest.source)})")
    if not urgent:
        lines.append("✅ Acil hukuki süre yok.")
    for p, a in urgent[:25]:
        flag = "🔴" if a.severity == "critical" else "🟠"
        lines.append(f"{flag} #{p['id']} {texts.esc(p['title'])} — {texts.esc(a.title)} ({L.fmt(a.due)})")
    missing = sum(1 for _, a in items if a.kind == "veri")
    if missing:
        lines.append(f"ℹ️ {missing} mülkte sözleşme başlangıç tarihi eksik (panelden girin).")
    return "\n".join(lines)
