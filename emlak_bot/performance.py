"""Ofis Performans & Liderlik Panosu.

Kapanan kiralama / satış işlemleri (deals), müşteri talepleri ve takip disiplini danışman bazında
özetlenir. Ciro, Taşınmaz Ticareti Hakkında Yönetmelik üst sınırlarıyla tahmin edilir (kiralamada
1 aylık kira, satışta toplam %4; KDV hariç) — işlemde gerçekleşen bedel girildiyse o kullanılır.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .law import add_months

PERIODS = {"ay": "Bu ay", "3ay": "Son 3 ay", "yil": "Bu yıl", "tum": "Tüm zamanlar"}
MONTHS_TR = ["Oca", "Şub", "Mar", "Nis", "May", "Haz", "Tem", "Ağu", "Eyl", "Eki", "Kas", "Ara"]


def period_start(key: str, today: date) -> date | None:
    first = today.replace(day=1)
    if key == "ay":
        return first
    if key == "3ay":
        return add_months(first, -2)
    if key == "yil":
        return date(today.year, 1, 1)
    return None


def fee_of(deal, cfg: Any) -> float:
    if deal["fee"] is not None:
        return float(deal["fee"])
    amount = float(deal["amount"] or 0)
    if deal["kind"] == "satis":
        return amount * float(getattr(cfg, "commission_sale_pct", 4.0)) / 100
    return amount * float(getattr(cfg, "commission_rent_months", 1.0))


@dataclass
class AgentStat:
    user_id: int
    name: str
    role: str
    active: bool
    rentals: int = 0
    sales: int = 0
    revenue: float = 0.0
    volume: float = 0.0
    days: list[int] = field(default_factory=list)
    portfolio: int = 0
    demands_active: int = 0
    demands_won: int = 0
    matches_sent: int = 0
    calls: int = 0
    rank: int = 0

    @property
    def deals(self) -> int:
        return self.rentals + self.sales

    @property
    def avg_days(self) -> float | None:
        return round(sum(self.days) / len(self.days), 1) if self.days else None


def leaderboard(db, cfg: Any, today: date, period: str = "ay") -> dict[str, Any]:
    period = period if period in PERIODS else "ay"
    start = period_start(period, today)
    since = start.isoformat() if start else "0000-00-00"
    users = {u["id"]: u for u in db.list_users(active_only=False)}
    stats: dict[int, AgentStat] = {}

    def st(uid: int | None) -> AgentStat | None:
        if uid is None or uid not in users:
            return None
        if uid not in stats:
            u = users[uid]
            stats[uid] = AgentStat(uid, u["name"], u["role"], bool(u["active"]))
        return stats[uid]

    for u in users.values():
        if u["active"] and (u["password_hash"] or u["channel"] == "telegram"):
            st(u["id"])   # giriş yapabilen herkes panoda görünsün (0 işlemle de)

    deals = db.query("SELECT * FROM deals WHERE deal_date>=? ORDER BY deal_date", (since,))
    office = {"revenue": 0.0, "volume_rent": 0.0, "volume_sale": 0.0, "rentals": 0, "sales": 0, "days": []}
    for d in deals:
        s = st(d["agent_id"])
        fee = fee_of(d, cfg)
        office["revenue"] += fee
        if d["kind"] == "satis":
            office["sales"] += 1
            office["volume_sale"] += float(d["amount"] or 0)
        else:
            office["rentals"] += 1
            office["volume_rent"] += float(d["amount"] or 0)
            if d["days_on_market"] is not None:
                office["days"].append(d["days_on_market"])
        if s is None:
            continue
        s.revenue += fee
        s.volume += float(d["amount"] or 0)
        if d["kind"] == "satis":
            s.sales += 1
        else:
            s.rentals += 1
            if d["days_on_market"] is not None:
                s.days.append(d["days_on_market"])

    for r in db.query("SELECT consultant_id AS uid, COUNT(*) AS n FROM sahibinden_listings WHERE status='active'"
                      " GROUP BY consultant_id"):
        s = st(r["uid"])
        if s:
            s.portfolio += r["n"]
    for r in db.query("SELECT agent_id AS uid, COUNT(*) AS n FROM properties WHERE status IN ('active','vacant')"
                      " GROUP BY agent_id"):
        s = st(r["uid"])
        if s:
            s.portfolio += r["n"]
    for r in db.query("SELECT agent_id AS uid, SUM(status='active') AS a, SUM(status='won' AND closed_at>=?) AS w"
                      " FROM client_demands GROUP BY agent_id", (since,)):
        s = st(r["uid"])
        if s:
            s.demands_active, s.demands_won = r["a"] or 0, r["w"] or 0
    for r in db.query("SELECT sent_by AS uid, COUNT(*) AS n FROM demand_matches WHERE sent_at>=? GROUP BY sent_by",
                      (since,)):
        s = st(r["uid"])
        if s:
            s.matches_sent = r["n"]
    for r in db.query("SELECT user_id AS uid, COUNT(*) AS n FROM activity WHERE ts>=? AND action IN"
                      " ('aradim','hatirlatma_onayladi','eslesme_sent','eslesme_viewing') GROUP BY user_id", (since,)):
        s = st(r["uid"])
        if s:
            s.calls = r["n"]

    rows = [s for s in stats.values() if s.active or s.deals]
    rows.sort(key=lambda s: (-s.revenue, -s.deals, -s.matches_sent, -s.calls, s.name))
    for i, s in enumerate(rows, 1):
        s.rank = i

    closed = db.one("SELECT SUM(status='won') AS w, SUM(status='lost') AS l FROM client_demands"
                    " WHERE closed_at>=?", (since,))
    won, lost = (closed["w"] or 0, closed["l"] or 0) if closed else (0, 0)

    # son 6 ayın ciro ve işlem sayısı (grafik)
    months = []
    first = today.replace(day=1)
    for k in range(5, -1, -1):
        m0 = add_months(first, -k)
        m1 = add_months(m0, 1)
        ds = db.query("SELECT * FROM deals WHERE deal_date>=? AND deal_date<?", (m0.isoformat(), m1.isoformat()))
        months.append({"label": f"{MONTHS_TR[m0.month - 1]} {str(m0.year)[2:]}", "revenue": sum(fee_of(d, cfg) for d in ds),
                       "count": len(ds), "current": k == 0})
    peak = max((m["revenue"] for m in months), default=0) or 1
    for m in months:
        m["pct"] = round(m["revenue"] / peak * 100)

    days = office.pop("days")
    office["avg_days"] = round(sum(days) / len(days), 1) if days else None
    office["deals"] = office["rentals"] + office["sales"]
    office["conversion"] = round(won / (won + lost) * 100) if won + lost else None
    office["won"], office["lost"] = won, lost
    return {"rows": rows, "office": office, "months": months, "period": period, "period_label": PERIODS[period],
            "start": start, "recent": [dict(d, fee_calc=fee_of(d, cfg)) for d in reversed(deals[-12:])]}
