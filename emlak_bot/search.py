"""Spotlight (Ctrl/⌘ + K) araması.

Kiracı, ev sahibi, telefon numarası, daire adı, Sahibinden ilanı, ekip üyesi ve
komutları tek kutudan bulur. Türkçe karakterler sadeleştirilir ("ayse" → "Ayşe"),
telefonlar rakam bazında eşleşir ("532 111" → 0532 111 22 33). Yetki kuralları
web paneliyle aynıdır: danışman sadece kendi portföyünü görür.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable

from . import rent as R
from . import texts
from .db import normalize_title as norm

GROUP_LIMIT = 6


@dataclass
class Hit:
    title: str
    url: str
    icon: str = "arrow-right"
    subtitle: str = ""
    score: float = 0.0
    badge: str = ""
    badge_class: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"title": self.title, "subtitle": self.subtitle, "url": self.url, "icon": self.icon,
                "badge": self.badge, "badge_class": self.badge_class}


@dataclass
class Group:
    title: str
    hits: list[Hit] = field(default_factory=list)


def digits(s: str | None) -> str:
    return re.sub(r"\D", "", s or "")


def phone_matches(query_digits: str, phone: str | None) -> bool:
    """Sorgudaki rakamlar telefonun içinde geçiyor mu (baştaki 0 / 90 farkı yok sayılır)."""
    if len(query_digits) < 3 or not phone:
        return False
    pd = digits(phone)
    if not pd:
        return False
    q = query_digits.lstrip("0")
    if q.startswith("90") and len(q) > 10:
        q = q[2:]
    return bool(q) and q in pd


def text_score(q: str, *fields: Any) -> float:
    """0–100 arası eşleşme puanı (tam > başlangıç > kelime başı > içerir > tüm kelimeler)."""
    if not q:
        return 0.0
    best = 0.0
    hay_parts = []
    for f in fields:
        if f in (None, ""):
            continue
        n = norm(str(f))
        if not n:
            continue
        hay_parts.append(n)
        if n == q:
            best = max(best, 100)
        elif n.startswith(q):
            best = max(best, 82)
        elif f" {q}" in f" {n}":
            best = max(best, 64)
        elif q in n:
            best = max(best, 40)
    if best == 0 and " " in q:
        hay = " ".join(hay_parts)
        if all(tok in hay for tok in q.split()):
            best = 46
    return best


# ---------------------------------------------------------------- kaynaklar
def _properties(ctx, user, q: str, qd: str, today: date) -> Group:
    db = ctx.db
    scope = ctx.scope_agent(user)
    names = {u["id"]: u["name"] for u in db.list_users(active_only=False)}
    hits: list[Hit] = []
    for status in ("active", "vacant"):
        for p in db.list_properties(scope, status=status):
            score = text_score(q, p["title"], p["tenant_name"], p["landlord_name"])
            note_score = text_score(q, p["notes"]) * 0.5
            score = max(score, note_score)
            if qd and (phone_matches(qd, p["tenant_phone"]) or phone_matches(qd, p["landlord_phone"])):
                score = max(score, 90)
            if score <= 0:
                continue
            days = texts.days_left(p, today)
            if status == "vacant":
                badge, cls = "Boş", "b-violet"
            elif days < 0:
                badge, cls = f"{-days} g gecikti", "b-err"
            elif days <= 30:
                badge, cls = f"{days} g kaldı", "b-warn"
            else:
                badge, cls = "", ""
            sub = " · ".join(x for x in (
                f"Kiracı: {p['tenant_name']}" if p["tenant_name"] else "",
                f"Ev sahibi: {p['landlord_name']}" if p["landlord_name"] else "",
                R.money(p["rent"]),
                names.get(p["agent_id"], "") if scope is None else "",
            ) if x)
            hits.append(Hit(title=p["title"], subtitle=sub, url=f"/mulk/{p['id']}", icon="home",
                            score=score, badge=badge, badge_class=cls))
    hits.sort(key=lambda h: (-h.score, h.title))
    return Group("Mülkler & Kiracılar", hits[:GROUP_LIMIT])


def _listings(ctx, user, q: str, qd: str, today: date) -> Group:
    db = ctx.db
    consultant = None if ctx.can_view_all(user) else user["name"]
    hits: list[Hit] = []
    for it in db.list_sahibinden_listings(consultant=consultant, status="all"):
        score = text_score(q, it["title"], it["location"], it["consultant_name"])
        if qd and len(qd) >= 5 and qd in (it["classified_id"] or ""):
            score = max(score, 95)
        if score <= 0:
            continue
        kind = {"kiralik": "Kiralık", "satilik": "Satılık"}.get(it["listing_type"], it["listing_type"])
        status = {"rented": ("Kiralandı", "b-violet"), "sold": ("Satıldı", "b-violet"),
                  "inactive": ("Pasif", "")}.get(it["status"], ("", ""))
        url = (f"/mulk/{it['property_id']}" if it["property_id"] and it["status"] == "rented"
               else f"/ilanlar?q={it['classified_id']}&durum=all")
        hits.append(Hit(title=it["title"], subtitle=f"{kind} · {it['price_str'] or ''} · {it['location'] or ''} · "
                                                    f"{it['consultant_name']}",
                        url=url, icon="building", score=score * 0.95, badge=status[0], badge_class=status[1]))
    hits.sort(key=lambda h: (-h.score, h.title))
    return Group("Sahibinden İlanları", hits[:GROUP_LIMIT])


def _people(ctx, user, q: str, qd: str, today: date) -> Group:
    if user["role"] != "owner":
        return Group("Ekip")
    hits = []
    for u in ctx.db.list_users(active_only=False):
        score = text_score(q, u["name"], u["username"])
        if score <= 0:
            continue
        role = "Ofis Sahibi" if u["role"] == "owner" else "Danışman"
        hits.append(Hit(title=u["name"], subtitle=f"{role}{'' if u['active'] else ' · pasif'}",
                        url=f"/?agent={u['id']}", icon="user", score=score * 0.9))
    hits.sort(key=lambda h: (-h.score, h.title))
    return Group("Ekip", hits[:GROUP_LIMIT])


def _actions(ctx, user, q: str) -> Group:
    from flask import url_for

    is_owner = user["role"] == "owner"
    hits = []
    for a in ctx.spotlight_actions:
        if a["owner"] and not is_owner:
            continue
        score = text_score(q, a["title"], a["keywords"]) if q else 50
        if score <= 0:
            continue
        hits.append(Hit(title=a["title"], url=url_for(a["endpoint"], **a["params"]), icon=a["icon"],
                        subtitle="Komut", score=score))
    hits.sort(key=lambda h: -h.score)
    return Group("Komutlar & Sayfalar", hits[: (8 if not q else 5)])


def _upcoming(ctx, user, today: date) -> Group:
    hits = []
    for p in ctx.db.list_properties(ctx.scope_agent(user)):
        days = texts.days_left(p, today)
        if days > 30:
            continue
        badge, cls = (f"{-days} g gecikti", "b-err") if days < 0 else (f"{days} g kaldı", "b-warn")
        hits.append(Hit(title=p["title"], subtitle=f"{p['tenant_name'] or '-'} · {R.money(p['rent'])} · "
                                                   f"{R.fmt_date(p['renewal_date'])}",
                        url=f"/mulk/{p['id']}", icon="calendar", score=-days, badge=badge, badge_class=cls))
    hits.sort(key=lambda h: -h.score)
    return Group("Yaklaşan Yenilemeler", hits[:5])


SourceFn = Callable[[Any, Any, str, str, date], Group]
CORE_SOURCES: list[SourceFn] = [_properties, _listings, _people]


def spotlight(ctx, user, raw_query: str, today: date | None = None) -> list[Group]:
    today = today or ctx.today()
    raw_query = (raw_query or "").strip()[:80]
    q = norm(raw_query)
    qd = digits(raw_query) if sum(ch.isdigit() for ch in raw_query) >= max(3, len(raw_query.replace(" ", "")) // 2) else ""
    if not q and not qd:
        return [g for g in (_actions(ctx, user, ""), _upcoming(ctx, user, today)) if g.hits]
    groups = [src(ctx, user, q, qd, today) for src in CORE_SOURCES + list(ctx.spotlight_providers)]
    groups.append(_actions(ctx, user, q))
    groups = [g for g in groups if g.hits]
    groups.sort(key=lambda g: -max(h.score for h in g.hits))
    return groups
