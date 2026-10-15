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


TEXT_FIELDS = ("landlord_name", "landlord_phone", "tenant_name", "tenant_phone", "notes")


def parse_property_form(f, partial: bool = False) -> dict:
    """Mülk formunu doğrular ve veritabanı alanlarına çevirir.

    partial=True (düzenleme): sadece formda gelen alanlar döner.
    """
    def given(key: str) -> bool:
        return not partial or key in f

    out: dict = {}
    if given("title"):
        title = (f.get("title") or "").strip()
        if not title:
            raise ValueError("Mülk adı/adresi gerekli.")
        out["title"] = title
    for key in TEXT_FIELDS:
        if given(key):
            out[key] = (f.get(key) or "").strip() or None
    if given("rent"):
        out["rent"] = R.parse_amount(f.get("rent", ""))
    if given("renewal_date"):
        out["renewal_date"] = R.parse_date(f.get("renewal_date", "")).isoformat()
    if "lease_start_date" in f:
        raw = (f.get("lease_start_date") or "").strip()
        out["lease_start_date"] = R.parse_date(raw).isoformat() if raw else None
        if out["lease_start_date"] and out.get("renewal_date") and out["lease_start_date"] > out["renewal_date"]:
            raise ValueError("Sözleşme başlangıç tarihi yenileme tarihinden sonra olamaz.")
    if "last_increase_pct" in f:
        raw = (f.get("last_increase_pct") or "").strip()
        if raw:
            pct = R.parse_pct(raw)
            out["last_increase_pct"] = pct
            if out.get("rent"):
                out["last_increase_amount"] = round(out["rent"] - out["rent"] / (1 + pct / 100), 2)
        elif partial:
            out["last_increase_pct"] = None
            out["last_increase_amount"] = None
    return out


def create_property(db: Database, user, f: dict) -> int:
    """Web formundan mülk oluşturur."""
    data = parse_property_form(f)
    data["agent_id"] = user["id"]
    if data.get("last_increase_pct") is None:
        data.pop("last_increase_pct", None)
        data.pop("last_increase_amount", None)
    pid = db.add_property(**data)
    db.log(user["id"], "mulk_ekledi", pid, data["title"])
    return pid
