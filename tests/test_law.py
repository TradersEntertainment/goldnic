"""Modül 2: Türk kira hukuku motoru (TÜFE tavanı, tahliye taahhüdü, 5. yıl tespit, 10. yıl tahliye)."""
from __future__ import annotations

import asyncio
import json
import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace as NS
from zoneinfo import ZoneInfo

import pytest
from werkzeug.security import generate_password_hash

from emlak_bot import law as L
from emlak_bot import legal, tufe
from emlak_bot.config import Config
from emlak_bot.db import Database
from emlak_bot.messenger import Messenger
from emlak_bot.scheduler import Scheduler
from emlak_bot.web import create_app

TZ = ZoneInfo("Europe/Istanbul")


def rates_from(table: dict[str, float]) -> dict[str, L.CpiRate]:
    return {p: L.CpiRate(p, v, L.announcement_date(p), "test") for p, v in table.items()}


# ---------------------------------------------------------------- tarih ve TÜFE dönemi
def test_month_arithmetic_handles_month_ends_and_leap_years():
    assert L.add_months(date(2024, 1, 31), 1) == date(2024, 2, 29)
    assert L.add_months(date(2025, 1, 31), 1) == date(2025, 2, 28)
    assert L.add_years(date(2024, 2, 29), 1) == date(2025, 2, 28)
    assert L.add_months(date(2026, 12, 15), 2) == date(2027, 2, 15)


def test_applicable_period_and_tuik_announcement_calendar():
    assert L.applicable_period(date(2026, 10, 1)) == "2026-09"   # Ekim yenilemesi → Eylül verisi
    assert L.applicable_period(date(2027, 1, 20)) == "2026-12"
    assert L.announcement_date("2026-08") == date(2026, 9, 3)    # perşembe
    assert L.announcement_date("2026-09") == date(2026, 10, 5)   # 3 Ekim cumartesi → pazartesi
    assert L.announcement_date("2025-12") == date(2026, 1, 5)    # 3 Ocak cumartesi → pazartesi
    assert tufe.expected_latest_period(date(2026, 11, 2)) == "2026-09"
    assert tufe.expected_latest_period(date(2026, 11, 3)) == "2026-10"


def test_rent_cap_uses_month_before_renewal_and_rounds_to_kurus():
    rates = rates_from({"2026-08": 31.79, "2026-09": 31.49})
    cap = L.rent_cap(date(2026, 10, 20), rates)
    assert cap.period == "2026-09" and cap.cap_pct == 31.49 and not cap.provisional and cap.rule == "tufe"
    assert cap.max_rent(32000) == Decimal("42076.80")
    assert L.max_rent(100.01, 50) == Decimal("150.02")           # yarım kuruş yukarı yuvarlanır
    assert L.max_rent(1234.56, 31.49) == Decimal("1623.32")


def test_rent_cap_is_provisional_until_the_month_is_announced():
    rates = rates_from({"2026-09": 31.49})
    cap = L.rent_cap(date(2026, 11, 2), rates)
    assert cap.period == "2026-10" and cap.provisional and cap.used_period == "2026-09"
    assert cap.cap_pct == 31.49 and cap.expected_on == date(2026, 11, 3)


def test_konut_25_percent_cap_only_between_2022_06_11_and_2024_07_01():
    rates = rates_from({"2023-05": 47.83, "2024-05": 62.51, "2024-06": 65.07, "2022-05": 39.33})
    assert L.rent_cap(date(2023, 6, 15), rates, "konut").cap_pct == 25.0
    assert L.rent_cap(date(2023, 6, 15), rates, "konut").rule == "konut25"
    assert L.rent_cap(date(2023, 6, 15), rates, "isyeri").cap_pct == 47.83     # işyeri kapsam dışı
    assert L.rent_cap(date(2024, 7, 1), rates, "konut").cap_pct == 25.0        # son gün dahil
    assert L.rent_cap(date(2024, 7, 2), rates, "konut").cap_pct == 65.07       # sınır kalktı
    assert L.rent_cap(date(2022, 6, 10), rates, "konut").cap_pct == 39.33      # başlamadan önce


def test_builtin_table_matches_published_bulletins():
    b = tufe.BUILTIN_AVG12
    assert b["2024-05"] == 62.51 and b["2024-06"] == 65.07 and b["2024-12"] == 58.51
    assert b["2025-01"] == 56.35 and b["2025-09"] == 38.36 and b["2025-12"] == 34.88
    assert b["2026-01"] == 33.98 and b["2026-02"] == 33.39 and b["2026-08"] == 31.79 and b["2026-09"] == 31.49


# ---------------------------------------------------------------- 10. yıl (TBK 347)
def test_ten_year_right_counts_contract_term_plus_ten_extension_years():
    t = L.ten_year_status(date(2015, 3, 1), 12, date(2026, 10, 15))
    assert t.initial_end == date(2016, 3, 1)
    assert t.right_from == date(2026, 3, 1)          # 1 yıllık sözleşme + 10 uzama yılı
    assert t.termination == date(2027, 3, 1)         # izleyen uzama yılının sonu
    assert t.notice_deadline == date(2026, 12, 1)    # en az 3 ay önce bildirim
    nxt = L.ten_year_status(date(2015, 3, 1), 12, date(2026, 12, 2))   # süre kaçırıldı → bir sonraki yıl
    assert nxt.termination == date(2028, 3, 1) and nxt.notice_deadline == date(2027, 12, 1)
    multi = L.ten_year_status(date(2014, 1, 1), 36, date(2026, 10, 15))
    assert multi.right_from == date(2027, 1, 1)


def test_ten_year_alert_is_red_three_months_before_the_right_arises():
    start = date(2016, 1, 15)   # hak 15.01.2027'de doğar
    assert L.ten_year_alert(start, 12, date(2026, 6, 1)) is None
    info = L.ten_year_alert(start, 12, date(2026, 9, 1))
    assert info.severity == "info" and info.stage == "180"
    red = L.ten_year_alert(start, 12, date(2026, 11, 1))
    assert red.severity == "critical" and "KIRMIZI ALARM" in red.title and "15.01.2027" in red.detail
    later = L.ten_year_alert(start, 12, date(2027, 2, 1))       # hak doğdu, ihtar son günü 15.10.2027
    assert later.severity == "info" and "15.10.2027" in later.detail
    assert L.ten_year_alert(start, 12, date(2027, 9, 20)).severity == "critical"


# ---------------------------------------------------------------- 5. yıl (TBK 344/3, 345)
def test_five_year_determination_windows():
    start = date(2021, 12, 1)                       # 5. yıl 01.12.2026'da dolar
    assert L.five_year_alert(start, date(2026, 1, 1)) is None
    a = L.five_year_alert(start, date(2026, 6, 15))
    assert a.severity == "info" and a.stage == "180"
    w = L.five_year_alert(start, date(2026, 10, 15))
    assert w.severity == "warning" and "01.11.2026" in w.detail   # davanın son günü: 30 gün önce
    c = L.five_year_alert(start, date(2026, 11, 15))
    assert c.severity == "critical" and "30.11.2026" in c.detail and "01.12.2027" in c.detail
    after = L.five_year_alert(start, date(2027, 1, 10))
    assert after.severity == "info" and after.stage == "open"
    tenth = L.five_year_status(date(2016, 12, 1), date(2026, 10, 15))
    assert tenth.years == 10 and tenth.mark == date(2026, 12, 1)


# ---------------------------------------------------------------- tahliye taahhüdü (TBK 352/1, 353)
def test_commitment_signed_on_delivery_day_is_flagged():
    same_day = L.check_commitment(date(2026, 1, 10), date(2027, 1, 10), date(2026, 3, 1), delivery=date(2026, 1, 10))
    assert any("geçersizlik riski" in i for i in same_day.issues)
    later = L.check_commitment(date(2026, 1, 20), date(2027, 1, 20), date(2026, 3, 1), delivery=date(2026, 1, 10))
    assert later.issues == []
    unknown = L.check_commitment(date(2026, 1, 20), date(2027, 1, 20), date(2026, 3, 1))
    assert any("teslim tarihi" in i for i in unknown.issues)


def test_commitment_deadlines_one_month_or_one_year_with_notice():
    signed, evict = date(2026, 1, 20), date(2027, 1, 20)
    before = L.check_commitment(signed, evict, date(2026, 12, 1), delivery=date(2026, 1, 10))
    assert before.phase == "before" and before.deadline == date(2027, 2, 20)
    alert = L.commitment_alert(before, evict)
    assert alert.severity == "warning" and "ihtar" in alert.detail.lower()
    noticed = L.check_commitment(signed, evict, date(2027, 2, 25), delivery=date(2026, 1, 10), notice=date(2027, 1, 5))
    assert noticed.notice_ok and noticed.deadline == date(2028, 1, 20) and noticed.phase == "window"
    late_notice = L.check_commitment(signed, evict, date(2027, 2, 1), notice=date(2027, 1, 25))
    assert not late_notice.notice_ok and late_notice.deadline == date(2027, 2, 20)
    window = L.check_commitment(signed, evict, date(2027, 2, 1), delivery=date(2026, 1, 10))
    w = L.commitment_alert(window, evict)
    assert w.severity == "critical" and "İİK 272" in w.law_ref and "20.02.2027" in w.detail
    lapsed = L.check_commitment(signed, evict, date(2027, 2, 21), delivery=date(2026, 1, 10))
    assert lapsed.phase == "lapsed" and L.commitment_alert(lapsed, evict).stage == "lapsed"
    closed = L.check_commitment(signed, evict, date(2027, 2, 1), status="executed")
    assert L.commitment_alert(closed, evict) is None


# ---------------------------------------------------------------- TÜFE veri kaynakları
BULLETIN = """
<html><body><div class="bulten"><h1>Tüketici Fiyat Endeksi, Eylül 2026</h1>
<p><b>Yıllık enflasyon %29,73 oldu</b></p>
<p>TÜFE&#39;deki (2025=100) değişim 2026 yılı Eylül ayında bir önceki aya göre %1,84 artış, bir önceki yılın
Aralık ayına göre %24,32 artış, bir önceki yılın aynı ayına göre %29,73 artış ve on iki aylık ortalamalara
göre %31,49 artış olarak gerçekleşti.</p>
<p>Özel kapsamlı TÜFE göstergesi (B) yıllık %29,00, on iki aylık ortalamalara göre %30,80 artış gösterdi.</p>
</div></body></html>
"""


def test_parse_tuik_bulletin_reads_headline_not_b_indicator():
    parsed = tufe.parse_bulletin(BULLETIN)
    assert parsed.period == "2026-09" and parsed.avg12 == 31.49 and parsed.annual == 29.73
    assert tufe.parse_bulletin("<p>alakasız sayfa</p>") is None


def test_find_bulletin_links_newest_first_and_absolute():
    listing = ('<a href="/Bulten/Index?p=Tuketici-Fiyat-Endeksi-Agustos-2026-58301&amp;dil=1">Ağustos</a>'
               '<a href="/Bulten/Index?p=Tuketici-Fiyat-Endeksi-Eylul-2026-58302&amp;dil=1">Eylül</a>'
               '<a href="/Bulten/Index?p=Uretici-Fiyat-Endeksi-Eylul-2026-58303&amp;dil=1">ÜFE</a>')
    links = tufe.find_bulletin_links(listing, "https://veriportali.tuik.gov.tr/Kategori/GetKategori?p=x")
    assert [p for p, _ in links] == ["2026-09", "2026-08"]
    assert links[0][1] == "https://veriportali.tuik.gov.tr/Bulten/Index?p=Tuketici-Fiyat-Endeksi-Eylul-2026-58302&dil=1"


def test_evds_index_to_twelve_month_average():
    idx, level = {}, 100.0
    for i in range(30):
        p = L.shift_period("2024-01", i)
        idx[p] = level
        level *= 1.02
    payload = {"items": [{"Tarih": f"{int(p[:4])}-{int(p[5:])}", "TP_FG_J0": str(v)} for p, v in idx.items()]}
    parsed = tufe.parse_evds(payload, "TP.FG.J0")
    assert parsed == pytest.approx(idx)
    avg = tufe.avg12_from_index(parsed)
    assert min(avg) == "2025-12"                             # 24 aylık veri gerekir
    assert avg["2026-06"] == pytest.approx((1.02 ** 12 - 1) * 100, abs=0.01)


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "law.db"))


def test_builtin_rates_seeded_and_source_priority(db):
    rates = tufe.load_rates(db)
    assert rates["2026-09"].avg12 == 31.49 and rates["2026-09"].source == "builtin"
    assert tufe.upsert_rate(db, "2026-09", 31.50, "manual")
    assert tufe.upsert_rate(db, "2026-09", 31.49, "tuik")
    assert not tufe.upsert_rate(db, "2026-09", 99.0, "manual")       # resmi verinin üzerine elle yazılmaz
    assert tufe.load_rates(db)["2026-09"].source == "tuik"
    Database(db.path)                                                 # yeniden açılış resmi veriyi bozmaz
    assert tufe.load_rates(db)["2026-09"].avg12 == 31.49
    with pytest.raises(ValueError):
        tufe.upsert_rate(db, "2026/10", 30, "manual")
    with pytest.raises(ValueError):
        tufe.upsert_rate(db, "2026-10", 500, "manual")


def fake_http(pages: dict[str, str], fail: set[str] = frozenset()):
    calls = []

    def get(url, headers):
        calls.append((url, headers))
        for key, body in pages.items():
            if key in url:
                if key in fail:
                    return 500, "hata", url
                return 200, body, url
        return 404, "", url
    get.calls = calls
    return get


def test_refresh_from_tuik_stores_new_month_and_falls_back_from_evds(db):
    listing = '<a href="/Bulten/Index?p=Tuketici-Fiyat-Endeksi-Ekim-2026-58400&amp;dil=1">Ekim</a>'
    october = BULLETIN.replace("Eylül", "Ekim").replace("%31,49", "%31,10")
    get = fake_http({"Kategori": listing, "Tuketici-Fiyat-Endeksi-Ekim": october, "evds": "{}"}, fail={"evds"})
    today = date(2026, 11, 3)
    assert tufe.needs_refresh(db, today)
    res = tufe.refresh(db, today, get=get, evds_key="anahtar", tuik_url="https://tuik.test/Kategori/GetKategori")
    assert res.ok and res.source == "tuik" and res.periods == ["2026-10"]
    stored = tufe.load_rates(db)["2026-10"]
    assert stored.avg12 == 31.10 and stored.source == "tuik" and stored.announced_on == date(2026, 11, 3)
    assert not tufe.needs_refresh(db, today)
    assert any(h.get("key") == "anahtar" for _, h in get.calls)      # EVDS anahtarı başlıkta gönderildi
    assert res.message.startswith("TÜİK") and db.get_setting("tufe_last_result").startswith("TÜİK")


def test_refresh_failure_is_reported_not_raised(db):
    res = tufe.refresh(db, date(2026, 11, 4), get=fake_http({}, set()))
    assert not res.ok and "TÜİK" in res.message
    assert "TÜİK" in db.get_setting("tufe_last_result")


# ---------------------------------------------------------------- servis + web
@pytest.fixture
def env(tmp_path):
    database = Database(str(tmp_path / "web.db"))
    owner = database.add_user(1, "Diren Doğan", "owner")
    agent = database.add_user(2, "Burkay Yıldırım", "agent")
    other = database.add_user(3, "Ecenaz Öztekin", "agent")
    database.set_credentials(owner, "diren", generate_password_hash("diren-pass-1"))
    database.set_credentials(agent, "burkay", generate_password_hash("burkay-pass-1"))
    database.set_credentials(other, "ecenaz", generate_password_hash("ecenaz-pass-1"))
    today = date.today()
    # 10. yıl hakkı 60 gün sonra doğacak mülk (kırmızı alarm)
    start = L.add_years(today + timedelta(days=60), -11)
    p10 = database.add_property(agent_id=agent, title="Yahya Kaptan 3+1", rent=30000, tenant_name="Ahmet Bey",
                                renewal_date=(today + timedelta(days=100)).isoformat(), lease_start_date=start.isoformat())
    # tahliye tarihi geçmiş, icra penceresinde taahhüt
    pt = database.add_property(agent_id=other, title="Alikahya 2+1", rent=20000, tenant_name="Ayşe Hanım",
                               renewal_date=(today + timedelta(days=200)).isoformat(),
                               lease_start_date=(today - timedelta(days=400)).isoformat(),
                               delivery_date=(today - timedelta(days=400)).isoformat())
    cid = legal.add_commitment(database, owner, database.get_property(pt), today - timedelta(days=300),
                               today - timedelta(days=5))
    # başlangıç tarihi olmayan mülk
    pm = database.add_property(agent_id=agent, title="Kartepe Villa", rent=50000,
                               renewal_date=(today + timedelta(days=300)).isoformat())
    cfg = Config("", frozenset(), "", TZ, 9, 7, "test-secret", 8000, True, False, "")
    app = create_app(database, cfg)
    app.config["TESTING"] = True
    return app, database, NS(owner=owner, agent=agent, other=other, p10=p10, pt=pt, pm=pm, cid=cid, today=today)


def login(client, user, pw):
    html = client.get("/login").get_data(as_text=True)
    token = re.search(r'name="csrf" value="([^"]+)"', html).group(1)
    client.post("/login", data={"username": user, "password": pw, "csrf": token})
    return token


def csrf_of(client, path):
    return re.search(r'name="csrf" value="([^"]+)"', client.get(path).get_data(as_text=True)).group(1)


def test_radar_lists_ten_year_alarm_commitment_window_and_missing_data(env):
    app, db, ids = env
    items = legal.radar(db, ids.today)
    kinds = {(p["id"], a.kind, a.severity) for p, a in items}
    assert (ids.p10, "tahliye10", "critical") in kinds
    assert (ids.pt, "taahhut", "critical") in kinds
    assert (ids.pm, "veri", "info") in kinds
    assert [p["id"] for p, _ in legal.radar(db, ids.today, ids.agent)] and \
        all(p["agent_id"] == ids.agent for p, _ in legal.radar(db, ids.today, ids.agent))


def test_hukuk_page_takip_and_property_panel_render(env):
    app, db, ids = env
    c = app.test_client()
    login(c, "diren", "diren-pass-1")
    page = c.get("/hukuk").get_data(as_text=True)
    assert "Hukuk Radarı" in page and "KIRMIZI ALARM" in page and "Alikahya 2+1" in page
    assert "Sözleşme başlangıç tarihi eksik" in page
    assert "%31,49" in page   # yerleşik tablodaki son oran (Eylül 2026)
    takip = c.get("/takip").get_data(as_text=True)
    assert "KIRMIZI ALARM" in takip and "1 mülkte sözleşme başlangıç tarihi eksik" in takip
    prop = c.get(f"/mulk/{ids.p10}").get_data(as_text=True)
    assert 'id="hukuk"' in prop and "10. yıl tahliye hakkı" in prop
    calc = c.get("/hukuk?kira=32.000&tarih=2026-10-20&tur=konut").get_data(as_text=True)
    assert "42.077" in calc  # 32.000 × 1,3149 = 42.076,80 ₺
    a = app.test_client()
    login(a, "burkay", "burkay-pass-1")
    mine = a.get("/hukuk").get_data(as_text=True)
    assert "Yahya Kaptan 3+1" in mine and "Alikahya 2+1" not in mine


def test_commitment_web_flow_and_permissions(env):
    app, db, ids = env
    c = app.test_client()
    login(c, "ecenaz", "ecenaz-pass-1")
    token = csrf_of(c, f"/mulk/{ids.pt}")
    r = c.post(f"/taahhut/{ids.cid}", data={"csrf": token, "action": "executed", "date": ids.today.isoformat()})
    assert r.status_code == 302
    assert db.one("SELECT status FROM eviction_commitments WHERE id=?", (ids.cid,))["status"] == "executed"
    assert not [a for p, a in legal.radar(db, ids.today) if a.kind == "taahhut"]
    page = c.post(f"/mulk/{ids.pt}/taahhut", data={
        "csrf": token, "signed_date": (ids.today - timedelta(days=400)).isoformat(),
        "eviction_date": (ids.today + timedelta(days=20)).isoformat()}, follow_redirects=True).get_data(as_text=True)
    assert "geçersizlik riski" in page   # teslim günü imzalanan taahhüt uyarısı
    new_id = db.one("SELECT MAX(id) AS m FROM eviction_commitments")["m"]
    bad = c.post(f"/taahhut/{new_id}", data={"csrf": token, "action": "notice",
                                            "date": (ids.today + timedelta(days=25)).isoformat()}, follow_redirects=True)
    assert "ÖNCE" in bad.get_data(as_text=True)
    ok = c.post(f"/taahhut/{new_id}", data={"csrf": token, "action": "notice", "date": ids.today.isoformat()})
    assert ok.status_code == 302
    assert db.one("SELECT notice_date FROM eviction_commitments WHERE id=?", (new_id,))["notice_date"] == ids.today.isoformat()
    other = app.test_client()
    login(other, "burkay", "burkay-pass-1")
    t2 = csrf_of(other, "/hukuk")
    assert other.post(f"/taahhut/{new_id}", data={"csrf": t2, "action": "waived"}).status_code == 403


def test_lease_details_form_and_ack_hide_alerts(env):
    app, db, ids = env
    c = app.test_client()
    login(c, "burkay", "burkay-pass-1")
    token = csrf_of(c, f"/mulk/{ids.pm}")
    c.post(f"/mulk/{ids.pm}/hukuk", data={"csrf": token, "lease_start_date": "01.12.2021", "lease_term_months": "12",
                                          "property_kind": "isyeri", "delivery_date": "2021-12-01"})
    p = db.get_property(ids.pm)
    assert p["lease_start_date"] == "2021-12-01" and p["property_kind"] == "isyeri" and p["lease_term_months"] == 12
    assert not [a for q, a in legal.radar(db, ids.today) if q["id"] == ids.pm and a.kind == "veri"]
    ref = next(a.ref for q, a in legal.radar(db, ids.today) if q["id"] == ids.p10 and a.kind == "tahliye10")
    c.post(f"/mulk/{ids.p10}/hukuk/onay", data={"csrf": token, "kind": "tahliye10", "ref": ref, "action": "vazgec"})
    assert not [a for q, a in legal.radar(db, ids.today) if q["id"] == ids.p10 and a.kind == "tahliye10"]
    assert c.post(f"/mulk/{ids.p10}/hukuk/onay", data={"csrf": token, "kind": "x", "ref": "y",
                                                      "action": "z"}).status_code == 400


def test_tufe_manual_entry_is_owner_only(env):
    app, db, ids = env
    a = app.test_client()
    login(a, "burkay", "burkay-pass-1")
    assert a.post("/hukuk/tufe", data={"csrf": csrf_of(a, "/hukuk"), "period": "2026-10", "rate": "31,1"}).status_code == 403
    o = app.test_client()
    login(o, "diren", "diren-pass-1")
    o.post("/hukuk/tufe", data={"csrf": csrf_of(o, "/hukuk"), "period": "2026-10", "rate": "31,1"})
    r = tufe.load_rates(db)["2026-10"]
    assert r.avg12 == 31.1 and r.source == "manual"


class FakeMessenger(Messenger):
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text, buttons=None):
        self.sent.append((chat_id, text))
        return True


def test_legal_alerts_sent_once_per_stage_and_opportunities_only_to_owner(env):
    app, db, ids = env
    m = FakeMessenger()
    n = asyncio.run(legal.run_legal_alerts(db, m, ids.today))
    assert n >= 2
    texts_by_chat: dict[str, list[str]] = {}
    for chat, text in m.sent:
        texts_by_chat.setdefault(chat, []).append(text)
    assert any("KIRMIZI ALARM" in t for t in texts_by_chat["2"])     # danışman kendi mülkünün alarmını alır
    assert any("KIRMIZI ALARM" in t for t in texts_by_chat["1"])     # ofis sahibi de alır
    assert not any("Yahya" in t for t in texts_by_chat.get("3", []))
    assert asyncio.run(legal.run_legal_alerts(db, FakeMessenger(), ids.today)) == 0   # aynı aşama tekrar gitmez
    # 5. yıl fırsatı (bilgi) sadece sahibe gider
    db.update_property(ids.pm, lease_start_date=L.add_years(ids.today + timedelta(days=150), -5).isoformat())
    m2 = FakeMessenger()
    asyncio.run(legal.run_legal_alerts(db, m2, ids.today))
    assert {c for c, t in m2.sent if "kira tespiti" in t} == {"1"}


def test_reminder_uses_renewal_specific_cap_with_konut_25(tmp_path):
    from emlak_bot.reminders import run_reminders
    database = Database(str(tmp_path / "r.db"))
    database.add_user(1, "Patron", "owner")
    pid = database.add_property(agent_id=1, title="Eski Daire", rent=10000, renewal_date="2024-03-15")
    m = FakeMessenger()
    asyncio.run(run_reminders(database, m, date(2024, 3, 10)))
    text = m.sent[0][1]
    assert "%25" in text and "12.500 ₺" in text      # %55,91 değil, %25 konut sınırı
    assert asyncio.run(run_reminders(database, FakeMessenger(), date(2024, 3, 11))) == 0  # aynı aşama tekrar gitmez
    shop = database.add_property(agent_id=1, title="Dükkan", rent=10000, renewal_date="2024-03-20",
                                 property_kind="isyeri")
    m2 = FakeMessenger()
    asyncio.run(run_reminders(database, m2, date(2024, 3, 12)))
    shop_text = next(t for _, t in m2.sent if "Dükkan" in t)
    assert "%55,9" in shop_text and "15.591 ₺" in shop_text   # işyerinde %25 sınırı yok: Şubat 2024 TÜFE
    assert shop


def test_market_estimate_uses_similar_listings(env):
    from emlak_bot.scraper import seed_initial_listings
    app, db, ids = env
    seed_initial_listings(db)
    pid = db.add_property(agent_id=ids.owner, title="İzmit Kent Konut 2+1 Ara Kat", rent=15000,
                          renewal_date="2027-01-01")
    est = legal.market_estimate(db, db.get_property(pid))
    assert est and est.count >= 3 and est.median > 15000 and est.gap_pct > 0
    assert all("2+1" in c["title"] for c in est.comps)


# ---------------------------------------------------------------- zamanlayıcı ve Telegram
def test_scheduler_runs_once_per_day_with_catch_up_and_survives_errors(db):
    calls = []
    s = Scheduler(db, TZ)
    s.add("ok", 10, 30, lambda: calls.append("ok"))
    s.add("boom", 10, 30, lambda: 1 / 0)
    day = datetime(2026, 10, 15, 9, 0, tzinfo=TZ)
    assert s.tick(day) == []
    assert s.tick(day.replace(hour=14)) == ["ok"]            # sunucu 10:30'da kapalıydı → telafi
    assert s.tick(day.replace(hour=15)) == []                 # aynı gün ikinci kez çalışmaz
    assert db.get_setting("sched_error_boom")
    assert s.tick(datetime(2026, 10, 16, 7, 31, tzinfo=ZoneInfo("UTC"))) == ["ok"]   # UTC 07:31 = İstanbul 10:31
    assert Scheduler(db, TZ).last_run("ok") == "2026-10-16"   # yeniden başlatmada hatırlanır


def test_tufe_job_announces_new_rate(db, monkeypatch):
    from emlak_bot import jobs
    db.add_user(111, "Diren Doğan", "owner")
    sent = []
    monkeypatch.setattr(jobs, "send_telegram_sync", lambda token, chat, text: sent.append((chat, text)) or True)

    def fake_refresh(database, today, **kw):
        tufe.upsert_rate(database, "2026-10", 31.10, "tuik")
        return tufe.RefreshResult(ok=True, source="tuik", periods=["2026-10"], message="ok")

    monkeypatch.setattr(tufe, "refresh", fake_refresh)
    cfg = NS(tz=ZoneInfo("Europe/Istanbul"), bot_token="T", chat_id="", group_chat_id="", evds_api_key="",
             evds_series="TP.FG.J0")
    monkeypatch.setattr("emlak_bot.jobs.local_today", lambda c: date(2026, 11, 3))
    assert jobs.tufe_job(db, cfg) == ["2026-10"]
    assert sent and sent[0][0] == "111" and "Kasım 2026 yenilemeleri" in sent[0][1] and "%31,1" in sent[0][1]
    assert jobs.tufe_job(db, cfg) == []        # veri tamam → tekrar çekmez


def test_telegram_tufe_and_hukuk_commands(db):
    from emlak_bot import handlers
    owner = db.add_user(10, "Diren Doğan", "owner")
    db.add_user(20, "Burkay", "agent")
    sent = []

    class Msg:
        async def reply_text(self, text, **kw):
            sent.append(text)

    def update(uid):
        return NS(effective_user=NS(id=uid, full_name="X"), effective_chat=NS(type="private"), effective_message=Msg())

    cfg = NS(setup_code="", owner_ids=frozenset(), group_chat_id="", tz=TZ, evds_api_key="", evds_series="TP.FG.J0")
    ctx = NS(application=NS(bot_data={"db": db, "cfg": cfg}), args=["2026-10", "31,10"])
    asyncio.run(handlers.cmd_tufe(update(20), ctx))
    assert "sadece ofis sahibi" in sent[-1]
    asyncio.run(handlers.cmd_tufe(update(10), ctx))
    assert tufe.load_rates(db)["2026-10"].source == "manual" and "Ekim 2026" in sent[-1]
    ctx.args = []
    asyncio.run(handlers.cmd_tufe(update(10), ctx))
    assert "TÜFE 12 aylık ortalama" in sent[-1]
    asyncio.run(handlers.cmd_hukuk(update(10), ctx))
    assert "Hukuk Radarı" in sent[-1]
    assert owner
