"""Bot ve web panelin ortak iş kuralları. Hatalar ValueError (Türkçe mesajla) fırlatır."""
from __future__ import annotations

from datetime import date

from . import rent as R
from .db import Database


def set_planned_increase(db: Database, user, p, raw: str):
    """'%30' ya da '45000' kabul eder. (pct, yeni_kira) döner."""
    raw = raw.strip()
    if "%" in raw:
        pct = R.parse_pct(raw)
        new = R.apply_pct(p["rent"], pct)
    else:
        new = R.parse_amount(raw)
        pct = R.pct_change(p["rent"], new)
    db.update_property(p["id"], planned_pct=pct, planned_rent=new)
    db.log(user["id"], "zam_girdi", p["id"], f"{R.pct_str(pct)} -> {R.money(new)}")
    return pct, new


def renew(db: Database, user, p, raw_new: str | None):
    """Sözleşmeyi yeniler. (yeni_kira, pct, sonraki_tarih) döner."""
    new = R.parse_amount(raw_new) if raw_new and raw_new.strip() else p["planned_rent"]
    if new is None:
        raise ValueError("Önce bu seneki artışı girin veya yeni kirayı yazın.")
    pct = R.pct_change(p["rent"], new)
    db.add_history(p["id"], p["renewal_date"], p["rent"], new, pct, user["id"])
    next_date = R.add_year(date.fromisoformat(p["renewal_date"]))
    db.update_property(
        p["id"], rent=new, renewal_date=next_date.isoformat(), last_increase_pct=pct,
        last_increase_amount=round(new - p["rent"], 2), planned_pct=None, planned_rent=None)
    db.log(user["id"], "yeniledi", p["id"], f"{R.money(p['rent'])} -> {R.money(new)} ({R.pct_str(pct)})")
    return new, pct, next_date


def create_property(db: Database, user, f: dict) -> int:
    """Web formundan mülk oluşturur."""
    title = (f.get("title") or "").strip()
    if not title:
        raise ValueError("Mülk adı/adresi gerekli.")
    rent = R.parse_amount(f.get("rent", ""))
    renewal = R.parse_date(f.get("renewal_date", ""))
    data = dict(
        agent_id=user["id"], title=title, rent=rent, renewal_date=renewal.isoformat(),
        landlord_name=(f.get("landlord_name") or "").strip() or None,
        landlord_phone=(f.get("landlord_phone") or "").strip() or None,
        tenant_name=(f.get("tenant_name") or "").strip() or None,
        tenant_phone=(f.get("tenant_phone") or "").strip() or None,
        notes=(f.get("notes") or "").strip() or None,
    )
    last = (f.get("last_increase_pct") or "").strip()
    if last:
        pct = R.parse_pct(last)
        data["last_increase_pct"] = pct
        data["last_increase_amount"] = round(rent - rent / (1 + pct / 100), 2)
    pid = db.add_property(**data)
    db.log(user["id"], "mulk_ekledi", pid, title)
    return pid
