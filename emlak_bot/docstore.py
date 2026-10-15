"""Belge kaydı: numaralandırma, doğrulama kodu, saklama ve mülk verisinden belge üretimi."""
from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from datetime import date, datetime
from typing import Any

from .db import now_turkey
from . import documents as D
from .law import add_months
from .validators import clean_tc, iban_clean

# Belge türüne göre zorunlu alanlar: (sütun, görünen ad)
REQUIRED: dict[str, list[tuple[str, str]]] = {
    "kira_sozlesmesi": [("landlord_name", "Ev sahibi adı"), ("landlord_tc", "Ev sahibi T.C. Kimlik No"),
                        ("tenant_name", "Kiracı adı"), ("tenant_tc", "Kiracı T.C. Kimlik No"),
                        ("address", "Kiralananın açık adresi")],
    "tahliye_taahhutnamesi": [("landlord_name", "Ev sahibi adı"), ("tenant_name", "Kiracı adı"),
                              ("tenant_tc", "Kiracı T.C. Kimlik No"), ("address", "Kiralananın açık adresi"),
                              ("lease_start_date", "Kira sözleşmesi tarihi"), ("delivery_date", "Teslim tarihi")],
    "teslim_tutanagi": [("landlord_name", "Ev sahibi adı"), ("tenant_name", "Kiracı adı"),
                        ("address", "Kiralananın açık adresi")],
}
RECOMMENDED: dict[str, list[tuple[str, str]]] = {
    "kira_sozlesmesi": [("landlord_iban", "Ev sahibi IBAN"), ("landlord_address", "Ev sahibi adresi"),
                        ("tenant_address", "Kiracı adresi"), ("payment_day", "Ödeme günü")],
}


def missing_fields(p, kind: str) -> list[str]:
    row = dict(p)
    return [label for col, label in REQUIRED[kind] if not str(row.get(col) or "").strip()]


def readiness(p) -> dict[str, dict[str, Any]]:
    row = dict(p)
    out = {}
    for kind in D.DOC_KINDS:
        miss = missing_fields(p, kind)
        rec = [label for col, label in RECOMMENDED.get(kind, []) if not str(row.get(col) or "").strip()]
        out[kind] = {"ready": not miss, "missing": miss, "recommended": rec}
    return out


# ---------------------------------------------------------------- kayıt
def _next_doc_no(c: sqlite3.Connection, year: int) -> str:
    row = c.execute("SELECT doc_no FROM documents WHERE doc_no LIKE ? ORDER BY doc_no DESC LIMIT 1",
                    (f"GN-{year}-%",)).fetchone()
    seq = int(row[0].rsplit("-", 1)[1]) + 1 if row else 1
    return f"GN-{year}-{seq:06d}"


def verify_code(secret: str, doc_no: str, kind: str, payload: dict) -> str:
    msg = f"{doc_no}|{kind}|{json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)}".encode()
    digest = hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()[:8].upper()
    return f"{digest[:4]}-{digest[4:]}"


def create(db, *, kind: str, property_id: int | None, payload: dict, render, user_id: int, secret: str,
           meta_kwargs: dict | None = None) -> dict:
    """Belge numarası ayırır, PDF'i üretir ve kaydeder. render(meta) -> bytes."""
    now = now_turkey()
    for _ in range(5):
        with db.connection() as c:
            doc_no = _next_doc_no(c, now.year)
        code = verify_code(secret, doc_no, kind, payload)
        meta = D.DocMeta(doc_no=doc_no, verify_code=code, created=now, **(meta_kwargs or {}))
        pdf = render(meta)
        try:
            doc_id = db.execute(
                "INSERT INTO documents (doc_no, property_id, kind, verify_code, sha256, payload, pdf, created_by, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (doc_no, property_id, kind, code, hashlib.sha256(pdf).hexdigest(),
                 json.dumps(payload, ensure_ascii=False, default=str), pdf, user_id, now.isoformat(timespec="seconds")))
        except sqlite3.IntegrityError:
            continue  # aynı anda başka belge aynı numarayı aldı; yeni numarayla tekrar dene
        db.log(user_id, "belge_olusturdu", property_id, f"{D.DOC_KINDS[kind]} {doc_no}")
        return {"id": doc_id, "doc_no": doc_no, "verify_code": code, "pdf": pdf}
    raise RuntimeError("Belge numarası ayrılamadı, tekrar deneyin.")


def list_for(db, property_id: int) -> list:
    return db.query(
        "SELECT d.id, d.doc_no, d.kind, d.verify_code, d.sha256, d.created_at, length(d.pdf) AS size, u.name AS user_name"
        " FROM documents d LEFT JOIN users u ON u.id=d.created_by WHERE d.property_id=? ORDER BY d.id DESC",
        (property_id,))


def get(db, doc_id: int):
    return db.one("SELECT * FROM documents WHERE id=?", (doc_id,))


def find(db, code_or_no: str):
    q = (code_or_no or "").strip().upper()
    if not q:
        return None
    return db.one(
        "SELECT d.id, d.doc_no, d.kind, d.verify_code, d.sha256, d.created_at, d.property_id, u.name AS user_name"
        " FROM documents d LEFT JOIN users u ON u.id=d.created_by WHERE d.verify_code=? OR d.doc_no=?", (q, q))


def filename(row) -> str:
    base = {"kira_sozlesmesi": "Kira-Sozlesmesi", "tahliye_taahhutnamesi": "Tahliye-Taahhutnamesi",
            "teslim_tutanagi": "Teslim-Tutanagi"}.get(row["kind"], "Belge")
    return f"{base}-{row['doc_no']}.pdf"


# ---------------------------------------------------------------- mülk verisinden belge verisi
def _d(v: str | None) -> date | None:
    try:
        return date.fromisoformat(v[:10]) if v else None
    except ValueError:
        return None


def landlord_of(p) -> D.Party:
    r = dict(p)
    return D.Party(name=r.get("landlord_name") or "", tc=clean_tc(r.get("landlord_tc")), address=r.get("landlord_address") or "",
                   phone=r.get("landlord_phone") or "", iban=iban_clean(r.get("landlord_iban")))


def tenant_of(p) -> D.Party:
    r = dict(p)
    return D.Party(name=r.get("tenant_name") or "", tc=clean_tc(r.get("tenant_tc")), address=r.get("tenant_address") or "",
                   phone=r.get("tenant_phone") or "")


def guarantor_of(p) -> D.Party | None:
    r = dict(p)
    if not (r.get("guarantor_name") or "").strip():
        return None
    return D.Party(name=r["guarantor_name"], tc=clean_tc(r.get("guarantor_tc")), address=r.get("guarantor_address") or "",
                   phone=r.get("guarantor_phone") or "")


def lines(text: str | None) -> list[str]:
    return [ln.strip(" -•\t") for ln in (text or "").splitlines() if ln.strip(" -•\t")]


def lease_from_property(p, *, contract_date: date, start: date | None = None, term_months: int | None = None) -> D.LeaseData:
    r = dict(p)
    kind = r.get("property_kind") if r.get("property_kind") in D.PROPERTY_KIND_LABELS else "konut"
    return D.LeaseData(
        landlord=landlord_of(p), tenant=tenant_of(p), guarantor=guarantor_of(p),
        guarantor_limit=r.get("guarantor_limit"), address=r.get("address") or "", rent=float(r["rent"]),
        start=start or _d(r.get("lease_start_date")) or contract_date,
        term_months=term_months or int(r.get("lease_term_months") or 12), contract_date=contract_date, kind=kind,
        unit_desc=r.get("unit_desc") or "", usage_purpose=r.get("usage_purpose") or ("Mesken" if kind == "konut" else "İşyeri"),
        payment_day=int(r.get("payment_day") or 1), deposit=r.get("deposit_amount"),
        condition=r.get("property_condition") or "Boyalı, temiz ve kullanıma hazır",
        fixtures=lines(r.get("fixtures")), special_terms=lines(r.get("special_terms")),
    )


def eviction_from_property(p, *, signed: date, eviction: date) -> D.EvictionData:
    r = dict(p)
    contract = _d(r.get("lease_start_date"))
    delivery = _d(r.get("delivery_date")) or contract
    if not contract or not delivery:
        raise ValueError("Kira sözleşmesi ve teslim tarihi gerekli.")
    return D.EvictionData(landlord=landlord_of(p), tenant=tenant_of(p), address=r.get("address") or "",
                          contract_date=contract, delivery_date=delivery, eviction_date=eviction, signed_date=signed)


def handover_items(text: str | None) -> list[D.HandoverItem]:
    """Her satır: 'Ad | Adet | Durum' (adet ve durum isteğe bağlı)."""
    items = []
    for ln in lines(text):
        parts = [x.strip() for x in ln.split("|")]
        items.append(D.HandoverItem(name=parts[0], qty=(parts[1] if len(parts) > 1 and parts[1] else "1"),
                                    state=(parts[2] if len(parts) > 2 and parts[2] else "Sağlam")))
    return items


def default_eviction_date(p, today: date) -> date:
    """Varsayılan tahliye tarihi: içinde bulunulan kira döneminin sonu (gelecekteki ilk kira yıldönümü)."""
    renewal = _d(dict(p).get("renewal_date"))
    if renewal and renewal > today:
        return renewal
    start = _d(dict(p).get("lease_start_date"))
    if not start:
        return add_months(today, 12)
    term = int(dict(p).get("lease_term_months") or 12)
    end = add_months(start, term)
    k = 1
    while end <= today:   # ilk süre bittiyse, uzayan yılların bir sonraki bitişi
        end = add_months(start, term + 12 * k)
        k += 1
    return end
