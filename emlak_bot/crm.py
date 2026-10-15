"""Müşteri Talep Havuzu: talepler, eşleşmeler, notlar ve otomatik bildirimler (veri katmanı).

Eşleştirme tetikleyicileri:
  * talep kaydedildi / güncellendi  → o talep tüm portföyle eşleştirilir (sessiz; danışman ekranda görür)
  * Sahibinden senkronu yeni ilan getirdi → tüm aktif talepler yeniden puanlanır, YENİ eşleşmeler bildirilir
  * kiracı çıktı (daire boşaldı)       → o daire için eşleşmeler bildirilir
  * her sabah                          → güvenlik ağı: kaçan tetikleyici olsa bile yeni eşleşmeler bildirilir
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from . import features as F
from . import matching as M
from .db import now_iso
from .whatsapp import get_whatsapp_chat_url

log = logging.getLogger(__name__)

STATUSES = {"active": "Aktif", "paused": "Beklemede", "won": "Kazanıldı", "lost": "Kaybedildi"}
MATCH_STATUSES = {"new": "Yeni", "sent": "Gönderildi", "interested": "İlgileniyor", "viewing": "Gösterim",
                  "rejected": "Beğenmedi", "dismissed": "Uygun değil"}
SOURCES = ["Telefon", "WhatsApp", "Ofis ziyareti", "Sahibinden mesajı", "Instagram", "Referans", "Mevcut müşteri",
           "Diğer"]
STALE_DAYS = 14        # bu kadar gün görüşülmeyen talep için "güncel mi?" hatırlatması
URGENT_CONTACT_DAYS = 2
SOON_VACANT_DAYS = 90  # tahliye taahhüdü olan daireler bu kadar gün önceden adaya dönüşür


# ---------------------------------------------------------------- yardımcılar
def tr_genitive(name: str) -> str:
    """Türkçe ilgi eki: Mehmet Bey'in, Ayşe Hanım'ın, Ali'nin, Kaya'nın, Öztürk'ün."""
    name = (name or "").strip()
    if not name:
        return "müşterinin"
    vowels = "aeıioöuüAEIİOÖUÜ"
    last = next((ch for ch in reversed(name) if ch in vowels), "e").lower()
    suffix = {"a": "ın", "ı": "ın", "e": "in", "i": "in", "o": "un", "u": "un", "ö": "ün", "ü": "ün"}.get(last, "in")
    return f"{name}'{'n' if name[-1] in vowels else ''}{suffix}"


def _scope_sql(scope_agent: int | None, alias: str = "d") -> tuple[str, list]:
    return ("", []) if scope_agent is None else (f" AND {alias}.agent_id=?", [scope_agent])


# ---------------------------------------------------------------- talepler
def create_demand(db, *, name: str, criteria: M.Criteria, agent_id: int, created_by: int, phone: str = "",
                  notes: str = "", source: str = "", move_date: str | None = None) -> int:
    name = (name or "").strip()
    if not name:
        raise ValueError("Müşteri adı gerekli.")
    now = now_iso()
    fields = criteria.to_fields()
    cols = {"name": name, "phone": (phone or "").strip() or None, "notes": (notes or "").strip() or None,
            "source": source or None, "move_date": move_date or None, "status": "active", "agent_id": agent_id,
            "created_by": created_by, "created_at": now, "updated_at": now, "last_contact_at": now, **fields}
    did = db.execute(f"INSERT INTO client_demands ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                     list(cols.values()))
    db.log(created_by, "talep_ekledi", did, f"{name}: {M.summary(criteria)}")
    return did


def update_demand(db, did: int, user_id: int, *, criteria: M.Criteria | None = None, **fields: Any) -> None:
    allowed = {"name", "phone", "notes", "source", "move_date", "agent_id"}
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"Güncellenemez alan: {bad}")
    sets = {k: (v.strip() if isinstance(v, str) else v) or None for k, v in fields.items()}
    if "name" in sets and not sets["name"]:
        raise ValueError("Müşteri adı gerekli.")
    if criteria is not None:
        sets.update(criteria.to_fields())
    sets["updated_at"] = now_iso()
    db.execute(f"UPDATE client_demands SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?", (*sets.values(), did))
    db.log(user_id, "talep_guncelledi", did, M.summary(criteria) if criteria else ", ".join(sorted(fields)))


def get_demand(db, did: int):
    return db.one("SELECT d.*, u.name AS agent_name FROM client_demands d LEFT JOIN users u ON u.id=d.agent_id"
                  " WHERE d.id=?", (did,))


def list_demands(db, *, scope_agent: int | None = None, status: str = "active", q: str = "") -> list[dict]:
    where, params = _scope_sql(scope_agent)
    if status != "all":
        where += " AND d.status=?"
        params.append(status)
    rows = db.query(
        "SELECT d.*, u.name AS agent_name,"
        " (SELECT COUNT(*) FROM demand_matches m WHERE m.demand_id=d.id AND m.active=1"
        "    AND m.status NOT IN ('dismissed','rejected')) AS match_count,"
        " (SELECT COUNT(*) FROM demand_matches m WHERE m.demand_id=d.id AND m.active=1 AND m.status='new') AS new_count,"
        " (SELECT MAX(score) FROM demand_matches m WHERE m.demand_id=d.id AND m.active=1"
        "    AND m.status NOT IN ('dismissed','rejected')) AS best_score"
        f" FROM client_demands d LEFT JOIN users u ON u.id=d.agent_id WHERE 1=1{where}"
        " ORDER BY (d.urgency='acil') DESC, new_count DESC, d.id DESC", params)
    out = [dict(r) for r in rows]
    if q:
        from .search import digits, phone_matches, text_score

        nq, qd = M.norm(q), digits(q)
        out = [r for r in out if text_score(nq, r["name"], r["notes"], r["hoods"], r["districts"]) > 0
               or (len(qd) >= 3 and phone_matches(qd, r["phone"]))]
    return out


def set_status(db, did: int, status: str, user_id: int, note: str = "") -> None:
    if status not in STATUSES:
        raise ValueError("Geçersiz durum.")
    closed = status in ("won", "lost")
    db.execute("UPDATE client_demands SET status=?, closed_at=?, close_note=?, updated_at=? WHERE id=?",
               (status, now_iso() if closed else None, (note or "").strip() or None, now_iso(), did))
    add_note(db, did, user_id, f"Durum: {STATUSES[status]}" + (f" — {note.strip()}" if note.strip() else ""),
             contact=False)
    db.log(user_id, "talep_durum", did, STATUSES[status])


def add_note(db, did: int, user_id: int | None, text: str, contact: bool = True) -> None:
    text = (text or "").strip()
    if not text:
        raise ValueError("Not boş olamaz.")
    now = now_iso()
    db.execute("INSERT INTO demand_notes (demand_id, user_id, text, created_at) VALUES (?,?,?,?)",
               (did, user_id, text[:2000], now))
    if contact:
        db.execute("UPDATE client_demands SET last_contact_at=? WHERE id=?", (now, did))


def notes_for(db, did: int) -> list:
    return db.query("SELECT n.*, u.name AS user_name FROM demand_notes n LEFT JOIN users u ON u.id=n.user_id"
                    " WHERE n.demand_id=? ORDER BY n.id DESC", (did,))


def delete_demand(db, did: int, user_id: int) -> None:
    row = get_demand(db, did)
    db.execute("DELETE FROM client_demands WHERE id=?", (did,))
    db.log(user_id, "talep_sildi", None, row["name"] if row else str(did))


# ---------------------------------------------------------------- adaylar
def _listing_candidate(it) -> M.Candidate:
    return M.Candidate(type="listing", id=it["id"], title=it["title"], price=it["price"],
                       deal_type=it["listing_type"] or "kiralik", feats=F.extract(it["title"], it["location"] or ""),
                       url=it["url"] or "", location=it["location"] or "", image_url=it["image_url"] or "",
                       agent_id=it["consultant_id"])


def _property_candidate(p, today: date, available_on: date | None = None) -> M.Candidate:
    r = dict(p)
    feats = F.extract(f"{r['title']} {r.get('unit_desc') or ''}", r.get("address") or "")
    loc = F.DISTRICTS.get(feats.district or "", "") or (r.get("address") or "")
    if available_on:
        note = f"Boşalacak: {available_on.strftime('%d.%m.%Y')}"
    else:
        note = "Boş daire"
        if r.get("exit_date"):
            try:
                note += f" · {(today - date.fromisoformat(r['exit_date'][:10])).days} gündür"
            except ValueError:
                pass
    return M.Candidate(type="property", id=r["id"], title=r["title"], price=r["rent"], deal_type="kiralik",
                       feats=feats, url=f"/mulk/{r['id']}", location=loc, agent_id=r["agent_id"],
                       available_on=available_on, note=note)


def candidates(db, today: date, target: tuple[str, int] | None = None) -> list[M.Candidate]:
    """Eşleşmeye aday ilanlar ve daireler (aktif ilanlar, boş daireler, 90 gün içinde boşalacaklar)."""
    out: list[M.Candidate] = []
    if target is None or target[0] == "listing":
        rows = db.list_sahibinden_listings(status="active") if target is None else \
            [r for r in [db.get_sahibinden_listing(target[1])] if r and r["status"] == "active"]
        out.extend(_listing_candidate(it) for it in rows)
    if target is None or target[0] == "property":
        on_portal = {r["property_id"] for r in db.query(
            "SELECT property_id FROM sahibinden_listings WHERE status='active' AND property_id IS NOT NULL")}
        pid = target[1] if target else None
        for p in db.list_properties(status="vacant"):
            if p["id"] not in on_portal and (pid is None or p["id"] == pid):
                out.append(_property_candidate(p, today))
        soon = db.query(
            "SELECT p.*, MIN(e.eviction_date) AS ev FROM properties p JOIN eviction_commitments e"
            " ON e.property_id=p.id WHERE p.status='active' AND e.status='active' AND e.eviction_date BETWEEN ? AND ?"
            " GROUP BY p.id", (today.isoformat(), (today + timedelta(days=SOON_VACANT_DAYS)).isoformat()))
        for p in soon:
            if p["id"] not in on_portal and (pid is None or p["id"] == pid):
                out.append(_property_candidate(p, today, date.fromisoformat(p["ev"][:10])))
    return out


def candidate_for(db, target_type: str, target_id: int, today: date) -> M.Candidate | None:
    """Kayıtlı bir eşleşmenin adayını (artık yayında olmasa da) gösterim için yeniden kurar."""
    if target_type == "listing":
        it = db.get_sahibinden_listing(target_id)
        return _listing_candidate(it) if it else None
    p = db.get_property(target_id)
    if not p:
        return None
    ev = db.one("SELECT MIN(eviction_date) AS ev FROM eviction_commitments WHERE property_id=? AND status='active'",
                (target_id,))
    avail = date.fromisoformat(ev["ev"][:10]) if ev and ev["ev"] and p["status"] == "active" else None
    return _property_candidate(p, today, avail)


# ---------------------------------------------------------------- eşleştirme
@dataclass
class NewMatch:
    match_id: int
    demand_id: int
    candidate: M.Candidate
    result: M.MatchResult


def refresh(db, today: date, *, demand_ids: list[int] | None = None, target: tuple[str, int] | None = None,
            silent: bool = False) -> list[NewMatch]:
    """Aktif talepleri adaylarla puanlar; yeni eşleşmeleri kaydeder ve döndürür.

    silent=True: yeni satırlar "bildirildi" sayılır (talep ekranda oluşturuldu, danışman zaten görüyor).
    """
    sql, params = "SELECT * FROM client_demands WHERE status='active'", []
    if demand_ids:
        sql += f" AND id IN ({','.join('?' for _ in demand_ids)})"
        params.extend(demand_ids)
    demands = db.query(sql, params)
    if not demands:
        return []
    cands = candidates(db, today, target)
    now = now_iso()
    new: list[NewMatch] = []
    with db.connection() as c:
        for d in demands:
            crit = M.Criteria.from_row(d)
            seen: set[tuple[str, int]] = set()
            for x in cands:
                r = M.score(crit, x, today)
                if r is None or r.score < M.MATCH_MIN:
                    continue
                seen.add((x.type, x.id))
                reasons = json.dumps(r.reasons, ensure_ascii=False)
                row = c.execute("SELECT id FROM demand_matches WHERE demand_id=? AND target_type=? AND target_id=?",
                                (d["id"], x.type, x.id)).fetchone()
                if row:
                    c.execute("UPDATE demand_matches SET score=?, reasons=?, active=1, updated_at=? WHERE id=?",
                              (r.score, reasons, now, row["id"]))
                else:
                    cur = c.execute(
                        "INSERT INTO demand_matches (demand_id, target_type, target_id, score, reasons, status, active,"
                        " created_at, updated_at, notified_at) VALUES (?,?,?,?,?, 'new', 1, ?,?,?)",
                        (d["id"], x.type, x.id, r.score, reasons, now, now, now if silent else None))
                    new.append(NewMatch(cur.lastrowid, d["id"], x, r))
            # artık uygun olmayan (ilan kalktı, daire kiralandı, ölçüt değişti) eşleşmeleri pasifleştir
            scope = "" if target is None else " AND target_type=? AND target_id=?"
            for row in c.execute(f"SELECT id, target_type, target_id FROM demand_matches WHERE demand_id=?"
                                 f" AND active=1{scope}", (d["id"], *(target or ()))).fetchall():
                if (row["target_type"], row["target_id"]) not in seen:
                    c.execute("UPDATE demand_matches SET active=0, updated_at=? WHERE id=?", (now, row["id"]))
    return new


def deactivate_target(db, target_type: str, target_id: int) -> None:
    db.execute("UPDATE demand_matches SET active=0, updated_at=? WHERE target_type=? AND target_id=? AND active=1",
               (now_iso(), target_type, target_id))


def set_match_status(db, mid: int, status: str, user_id: int, note: str = "", viewing_at: str | None = None) -> None:
    if status not in MATCH_STATUSES:
        raise ValueError("Geçersiz eşleşme durumu.")
    row = db.one("SELECT * FROM demand_matches WHERE id=?", (mid,))
    if not row:
        raise ValueError("Eşleşme bulunamadı.")
    now = now_iso()
    sets = {"status": status, "updated_at": now}
    if status == "sent" and not row["sent_at"]:
        sets.update(sent_at=now, sent_by=user_id)
    if viewing_at:
        sets["viewing_at"] = viewing_at
    if note:
        sets["note"] = note.strip()[:500]
    db.execute(f"UPDATE demand_matches SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?", (*sets.values(), mid))
    if status in ("sent", "interested", "viewing", "rejected"):
        db.execute("UPDATE client_demands SET last_contact_at=? WHERE id=?", (now, row["demand_id"]))
    label = {"sent": "İlan WhatsApp'tan gönderildi", "interested": "Müşteri ilgilendi",
             "viewing": "Gösterim planlandı" + (f" ({viewing_at.replace('T', ' ')})" if viewing_at else ""),
             "rejected": "Müşteri beğenmedi", "dismissed": "Eşleşme uygun bulunmadı", "new": "Eşleşme yeniden açıldı"}
    tgt = _target_title(db, row["target_type"], row["target_id"])
    add_note(db, row["demand_id"], user_id, f"{label[status]}: {tgt}" + (f" — {note.strip()}" if note else ""),
             contact=status in ("sent", "interested", "viewing", "rejected"))
    db.log(user_id, "eslesme_" + status, row["demand_id"], tgt)


def _target_title(db, target_type: str, target_id: int) -> str:
    if target_type == "listing":
        r = db.one("SELECT title FROM sahibinden_listings WHERE id=?", (target_id,))
    else:
        r = db.one("SELECT title FROM properties WHERE id=?", (target_id,))
    return r["title"] if r else f"#{target_id}"


# ---------------------------------------------------------------- görünüm verisi
def _view(db, m, d, cand: M.Candidate | None, *, agent_name: str, brand: str) -> dict:
    reasons = [tuple(x) for x in json.loads(m["reasons"] or "[]")]
    wa = ""
    if cand and d["phone"]:
        wa = get_whatsapp_chat_url(d["phone"], M.whatsapp_text(d["name"], cand, agent_name, brand))
    return {"id": m["id"], "demand_id": m["demand_id"], "score": m["score"], "reasons": reasons, "status": m["status"],
            "status_label": MATCH_STATUSES.get(m["status"], m["status"]), "active": bool(m["active"]),
            "created_at": m["created_at"], "sent_at": m["sent_at"], "viewing_at": m["viewing_at"], "note": m["note"],
            "target_type": m["target_type"], "target_id": m["target_id"], "cand": cand, "wa_url": wa,
            "strong": m["score"] >= M.STRONG, "demand": d}


def matches_for(db, demand, *, today: date, brand: str, include_inactive: bool = False) -> list[dict]:
    sql = "SELECT * FROM demand_matches WHERE demand_id=?"
    if not include_inactive:
        sql += " AND active=1"
    rows = db.query(sql + " ORDER BY (status IN ('dismissed','rejected')), score DESC, id DESC", (demand["id"],))
    agent = db.get_user(demand["agent_id"]) if demand["agent_id"] else None
    out = []
    for m in rows:
        cand = candidate_for(db, m["target_type"], m["target_id"], today)
        if cand is None:
            continue
        out.append(_view(db, m, demand, cand, agent_name=agent["name"] if agent else brand, brand=brand))
    return out


def matches_for_target(db, target_type: str, target_id: int, *, today: date, brand: str,
                       scope_agent: int | None = None) -> list[dict]:
    """Bir ilan / daire için uygun müşteriler (en yüksek puan önce)."""
    where, params = _scope_sql(scope_agent)
    rows = db.query(
        "SELECT m.*, d.name AS d_name FROM demand_matches m JOIN client_demands d ON d.id=m.demand_id"
        f" WHERE m.target_type=? AND m.target_id=? AND m.active=1 AND d.status='active'{where}"
        " ORDER BY (m.status IN ('dismissed','rejected')), m.score DESC", (target_type, target_id, *params))
    cand = candidate_for(db, target_type, target_id, today)
    out = []
    users = {u["id"]: u["name"] for u in db.list_users(active_only=False)}
    for m in rows:
        d = get_demand(db, m["demand_id"])
        out.append(_view(db, m, d, cand, agent_name=users.get(d["agent_id"], brand), brand=brand))
    return out


def listing_match_counts(db, scope_agent: int | None = None) -> dict[int, int]:
    where, params = _scope_sql(scope_agent)
    rows = db.query(
        "SELECT m.target_id AS lid, COUNT(*) AS n FROM demand_matches m JOIN client_demands d ON d.id=m.demand_id"
        " WHERE m.target_type='listing' AND m.active=1 AND m.status NOT IN ('dismissed','rejected')"
        f" AND d.status='active'{where} GROUP BY m.target_id", params)
    return {r["lid"]: r["n"] for r in rows}


def new_match_count(db, scope_agent: int | None = None) -> int:
    where, params = _scope_sql(scope_agent)
    row = db.one("SELECT COUNT(*) AS n FROM demand_matches m JOIN client_demands d ON d.id=m.demand_id"
                 f" WHERE m.active=1 AND m.status='new' AND d.status='active'{where}", params)
    return row["n"] if row else 0


def stats(db, scope_agent: int | None = None, today: date | None = None) -> dict[str, int]:
    where, params = _scope_sql(scope_agent)
    month = (today or date.today()).strftime("%Y-%m")
    one = lambda sql, p=(): (db.one(sql, p) or {"n": 0})["n"]  # noqa: E731
    return {
        "active": one(f"SELECT COUNT(*) AS n FROM client_demands d WHERE status='active'{where}", params),
        "urgent": one(f"SELECT COUNT(*) AS n FROM client_demands d WHERE status='active' AND urgency='acil'{where}",
                      params),
        "new_matches": new_match_count(db, scope_agent),
        "won_month": one(f"SELECT COUNT(*) AS n FROM client_demands d WHERE status='won' AND closed_at LIKE ?{where}",
                         [f"{month}%", *params]),
        "sent_month": one("SELECT COUNT(*) AS n FROM demand_matches m JOIN client_demands d ON d.id=m.demand_id"
                          f" WHERE m.sent_at LIKE ?{where}", [f"{month}%", *params]),
    }


# ---------------------------------------------------------------- bildirim
def notification_text(d, nm: NewMatch, extra: int, web_url: str = "") -> str:
    from . import texts

    x, r = nm.candidate, nm.result
    what = "ilan" if x.type == "listing" else ("daire" if not x.available_on else "boşalacak daire")
    lines = [
        f"🔔 <b>Bu {what} tam {texts.esc(tr_genitive(d['name']))} aradığı kriterde!</b> (%{r.score} uyum)",
        f"🏠 {texts.esc(x.title)}",
    ]
    if x.price:
        lines.append(f"💰 {M.money_short(x.price)}{' / ay' if x.deal_type == 'kiralik' else ''}")
    if x.location:
        lines.append(f"📍 {texts.esc(x.location)}")
    if x.note and x.type == "property":
        lines.append(f"🗝 {texts.esc(x.note)}")
    lines.append(f"🎯 Talep: {texts.esc(M.summary(M.Criteria.from_row(d)))}")
    if d["phone"]:
        lines.append(f"📞 {texts.esc(d['name'])}: {texts.esc(d['phone'])}")
    if extra:
        lines.append(f"➕ {extra} eşleşme daha var.")
    if not d["phone"]:
        lines.append("Müşterinin telefonu kayıtlı değil — talebe ekleyin, tek tıkla WhatsApp'tan gönderin.")
    return "\n".join(lines)


def notify(db, cfg, new: list[NewMatch]) -> int:
    """Yeni eşleşmeleri danışmana ve ofis sahibine Telegram'dan (WhatsApp butonuyla) bildirir."""
    from .jobs import owner_targets
    from .messenger import send_telegram_sync

    pending = [nm for nm in new if nm.result.score >= M.MATCH_MIN]
    if not pending:
        return 0
    by_demand: dict[int, list[NewMatch]] = {}
    for nm in pending:
        by_demand.setdefault(nm.demand_id, []).append(nm)
    sent = 0
    brand = getattr(cfg, "brand_name", "Gold Nicomedia")
    web_url = getattr(cfg, "web_url", "")
    token = getattr(cfg, "bot_token", "")
    for did, items in by_demand.items():
        d = get_demand(db, did)
        if not d or d["status"] != "active":
            continue
        items.sort(key=lambda nm: -nm.result.score)
        top = items[0]
        text = notification_text(d, top, len(items) - 1, web_url)
        buttons: list[list[tuple[str, str]]] = []
        agent = db.get_user(d["agent_id"]) if d["agent_id"] else None
        if d["phone"]:
            msg = M.whatsapp_text(d["name"], top.candidate, agent["name"] if agent else brand, brand)
            buttons.append([("💬 WhatsApp'tan gönder", get_whatsapp_chat_url(d["phone"], msg))])
        if web_url:
            buttons.append([("🎯 Eşleşmeleri aç", f"{web_url}/talep/{did}")])
        delivered = False
        if token:
            targets = owner_targets(db, cfg)
            if agent and agent["channel"] == "telegram" and agent["active"]:
                targets.add(agent["chat_id"])
            for chat in targets:
                delivered |= send_telegram_sync(token, chat, text, url_buttons=buttons)
        if getattr(cfg, "whatsapp_target_phone", "") and (getattr(cfg, "whatsapp_token", "")
                                                          or getattr(cfg, "whatsapp_api_url", "")):
            from .texts import strip_html
            from .whatsapp import send_whatsapp_notification

            delivered |= send_whatsapp_notification(cfg.whatsapp_target_phone, strip_html(text), cfg)
        if delivered or not token:
            ids = [nm.match_id for nm in items]
            db.execute(f"UPDATE demand_matches SET notified_at=? WHERE id IN ({','.join('?' for _ in ids)})",
                       (now_iso(), *ids))
        sent += int(delivered)
    return sent


def refresh_and_notify(db, cfg, today: date, *, target: tuple[str, int] | None = None, background: bool = False
                       ) -> list[NewMatch]:
    """Yeniden eşleştirir; yeni eşleşmeleri bildirir (istenirse arka planda, isteği bekletmeden)."""
    new = refresh(db, today, target=target)
    if new:
        if background:
            threading.Thread(target=_safe_notify, args=(db, cfg, new), name="match-notify", daemon=True).start()
        else:
            _safe_notify(db, cfg, new)
    return new


def _safe_notify(db, cfg, new) -> None:
    try:
        notify(db, cfg, new)
    except Exception:  # noqa: BLE001 - bildirim hatası eşleştirmeyi geri almamalı
        log.exception("Eşleşme bildirimi gönderilemedi")


def install(db, cfg) -> None:
    """Sahibinden senkronundan sonra otomatik eşleştirme kancasını kurar (web paneli ve bot için ortak).

    Yeni / güncellenen / 30 günlük yeniden yayınlanan ilan geldiğinde tüm aktif talepler yeniden
    puanlanır ve yeni eşleşmeler danışmana bildirilir.
    """
    from . import scraper

    def after_sync(db_, res: dict) -> None:
        if res.get("created") or res.get("republished") or res.get("updated"):
            today = datetime.now(getattr(cfg, "tz", None)).date() if getattr(cfg, "tz", None) else date.today()
            refresh_and_notify(db_, cfg, today, background=True)

    scraper.SYNC_HOOKS["matching"] = after_sync


def pending_notifications(db, today: date | None = None) -> list[NewMatch]:
    """Bildirimi henüz gitmemiş (ör. bot kapalıyken oluşmuş) eşleşmeler."""
    rows = db.query("SELECT m.* FROM demand_matches m JOIN client_demands d ON d.id=m.demand_id"
                    " WHERE m.notified_at IS NULL AND m.active=1 AND m.status='new' AND d.status='active'")
    today = today or date.today()
    out = []
    for m in rows:
        cand = candidate_for(db, m["target_type"], m["target_id"], today)
        if cand:
            out.append(NewMatch(m["id"], m["demand_id"], cand,
                                M.MatchResult(m["score"], [tuple(x) for x in json.loads(m["reasons"] or "[]")])))
    return out


def followups(db, today: date, scope_agent: int | None = None) -> list[dict]:
    """Takip gerektiren talepler: acil olup aranmayanlar ve uzun süredir görüşülmeyenler."""
    where, params = _scope_sql(scope_agent)
    out = []
    for d in db.query(f"SELECT d.* FROM client_demands d WHERE d.status='active'{where}", params):
        last = d["last_contact_at"] or d["created_at"]
        try:
            idle = (today - datetime.fromisoformat(last).date()).days
        except ValueError:
            continue
        if d["urgency"] == "acil" and idle >= URGENT_CONTACT_DAYS:
            out.append({"demand": d, "idle": idle, "kind": "urgent"})
        elif idle >= STALE_DAYS:
            out.append({"demand": d, "idle": idle, "kind": "stale"})
    return out
