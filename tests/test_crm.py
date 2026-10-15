"""Modül 4: Müşteri Talep Havuzu (Matching CRM) ve Performans Panosu."""
from __future__ import annotations

import json
import re
import time
from datetime import date, timedelta
from zoneinfo import ZoneInfo

import pytest
from werkzeug.security import generate_password_hash

from emlak_bot import crm, performance, scraper
from emlak_bot import features as F
from emlak_bot import matching as M
from emlak_bot.config import Config
from emlak_bot.db import Database
from emlak_bot.web import create_app

TZ = ZoneInfo("Europe/Istanbul")
CLAUDE_MD_EXAMPLE = ("Mehmet Bey 0532 111 22 33, 3+1 kiralık arıyor, bütçe 30-35 bin TL, Yahya Kaptan veya Alikahya "
                     "istiyor, acelesi var.")


def cfg(**kw) -> Config:
    base = dict(bot_token="", owner_ids=frozenset(), db_path="", tz=TZ, reminder_hour=9, inactivity_days=7,
                secret_key="test-secret", port=8000, web_enabled=True, cookie_secure=False, web_url="")
    base.update(kw)
    return Config(**base)


def cand(title: str, price: float, location: str = "Kocaeli / İzmit", deal: str = "kiralik", **kw) -> M.Candidate:
    return M.Candidate(type="listing", id=kw.pop("id", 1), title=title, price=price, deal_type=deal,
                       feats=F.extract(title, location), location=location, **kw)


# ---------------------------------------------------------------- serbest metin ayrıştırma
def test_parse_claude_md_example():
    p = M.parse(CLAUDE_MD_EXAMPLE)
    c = p.criteria
    assert p.name == "Mehmet Bey" and p.phone == "0532 111 22 33"
    assert c.deal_type == "kiralik" and c.rooms == {"3+1"} and c.rooms_min is None
    assert (c.budget_min, c.budget_max) == (30000, 35000)
    assert c.hoods == {"yahya kaptan", "alikahya"} and c.all_districts == {"izmit"}
    assert c.urgency == "acil" and c.category == "konut" and not c.kinds
    assert M.summary(c) == "Kiralık · 3+1 · 30.000 ₺ – 35.000 ₺ · Alikahya · Yahya Kaptan · Acil"


@pytest.mark.parametrize("text, lo, hi", [
    ("satılık 2+1, 3,5 milyona kadar", None, 3_500_000),
    ("kiralık daire en fazla 45.000 TL", None, 45_000),
    ("kiralık 25000 tl üzeri", 25_000, None),
    ("stüdyo 15k max", None, 15_000),
    ("yatırımlık arsa 5 milyon civarı", 4_000_000, 5_000_000),
    ("villa 8-12 milyon", 8_000_000, 12_000_000),
    ("2+1 kiralık 20 bine kadar 2026 yılında taşınacak", None, 20_000),
    ("3+1 kira, 2 katlı olmasın", None, None),
])
def test_parse_budget_variants(text, lo, hi):
    c = M.parse(text).criteria
    assert (c.budget_min, c.budget_max) == (lo, hi)


def test_parse_rooms_kinds_amenities_and_deal_inference():
    c = M.parse("en az 3+1 kiralık, Kartepe, eşyasız, bahçeli, acelesi yok").criteria
    assert c.rooms_min == 3 and not c.rooms and c.districts == {"kartepe"}
    assert c.avoid == {"esyali"} and "bahce" in c.amenities and c.urgency == "esnek"
    c = M.parse("Fethiye caddesi dükkan 15-20 bin").criteria
    assert c.kinds == {"dukkan"} and c.category == "isyeri" and c.deal_type == "kiralik"
    c = M.parse("Ayşe Hanım 2+1, bütçe 3 milyon, Kent Konut").criteria   # işlem türü yazılmadı: bütçeden anlaşılır
    assert c.deal_type == "satilik" and c.hoods == {"kent konut"}
    assert M.parse("Zeynep Kaya: 1+1 öğrenci, umuttepe, eşyalı").name == "Zeynep Kaya"
    assert M.parse("Kartepe 3+1 kiralık").name == ""        # yer adı isim sanılmaz
    assert M.parse("2+1 kiralık Kent Konut").criteria.kinds == set()       # site adı ("konut") tür sayılmaz
    assert M.parse("konut arıyor 3+1").criteria.kinds == {"daire"}
    assert M.parse("oda 2 salon 1 değil: 3 oda 1 salon kiralık").criteria.rooms == {"3+1"}


def test_criteria_roundtrip_through_db_fields():
    c = M.parse("satılık villa veya müstakil 4+1, en az 8 milyon, Sapanca, havuzlu, eşyasız").criteria
    back = M.Criteria.from_row(c.to_fields())
    assert back == c


# ---------------------------------------------------------------- puanlama
def test_score_perfect_and_hard_filters():
    c = M.parse(CLAUDE_MD_EXAMPLE).criteria
    r = M.score(c, cand("İZMİT YAHYA KAPTAN'DA ARAKAT 3+1 KİRALIK DAİRE", 32000))
    assert r and r.score >= M.STRONG and r.strong
    assert ("3+1 ✓", "ok") in r.reasons and ("Bütçe içinde ✓", "ok") in r.reasons
    assert ("Yahya Kaptan ✓", "ok") in r.reasons
    assert M.score(c, cand("İZMİT YAHYA KAPTAN 2+1 KİRALIK DAİRE", 32000)) is None          # oda tutmuyor
    assert M.score(c, cand("İZMİT YAHYA KAPTAN 3+1 KİRALIK DAİRE", 40000)) is None          # bütçenin %14 üstü
    assert M.score(c, cand("KARTEPE 3+1 KİRALIK DAİRE", 32000, "Kocaeli / Kartepe")) is None  # başka ilçe
    assert M.score(c, cand("İZMİT YAHYA KAPTAN 3+1 SATILIK DAİRE", 32000, deal="satilik")) is None
    assert M.score(c, cand("İZMİT YAHYA KAPTAN 200 M2 KİRALIK DÜKKAN", 32000)) is None       # konut aranıyor


def test_score_soft_signals():
    c = M.parse(CLAUDE_MD_EXAMPLE).criteria
    over = M.score(c, cand("İZMİT ALİKAHYA 3+1 KİRALIK DAİRE", 37000))                 # %5,7 üstü: pazarlık
    assert over and M.MATCH_MIN <= over.score < 93 and any("üstü" in t for t, _ in over.reasons)
    other_hood = M.score(c, cand("İZMİT GÜNDOĞDU 3+1 KİRALIK DAİRE", 25000))
    assert other_hood and other_hood.score < M.MATCH_MIN                                  # aynı ilçe başka mahalle
    rez = M.parse("kiralık daire 2+1 Tepe Rezidence 45 bin").criteria
    r = M.score(rez, cand("İZMİT TEPE REZIDENCE'DA EŞYALI KİRALIK 2+1 REZİDANS", 42500))
    assert r and r.score >= M.MATCH_MIN                                                    # daire ≈ rezidans
    unfurnished = M.parse("2+1 kiralık Kent Konut eşyasız 30 bin").criteria
    a = M.score(unfurnished, cand("İZMİT KENT KONUT 3 SİTESİ EŞYALI KİRALIK 2+1 DAİRE", 26000))
    b = M.score(unfurnished, cand("İZMİT KENT KONUT 1. ETAP KİRALIK 2+1 DAİRE", 25000))
    assert a and b and b.score > a.score


def test_soon_vacant_candidates_lose_points_for_urgent_clients():
    c = M.parse(CLAUDE_MD_EXAMPLE).criteria
    today = date(2026, 10, 15)
    now = M.score(c, cand("YAHYA KAPTAN 3+1 DAİRE", 32000), today)
    later = M.score(c, cand("YAHYA KAPTAN 3+1 DAİRE", 32000, available_on=today + timedelta(days=70)), today)
    assert now.score > later.score and any("gün sonra boşalıyor" in t for t, _ in later.reasons)


def test_turkish_genitive_and_whatsapp_text():
    assert [crm.tr_genitive(n) for n in ("Mehmet Bey", "Ayşe Hanım", "Ali", "Ahmet Kaya", "Can Öztürk", "Su Yolcu")] == \
        ["Mehmet Bey'in", "Ayşe Hanım'ın", "Ali'nin", "Ahmet Kaya'nın", "Can Öztürk'ün", "Su Yolcu'nun"]
    msg = M.whatsapp_text("Mehmet Bey", cand("İZMİT YAHYA KAPTAN 3+1 DAİRE", 32000, url="https://www.sahibinden.com/ilan/1"),
                          "Diren Doğan", "Gold Nicomedia")
    assert msg.startswith("Merhaba Mehmet Bey,") and "32.000 ₺ / ay" in msg and "https://www.sahibinden.com/ilan/1" in msg


# ---------------------------------------------------------------- veri katmanı ve tetikleyiciler
@pytest.fixture
def env(tmp_path):
    database = Database(str(tmp_path / "crm.db"))
    owner = database.add_user(1, "Diren Doğan", "owner")
    agent = database.add_user(2, "Burkay Yıldırım", "agent")
    other = database.add_user(3, "Ecenaz Öztekin", "agent")
    for uid, user in ((owner, "diren"), (agent, "burkay"), (other, "ecenaz")):
        database.set_credentials(uid, user, generate_password_hash(f"{user}-pass-1"))
    app = create_app(database, cfg())   # 64 Sahibinden ilanını tohumlar ve CRM modülünü kurar
    app.config["TESTING"] = True
    return app, database, dict(owner=owner, agent=agent, other=other, today=date.today())


def login(client, user):
    """Giriş yapar ve oturum yenilendikten sonraki (geçerli) CSRF anahtarını döndürür."""
    html = client.get("/login").get_data(as_text=True)
    token = re.search(r'name="csrf" value="([^"]+)"', html).group(1)
    client.post("/login", data={"username": user, "password": f"{user}-pass-1", "csrf": token})
    return re.search(r'name="csrf-token" content="([^"]+)"', client.get("/").get_data(as_text=True)).group(1)


def add_demand(db, text: str, agent: int, name: str | None = None) -> int:
    p = M.parse(text)
    return crm.create_demand(db, name=name or p.name or "Test Müşteri", phone=p.phone, criteria=p.criteria, notes=text,
                             agent_id=agent, created_by=agent)


def test_demand_matches_seeded_portfolio(env):
    _, db, ids = env
    did = add_demand(db, "Ali Veli 0555 123 45 67, 2+1 kiralık Kent Konut, 20-27 bin", ids["agent"])
    new = crm.refresh(db, ids["today"], demand_ids=[did], silent=True)
    titles = {nm.candidate.title for nm in new}
    assert "İZMİT KENT KONUT 2.ETAP SİTESİ KİRALIK ARAKAT 2+1 DAİRE" in titles
    assert "İZMİT KENT KONUT 1. ETAP ARAKAT KİRALIK 2+1 DAİRE" in titles
    assert all(nm.candidate.deal_type == "kiralik" and nm.result.score >= M.MATCH_MIN for nm in new)
    assert not any("SATILIK" in t for t in titles)
    rows = db.query("SELECT * FROM demand_matches WHERE demand_id=?", (did,))
    assert len(rows) == len(new) and all(r["notified_at"] for r in rows)      # sessiz: ekranda görüldü
    assert crm.refresh(db, ids["today"], demand_ids=[did]) == []               # tekrar çalışınca yeni yok


def test_new_sahibinden_listing_triggers_match_and_notification(env, monkeypatch):
    _, db, ids = env
    sent = []
    monkeypatch.setattr("emlak_bot.messenger.send_telegram_sync",
                        lambda token, chat, text, url_buttons=None: sent.append((chat, text, url_buttons)) or True)
    crm.install(db, cfg(bot_token="T", chat_id="-100500", group_chat_id="-100500", web_url="https://os.example"))
    did = add_demand(db, CLAUDE_MD_EXAMPLE, ids["agent"])
    crm.refresh(db, ids["today"], demand_ids=[did], silent=True)
    res = scraper.sync_listings_data([{"cid": "1299000111", "title": "İZMİT YAHYA KAPTAN'DA ARAKAT 3+1 KİRALIK DAİRE",
                                       "price": 33000, "listing_type": "kiralik", "location": "Kocaeli / İzmit",
                                       "url": "https://www.sahibinden.com/ilan/1299000111",
                                       "consultant_name": "Diren Doğan"}], db)
    assert res["created"] == 1
    lid = db.one("SELECT id FROM sahibinden_listings WHERE classified_id='1299000111'")["id"]
    m = db.one("SELECT * FROM demand_matches WHERE demand_id=? AND target_type='listing' AND target_id=?", (did, lid))
    assert m and m["score"] >= M.STRONG and m["status"] == "new"
    for _ in range(50):                      # bildirim arka plan iş parçacığında gider
        if sent:
            break
        time.sleep(0.05)
    import html as htmllib
    time.sleep(0.2)
    by_chat = {chat: (text, buttons) for chat, text, buttons in sent}
    assert "-100500" in by_chat and "1" in by_chat             # ofis grubu + Telegram'daki ofis sahibi
    assert len(sent) == len(by_chat)                          # her alıcıya tek mesaj
    text, buttons = by_chat["-100500"]
    assert "Bu ilan tam Mehmet Bey'in aradığı kriterde!" in htmllib.unescape(text) and "33.000 ₺" in text
    assert buttons[0][0][1].startswith("https://wa.me/905321112233?text=")
    assert buttons[1][0][1] == f"https://os.example/talep/{did}"
    for _ in range(50):
        if db.one("SELECT notified_at FROM demand_matches WHERE id=?", (m["id"],))["notified_at"]:
            break
        time.sleep(0.05)
    assert db.one("SELECT notified_at FROM demand_matches WHERE id=?", (m["id"],))["notified_at"]


def test_listing_closed_or_rented_deactivates_matches_and_records_deal(env):
    _, db, ids = env
    did = add_demand(db, "2+1 kiralık Kent Konut 20-27 bin", ids["agent"], name="Ali Veli")
    crm.refresh(db, ids["today"], demand_ids=[did], silent=True)
    lid = db.one("SELECT id FROM sahibinden_listings WHERE title='İZMİT KENT KONUT 1. ETAP ARAKAT KİRALIK 2+1 DAİRE'")["id"]
    db.execute("UPDATE sahibinden_listings SET first_seen=? WHERE id=?",
               ((ids["today"] - timedelta(days=12)).isoformat() + "T10:00:00", lid))
    pid = db.mark_listing_rented(lid, ids["agent"], "Kent Konut 2+1", 25000, ids["today"].isoformat(),
                                 (ids["today"] + timedelta(days=365)).isoformat(), tenant_name="Ali Veli")
    m = db.one("SELECT active FROM demand_matches WHERE demand_id=? AND target_id=?", (did, lid))
    assert m["active"] == 0
    deal = db.one("SELECT * FROM deals WHERE listing_id=?", (lid,))
    assert deal["kind"] == "kiralama" and deal["amount"] == 25000 and deal["days_on_market"] == 12
    assert deal["property_id"] == pid and deal["agent_id"] == ids["agent"]


def test_vacant_property_is_matched_and_old_tenant_data_cleared(env):
    app, db, ids = env
    pid = db.add_property(agent_id=ids["agent"], title="Yahya Kaptan Mah. Gül Sk. 3+1 Daire", rent=31000,
                          renewal_date=(ids["today"] + timedelta(days=40)).isoformat(), tenant_name="Eski Kiracı",
                          tenant_tc="10000000146", guarantor_name="Kefil", address="Yahya Kaptan Mah. İzmit")
    db.add_history  # noqa: B018 - mülk hazır
    did = add_demand(db, CLAUDE_MD_EXAMPLE, ids["agent"])
    c = app.test_client()
    token = login(c, "burkay")
    r = c.post(f"/mulk/{pid}/kiraci-cikti", data={"csrf": token, "exit_date": ids["today"].isoformat(),
                                                  "next_step": "vacant"})
    assert r.status_code == 302
    m = db.one("SELECT * FROM demand_matches WHERE demand_id=? AND target_type='property' AND target_id=?", (did, pid))
    assert m and m["score"] >= M.MATCH_MIN
    page = c.get(f"/mulk/{pid}").get_data(as_text=True)
    assert "Bu daireye uygun müşteriler" in page and "Mehmet Bey" in page
    c.post(f"/mulk/{pid}/yeni-kiraci", data={"csrf": token, "tenant_name": "Yeni Kiracı", "tenant_phone": "05321112233",
                                             "rent": "33.000", "start_date": ids["today"].isoformat()})
    p = db.get_property(pid)
    assert p["tenant_name"] == "Yeni Kiracı" and p["tenant_tc"] is None and p["guarantor_name"] is None
    assert db.one("SELECT active FROM demand_matches WHERE id=?", (m["id"],))["active"] == 0
    deal = db.one("SELECT * FROM deals WHERE property_id=? AND listing_id IS NULL", (pid,))
    assert deal["amount"] == 33000 and deal["days_on_market"] == 0 and deal["client_name"] == "Yeni Kiracı"


def test_soon_vacant_property_with_eviction_commitment_is_a_candidate(env):
    _, db, ids = env
    pid = db.add_property(agent_id=ids["agent"], title="Alikahya 3+1 Daire", rent=30000,
                          renewal_date=(ids["today"] + timedelta(days=200)).isoformat(), address="Alikahya, İzmit")
    db.execute("INSERT INTO eviction_commitments (property_id, signed_date, eviction_date, status, created_at)"
               " VALUES (?,?,?, 'active', ?)", (pid, (ids["today"] - timedelta(days=100)).isoformat(),
                                                (ids["today"] + timedelta(days=20)).isoformat(), "2026-01-01"))
    cands = [x for x in crm.candidates(db, ids["today"]) if x.type == "property"]
    assert [(x.id, x.available_on) for x in cands] == [(pid, ids["today"] + timedelta(days=20))]
    did = add_demand(db, CLAUDE_MD_EXAMPLE, ids["agent"])
    new = crm.refresh(db, ids["today"], demand_ids=[did], silent=True)
    assert any(nm.candidate.type == "property" and nm.candidate.id == pid for nm in new)


# ---------------------------------------------------------------- web arayüzü
def test_web_demand_flow_permissions_and_whatsapp(env):
    app, db, ids = env
    c = app.test_client()
    token = login(c, "burkay")
    assert c.get("/talepler").status_code == 200
    r = c.post("/talepler/yeni", data={"csrf": token, "request": "Nurten Hanım 0533 222 33 44 — 2+1 kiralık Kent Konut "
                                                                  "20-27 bin, acil"})
    assert r.status_code == 302
    did = int(r.headers["Location"].rsplit("/", 1)[1])
    d = db.one("SELECT * FROM client_demands WHERE id=?", (did,))
    assert d["name"] == "Nurten Hanım" and d["phone"] == "0533 222 33 44" and d["urgency"] == "acil"
    assert d["agent_id"] == ids["agent"]
    page = c.get(f"/talep/{did}").get_data(as_text=True)
    assert "KENT KONUT" in page and "https://wa.me/905332223344?text=" in page and "data-wa-send" in page
    # eksik ad → form tekrar gösterilir
    bad = c.post("/talepler/yeni", data={"csrf": token, "request": "3+1 kiralık 30 bin"})
    assert bad.status_code == 400 and "Müşteri adını yazın" in bad.get_data(as_text=True)
    # başka danışman göremez, ofis sahibi görür
    c2 = app.test_client()
    login(c2, "ecenaz")
    assert c2.get(f"/talep/{did}").status_code == 403
    assert "Nurten Hanım" not in c2.get("/talepler").get_data(as_text=True)
    c3 = app.test_client()
    login(c3, "diren")
    assert c3.get(f"/talep/{did}").status_code == 200
    # akıllı ayrıştırma API'si
    js = c.get("/api/talepler/ayristir", query_string={"q": CLAUDE_MD_EXAMPLE}).get_json()
    assert js["name"] == "Mehmet Bey" and js["phone"] == "0532 111 22 33"
    assert {"text": "Acil", "icon": "alert", "cls": "b-err"} in js["chips"]


def test_match_status_ajax_note_and_activity(env):
    app, db, ids = env
    did = add_demand(db, "Ali Veli 0555 123 45 67 2+1 kiralık Kent Konut 20-27 bin", ids["agent"])
    crm.refresh(db, ids["today"], demand_ids=[did], silent=True)
    mid = db.one("SELECT id FROM demand_matches WHERE demand_id=? ORDER BY score DESC", (did,))["id"]
    c = app.test_client()
    token = login(c, "burkay")
    r = c.post(f"/eslesme/{mid}/durum?status=sent", data={"csrf": token, "ajax": "1"})
    assert r.get_json() == {"ok": True, "status": "sent", "label": "Gönderildi"}
    m = db.one("SELECT * FROM demand_matches WHERE id=?", (mid,))
    assert m["status"] == "sent" and m["sent_at"] and m["sent_by"] == ids["agent"]
    assert any("WhatsApp'tan gönderildi" in n["text"] for n in crm.notes_for(db, did))
    assert db.one("SELECT 1 FROM activity WHERE action='eslesme_sent' AND property_id=?", (did,))
    c.post(f"/eslesme/{mid}/durum", data={"csrf": token, "status": "viewing", "viewing_at": "2026-10-20T14:30"})
    assert db.one("SELECT viewing_at FROM demand_matches WHERE id=?", (mid,))["viewing_at"] == "2026-10-20T14:30"
    assert c.post(f"/eslesme/{mid}/durum", data={"csrf": token, "status": "uydurma", "ajax": "1"}).status_code == 400
    c2 = app.test_client()
    t2 = login(c2, "ecenaz")
    assert c2.post(f"/eslesme/{mid}/durum", data={"csrf": t2, "status": "dismissed"}).status_code == 403


def test_agenda_spotlight_and_listing_badges(env):
    app, db, ids = env
    did = add_demand(db, "Ali Veli 0555 123 45 67 2+1 kiralık Kent Konut 20-27 bin acil", ids["agent"])
    crm.refresh(db, ids["today"], demand_ids=[did])
    c = app.test_client()
    login(c, "burkay")
    takip = c.get("/takip").get_data(as_text=True)
    assert "Ali Veli&#39;nin aradığı kriterde" in takip or "Ali Veli'nin aradığı kriterde" in takip
    assert "WhatsApp&#39;tan gönder" in takip or "WhatsApp'tan gönder" in takip
    groups = c.get("/api/spotlight", query_string={"q": "ali veli"}).get_json()["groups"]
    assert any(g["title"] == "Müşteri Talepleri" and g["items"][0]["url"] == f"/talep/{did}" for g in groups)
    groups = c.get("/api/spotlight", query_string={"q": "555 123"}).get_json()["groups"]
    assert any(g["title"] == "Müşteri Talepleri" for g in groups)
    c3 = app.test_client()
    login(c3, "diren")
    listings = c3.get("/ilanlar").get_data(as_text=True)
    assert re.search(r'href="/ilanlar/\d+/musteriler">🎯 1 müşteri', listings)
    lid = db.one("SELECT target_id FROM demand_matches WHERE demand_id=? AND target_type='listing'", (did,))["target_id"]
    assert "Ali Veli" in c3.get(f"/ilanlar/{lid}/musteriler").get_data(as_text=True)
    # danışman menüsünde son sekme "Talepler"
    assert 'href="/talepler"' in c.get("/").get_data(as_text=True)


def test_followups_for_urgent_and_stale_demands(env):
    _, db, ids = env
    urgent = add_demand(db, "Acil Bey 3+1 kiralık Kartepe acil", ids["agent"], name="Acil Bey")
    stale = add_demand(db, "Yavaş Bey 2+1 kiralık Başiskele", ids["agent"], name="Yavaş Bey")
    fresh = add_demand(db, "Yeni Bey 1+1 kiralık İzmit", ids["agent"], name="Yeni Bey")
    old = (ids["today"] - timedelta(days=3)).isoformat() + "T09:00:00"
    db.execute("UPDATE client_demands SET last_contact_at=?, created_at=? WHERE id=?", (old, old, urgent))
    older = (ids["today"] - timedelta(days=20)).isoformat() + "T09:00:00"
    db.execute("UPDATE client_demands SET last_contact_at=?, created_at=? WHERE id=?", (older, older, stale))
    fu = {f["demand"]["id"]: f["kind"] for f in crm.followups(db, ids["today"])}
    assert fu == {urgent: "urgent", stale: "stale"} and fresh not in fu
    crm.add_note(db, urgent, ids["agent"], "Aradım, hafta sonu bakacak.")
    assert urgent not in {f["demand"]["id"] for f in crm.followups(db, ids["today"])}


def test_matching_job_sends_pending_notifications(env, monkeypatch):
    _, db, ids = env
    from emlak_bot import jobs

    sent = []
    monkeypatch.setattr("emlak_bot.messenger.send_telegram_sync",
                        lambda token, chat, text, url_buttons=None: sent.append(text) or True)
    did = add_demand(db, "Ali Veli 0555 123 45 67 2+1 kiralık Kent Konut 20-27 bin", ids["agent"])
    crm.refresh(db, ids["today"], demand_ids=[did])          # bildirim bekleyen (notified_at boş) eşleşmeler
    assert jobs.matching_job(db, cfg(bot_token="T", chat_id="42", group_chat_id="42")) == 1
    import html as htmllib
    # ofis sohbeti + Telegram'daki ofis sahibi + talebin danışmanı: talep başına tek özet mesaj
    assert len(sent) == 3 and len(set(sent)) == 1
    assert "Ali Veli'nin aradığı kriterde" in htmllib.unescape(sent[0]) and "eşleşme daha var" in sent[0]
    assert jobs.matching_job(db, cfg(bot_token="T", chat_id="42", group_chat_id="42")) == 0   # tekrar gönderilmez


# ---------------------------------------------------------------- performans panosu
def test_leaderboard_revenue_days_and_ranking(env):
    app, db, ids = env
    today = ids["today"]
    first = today.replace(day=1)
    db.record_deal("kiralama", ids["agent"], 30000, first.isoformat(), listed_on=(first - timedelta(days=10)).isoformat())
    db.record_deal("satis", ids["other"], 4_000_000, first.isoformat(), listed_on=(first - timedelta(days=40)).isoformat())
    db.record_deal("kiralama", ids["other"], 20000, (first - timedelta(days=400)).isoformat())   # dönem dışı
    data = performance.leaderboard(db, cfg(), today, "ay")
    by = {s.name: s for s in data["rows"]}
    assert by["Ecenaz Öztekin"].revenue == 160_000 and by["Ecenaz Öztekin"].rank == 1          # %4 satış
    assert by["Burkay Yıldırım"].revenue == 30_000 and by["Burkay Yıldırım"].avg_days == 10    # 1 aylık kira
    assert data["office"]["deals"] == 2 and data["office"]["avg_days"] == 10
    assert performance.leaderboard(db, cfg(), today, "tum")["office"]["deals"] == 3
    assert performance.leaderboard(db, cfg(commission_sale_pct=2.0), today, "ay")["office"]["revenue"] == 110_000
    c = app.test_client()
    login(c, "burkay")
    page = c.get("/performans").get_data(as_text=True)
    assert "Liderlik Panosu" in page and "30.000 ₺" in page and "160.000 ₺" not in page   # başkasının cirosu gizli
    c2 = app.test_client()
    login(c2, "diren")
    assert "160.000 ₺" in c2.get("/performans?donem=ay").get_data(as_text=True)


def test_backfill_deals_from_existing_rented_listings(tmp_path):
    db = Database(str(tmp_path / "bf.db"))
    uid = db.add_user(1, "Diren Doğan", "owner")
    lid, _ = db.upsert_sahibinden_listing({"cid": "77", "title": "Eski ilan 2+1", "price": 20000,
                                           "consultant_name": "Diren Doğan"})
    db.execute("UPDATE sahibinden_listings SET status='rented', deal_date='2026-05-01', deal_rent=21000,"
               " first_seen='2026-04-01T09:00:00', consultant_id=? WHERE id=?", (uid, lid))
    assert db.backfill_deals() == 1 and db.backfill_deals() == 0
    deal = db.one("SELECT * FROM deals WHERE listing_id=?", (lid,))
    assert deal["amount"] == 21000 and deal["days_on_market"] == 30 and deal["agent_id"] == uid


def test_telegram_url_buttons_payload(monkeypatch):
    from emlak_bot import messenger

    captured = {}

    class Resp:
        status_code = 200
        text = "ok"

    def fake_post(url, json=None, timeout=None):
        captured.update(url=url, json=json)
        return Resp()

    import httpx
    monkeypatch.setattr(httpx, "post", fake_post)
    assert messenger.send_telegram_sync("TOKEN", "42", "<b>x</b>", url_buttons=[[("WA", "https://wa.me/90532")],
                                                                                 [("bad", "javascript:alert(1)")]])
    assert captured["json"]["reply_markup"] == {"inline_keyboard": [[{"text": "WA", "url": "https://wa.me/90532"}]]}
    assert json.dumps(captured["json"], ensure_ascii=False).count("javascript") == 0
