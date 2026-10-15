"""Takip Merkezi: bugün ilgilenilmesi gereken her şey tek, önceliklendirilmiş listede.

"Sıfır Olay Kaçırma" ilkesi: kira yenilemeleri, hukuki süreler, müşteri eşleşmeleri ve
sistem uyarıları aynı yerde toplanır. Her modül ``@provider`` ile kendi kalemlerini ekler.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable

from flask import g, url_for

from . import rent as R
from . import texts

log = logging.getLogger(__name__)

SEVERITY_RANK = {"critical": 0, "warning": 1, "match": 2, "info": 3, "ok": 4}
SEVERITY_LABEL = {"critical": "Kritik", "warning": "Yakında", "match": "Fırsat", "info": "Bilgi", "ok": "Tamam"}


@dataclass
class AgendaItem:
    severity: str           # critical | warning | match | info | ok
    kind: str               # renewal | vacant | legal | match | system
    title: str
    subtitle: str = ""
    url: str = "#"
    icon: str = "bell"
    due: date | None = None
    tags: list[tuple[str, str]] = field(default_factory=list)
    actions: list[dict[str, Any]] = field(default_factory=list)
    side: str = ""
    property_id: int | None = None

    @property
    def sort_key(self) -> tuple:
        return (SEVERITY_RANK.get(self.severity, 9), self.due or date.max, self.title)


Provider = Callable[[Any, Any, date], list[AgendaItem]]
_PROVIDERS: list[tuple[str, Provider]] = []


def provider(name: str):
    """Takip Merkezi'ne kalem sağlayan fonksiyonları kaydeder."""
    def deco(fn: Provider) -> Provider:
        if all(n != name for n, _ in _PROVIDERS):
            _PROVIDERS.append((name, fn))
        return fn
    return deco


def build(ctx, user, today: date | None = None) -> list[AgendaItem]:
    today = today or ctx.today()
    items: list[AgendaItem] = []
    for name, fn in _PROVIDERS:
        try:
            items.extend(fn(ctx, user, today))
        except Exception:  # noqa: BLE001 - bir modülün hatası tüm listeyi düşürmemeli
            log.exception("Takip sağlayıcısı hata verdi: %s", name)
    items.sort(key=lambda i: i.sort_key)
    return items


def build_cached(ctx, user) -> list[AgendaItem]:
    """Aynı istek içinde (menü rozeti + sayfa) tek kez hesaplanır."""
    if "_agenda" not in g:
        g._agenda = build(ctx, user)
    return g._agenda


def visible_properties(ctx, user, status: str = "active"):
    return ctx.db.list_properties(ctx.scope_agent(user), status=status)


# ---------------------------------------------------------------- kira yenilemeleri
@provider("renewals")
def renewal_items(ctx, user, today: date) -> list[AgendaItem]:
    from .whatsapp import build_tenant_message, get_whatsapp_chat_url

    db = ctx.db
    acked = db.acked_pairs()
    names = {u["id"]: u["name"] for u in db.list_users(active_only=False)}
    show_agent = ctx.can_view_all(user)
    out: list[AgendaItem] = []
    takip = url_for("takip")
    for p in visible_properties(ctx, user):
        days = texts.days_left(p, today)
        if days > 30:
            continue
        is_acked = (p["id"], p["renewal_date"]) in acked
        url = url_for("property_view", pid=p["id"])
        who = f" · {names.get(p['agent_id'], '?')}" if show_agent else ""
        sub = f"{p['tenant_name'] or 'Kiracı girilmemiş'} · {R.money(p['rent'])} · {R.fmt_date(p['renewal_date'])}{who}"
        tags: list[tuple[str, str]] = []
        if p["planned_rent"] is None:
            tags.append(("Zam girilmedi", "b-warn"))
        else:
            tags.append((f"Plan: {R.pct_str(p['planned_pct'])} → {R.money(p['planned_rent'])}", "b-gold"))
        actions: list[dict[str, Any]] = []
        if not is_acked:
            actions.append({"label": "Aradım", "icon": "phone", "method": "post", "style": "ok",
                            "url": url_for("property_aradim", pid=p["id"]), "fields": {"next": takip}})
            if p["tenant_phone"]:
                msg = build_tenant_message(p["title"], R.fmt_date(p["renewal_date"]), days,
                                           names.get(p["agent_id"], ctx.cfg.brand_name), ctx.cfg.brand_name)
                actions.append({"label": "WhatsApp", "icon": "chat", "style": "wa", "external": True,
                                "url": get_whatsapp_chat_url(p["tenant_phone"], msg)})
        actions.append({"label": "Detay", "icon": "arrow-right", "url": url})

        if days < 0:
            severity = "critical" if not is_acked else "warning"
            title = (f"Kira yenilemesi {-days} gün gecikti: {p['title']}" if not is_acked
                     else f"Yenileme kaydı bekliyor ({-days} gün geçti): {p['title']}")
            side = f"{-days} gün geçti"
        elif not is_acked:
            severity = "critical" if days <= 7 else "warning"
            title = f"Kiracı aranmadı — yenilemeye {days} gün: {p['title']}" if days else f"Yenileme BUGÜN: {p['title']}"
            side = "Bugün" if days == 0 else f"{days} gün"
        elif p["planned_rent"] is None:
            severity = "info"
            title = f"Arandı, zam oranı girilmedi: {p['title']}"
            side = f"{days} gün"
        elif days <= 7:
            severity = "info"
            title = f"Yenileme günü yaklaşıyor — 'Yenilendi' kaydını unutmayın: {p['title']}"
            side = f"{days} gün"
        else:
            continue
        out.append(AgendaItem(severity=severity, kind="renewal", title=title, subtitle=sub, url=url,
                              icon="calendar" if days >= 0 else "alert", due=date.fromisoformat(p["renewal_date"]),
                              tags=tags, actions=actions, side=side, property_id=p["id"]))
    return out


# ---------------------------------------------------------------- boş mülkler
def _client_counts(ctx, user) -> dict[int, int]:
    """Boş dairelere uyan aktif müşteri talebi sayıları (danışman sadece kendi taleplerini sayar)."""
    scope = ctx.scope_agent(user)
    sql = ("SELECT m.target_id AS pid, COUNT(*) AS n FROM demand_matches m JOIN client_demands d ON d.id=m.demand_id"
           " WHERE m.target_type='property' AND m.active=1 AND d.status='active'"
           " AND m.status NOT IN ('dismissed','rejected')")
    params: list[Any] = []
    if scope is not None:
        sql += " AND d.agent_id=?"
        params.append(scope)
    try:
        return {r["pid"]: r["n"] for r in ctx.db.query(sql + " GROUP BY m.target_id", params)}
    except Exception:  # noqa: BLE001 - CRM tabloları yoksa (eski veritabanı) sayı gösterilmez
        return {}


@provider("vacant")
def vacant_items(ctx, user, today: date) -> list[AgendaItem]:
    out: list[AgendaItem] = []
    clients = _client_counts(ctx, user)
    for p in visible_properties(ctx, user, status="vacant"):
        since = ""
        days_empty = None
        if p["exit_date"]:
            try:
                days_empty = (today - date.fromisoformat(p["exit_date"][:10])).days
                since = f" · {days_empty} gündür boş"
            except ValueError:
                since = ""
        url = url_for("property_view", pid=p["id"])
        actions = [{"label": "Yeni kiracı gir", "icon": "user-plus", "url": url + "#yeni-kiraci"}]
        tags: list[tuple[str, str]] = []
        n = clients.get(p["id"], 0)
        if n:
            tags.append((f"🎯 {n} uygun müşteri", "b-gold"))
            actions.insert(0, {"label": "Müşterilere gönder", "icon": "chat", "style": "wa",
                               "url": url_for("property_clients", pid=p["id"])})
        # 30 günü geçen boşluk ofise kira kaybettirir: öne çıkar
        severity = "warning" if (days_empty or 0) >= 30 else ("match" if n else "info")
        out.append(AgendaItem(
            severity=severity, kind="vacant", title=f"Boş mülk — kiracı aranıyor: {p['title']}",
            subtitle=f"Son kira {R.money(p['rent'])}{since}", url=url, icon="door", tags=tags,
            actions=actions, property_id=p["id"], side=f"{days_empty} gün" if days_empty else "",
        ))
    return out
