"""Modül 1: PWA kabuğu, Spotlight, alt menü, Takip Merkezi ve mevcut özelliklerin iyileştirmeleri."""
from __future__ import annotations

import re
import struct
from datetime import date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from werkzeug.security import generate_password_hash

from emlak_bot import rent as R
from emlak_bot.config import Config
from emlak_bot.db import Database
from emlak_bot.scraper import seed_initial_listings
from emlak_bot.web import create_app

PKG = Path(__file__).resolve().parent.parent / "emlak_bot"


def make_cfg(**kw) -> Config:
    base = dict(bot_token="", owner_ids=frozenset(), db_path="", tz=ZoneInfo("Europe/Istanbul"), reminder_hour=9,
                inactivity_days=7, secret_key="test-secret", port=8000, web_enabled=True, cookie_secure=False,
                web_url="", brand_name="Gold Nicomedia")
    base.update(kw)
    return Config(**base)


@pytest.fixture
def env(tmp_path):
    db = Database(str(tmp_path / "pwa.db"))
    owner = db.add_user(1, "Diren Doğan", "owner")
    mehmet = db.add_user(2, "Mehmet Kaya", "agent")
    ayse = db.add_user(3, "Ayşe Demir", "agent")
    db.set_credentials(owner, "diren", generate_password_hash("diren-pass-1"))
    db.set_credentials(mehmet, "mehmet", generate_password_hash("mehmet-pass-1"))
    db.set_credentials(ayse, "ayse", generate_password_hash("ayse-pass-123"))
    t = date.today()
    p1 = db.add_property(agent_id=mehmet, title="Yahya Kaptan 3+1", rent=30000, tenant_name="Ahmet Yılmaz",
                         tenant_phone="0532 111 22 33", renewal_date=(t + timedelta(days=5)).isoformat())
    p2 = db.add_property(agent_id=ayse, title="Alikahya Dubleks", rent=20000, tenant_name="Şükrü Öztürk",
                         landlord_name="Gülşen Çakır", renewal_date=(t - timedelta(days=3)).isoformat())
    p3 = db.add_property(agent_id=ayse, title="Kartepe Villa", rent=75000, renewal_date=(t + timedelta(days=200)).isoformat())
    app = create_app(db, make_cfg())
    app.config["TESTING"] = True
    return app, db, dict(owner=owner, mehmet=mehmet, ayse=ayse, p1=p1, p2=p2, p3=p3)


def csrf_of(client, path="/login") -> str:
    html = client.get(path).get_data(as_text=True)
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def login(client, user, pw):
    return client.post("/login", data={"username": user, "password": pw, "csrf": csrf_of(client)})


def png_size(path: Path) -> tuple[int, int]:
    head = path.read_bytes()[:24]
    assert head[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", head[16:24])


# ---------------------------------------------------------------- PWA
def test_manifest_is_installable(env):
    app, *_ = env
    r = app.test_client().get("/manifest.webmanifest")
    assert r.status_code == 200
    assert r.mimetype == "application/manifest+json"
    m = r.get_json()
    assert m["display"] == "standalone" and m["start_url"].startswith("/") and m["scope"] == "/"
    assert m["theme_color"] and m["background_color"] and m["lang"] == "tr-TR"
    sizes = {(i["sizes"], i["purpose"]) for i in m["icons"]}
    assert ("192x192", "any") in sizes and ("512x512", "any") in sizes and ("512x512", "maskable") in sizes
    assert any(s["url"] == "/takip" for s in m["shortcuts"])


def test_icons_are_real_pngs_with_declared_sizes():
    icons = PKG / "static" / "icons"
    assert png_size(icons / "icon-192.png") == (192, 192)
    assert png_size(icons / "icon-512.png") == (512, 512)
    assert png_size(icons / "maskable-512.png") == (512, 512)
    assert png_size(icons / "apple-touch-icon.png") == (180, 180)
    assert (icons / "icon.svg").read_text().startswith("<svg")


def test_service_worker_scope_and_precache_urls_resolve(env):
    app, *_ = env
    c = app.test_client()
    r = c.get("/sw.js")
    assert r.status_code == 200
    assert r.mimetype == "application/javascript"
    assert r.headers["Service-Worker-Allowed"] == "/"
    assert r.headers["Cache-Control"] == "no-cache"
    body = r.get_data(as_text=True)
    assert app.config["ASSET_VERSION"] in body
    precache = re.search(r"const PRECACHE = (\[.*?\]);", body).group(1)
    urls = re.findall(r'"([^"]+)"', precache)
    assert "/offline" in urls
    for u in urls:  # kurulum (install) adımı tek bir 404'te bile başarısız olur
        assert c.get(u).status_code == 200, u


def test_offline_page_is_public_and_has_no_personal_data(env):
    app, *_ = env
    html = app.test_client().get("/offline").get_data(as_text=True)
    assert "Bağlantı yok" in html and "Ahmet" not in html


def test_shell_has_pwa_meta_bottom_nav_and_spotlight(env):
    app, *_ = env
    c = app.test_client()
    login(c, "diren", "diren-pass-1")
    html = c.get("/").get_data(as_text=True)
    for needle in ('rel="manifest"', 'apple-mobile-web-app-capable', 'viewport-fit=cover', 'class="bottomnav"',
                   'id="spotlight"', 'data-open="quickAdd"', 'data-spotlight', '/static/app.js?v='):
        assert needle in html, needle
    assert "Ekip" in html  # sahibin alt menüsünde ekip sekmesi


def test_templates_have_no_inline_scripts_or_handlers():
    """CSP "script-src 'self'" satır içi kodu engeller; şablonlarda olmamalı."""
    for tpl in (PKG / "templates").glob("*.html"):
        src = tpl.read_text(encoding="utf-8")
        for tag in re.findall(r"<script\b[^>]*>", src):
            assert "src=" in tag, f"{tpl.name}: satır içi <script>"
        assert not re.search(r"\son[a-z]+\s*=", src), f"{tpl.name}: onclick benzeri satır içi olay"


def test_security_and_cache_headers(env):
    app, *_ = env
    c = app.test_client()
    r = c.get("/login")
    csp = r.headers["Content-Security-Policy"]
    script_src = re.search(r"script-src ([^;]+)", csp).group(1)
    assert script_src.strip() == "'self'"
    assert "object-src 'none'" in csp and "frame-ancestors 'none'" in csp
    assert r.headers["Cache-Control"] == "no-store"
    v = app.config["ASSET_VERSION"]
    s = c.get(f"/static/app.css?v={v}")
    assert s.status_code == 200 and "immutable" in s.headers["Cache-Control"]


# ---------------------------------------------------------------- Spotlight
def test_spotlight_requires_login(env):
    app, *_ = env
    r = app.test_client().get("/api/spotlight?q=a")
    assert r.status_code == 401 and r.is_json


def _titles(resp) -> list[str]:
    return [i["title"] for g in resp.get_json()["groups"] for i in g["items"]]


def test_spotlight_respects_agent_visibility(env):
    app, db, ids = env
    c = app.test_client()
    login(c, "mehmet", "mehmet-pass-1")
    assert "Yahya Kaptan 3+1" in _titles(c.get("/api/spotlight?q=yahya"))
    assert _titles(c.get("/api/spotlight?q=alikahya")) == []      # Ayşe'nin mülkü görünmez
    assert _titles(c.get("/api/spotlight?q=sukru")) == []
    assert "Ayşe Demir" not in _titles(c.get("/api/spotlight?q=ayse"))  # ekip araması sadece sahipte


def test_spotlight_turkish_folding_and_phone_digits(env):
    app, db, ids = env
    c = app.test_client()
    login(c, "diren", "diren-pass-1")
    assert "Alikahya Dubleks" in _titles(c.get("/api/spotlight?q=sukru ozturk"))  # Şükrü Öztürk
    assert "Alikahya Dubleks" in _titles(c.get("/api/spotlight?q=GÜLŞEN"))       # ev sahibi
    assert "Yahya Kaptan 3+1" in _titles(c.get("/api/spotlight?q=0532 111"))     # telefonun bir parçası
    assert "Yahya Kaptan 3+1" in _titles(c.get("/api/spotlight?q=5321112233"))
    assert "Ayşe Demir" in _titles(c.get("/api/spotlight?q=ayse"))                # sahip ekibi görür


def test_spotlight_empty_query_offers_actions_and_upcoming(env):
    app, db, ids = env
    c = app.test_client()
    login(c, "mehmet", "mehmet-pass-1")
    groups = {g["title"]: g["items"] for g in c.get("/api/spotlight?q=").get_json()["groups"]}
    assert "Komutlar & Sayfalar" in groups and "Yaklaşan Yenilemeler" in groups
    assert all("Toplu" not in i["title"] for i in groups["Komutlar & Sayfalar"])  # sahibe özel komut gizli
    assert [i["title"] for i in groups["Yaklaşan Yenilemeler"]] == ["Yahya Kaptan 3+1"]
    c2 = app.test_client()
    login(c2, "diren", "diren-pass-1")
    owner_actions = [i["title"] for i in c2.get("/api/spotlight?q=toplu").get_json()["groups"][0]["items"]]
    assert any("Toplu" in t for t in owner_actions)


# ---------------------------------------------------------------- Takip Merkezi
def test_takip_orders_by_severity_and_scopes_agent(env):
    app, db, ids = env
    c = app.test_client()
    login(c, "diren", "diren-pass-1")
    html = c.get("/takip").get_data(as_text=True)
    assert "gecikti" in html and "Alikahya Dubleks" in html and "Yahya Kaptan 3+1" in html
    assert html.index("Alikahya Dubleks") < html.index("Yahya Kaptan 3+1")  # gecikmiş olan önce
    assert "Kartepe Villa" not in html  # 200 gün sonra: henüz takip gerekmez

    m = app.test_client()
    login(m, "mehmet", "mehmet-pass-1")
    mh = m.get("/takip").get_data(as_text=True)
    assert "Yahya Kaptan 3+1" in mh and "Alikahya" not in mh


def test_aradim_from_takip_returns_to_takip_and_clears_alert(env):
    app, db, ids = env
    c = app.test_client()
    login(c, "mehmet", "mehmet-pass-1")
    token = csrf_of(c, "/takip")
    r = c.post(f"/mulk/{ids['p1']}/aradim", data={"csrf": token, "next": "/takip"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/takip")
    assert db.is_reminder_acked(ids["p1"], db.get_property(ids["p1"])["renewal_date"])
    r2 = c.post(f"/mulk/{ids['p1']}/aradim", data={"csrf": token, "next": "https://evil.example/"})
    assert "evil.example" not in r2.headers["Location"]  # açık yönlendirme yok


# ---------------------------------------------------------------- tüm sayfalar render oluyor mu (duman testi)
OWNER_PAGES = ["/", "/?f=soon", "/?f=overdue", "/?f=missing", "/?f=uncalled", "/takip", "/ilanlar",
               "/ilanlar?durum=all&sirala=fiyat_azalan", "/ilanlar?durum=sold", "/ekle", "/hesap", "/aktivite",
               "/ekip", "/toplu-giris"]


def test_every_page_renders_for_owner_and_agent(env):
    app, db, ids = env
    seed_initial_listings(db)
    o = app.test_client()
    login(o, "diren", "diren-pass-1")
    for path in OWNER_PAGES + [f"/mulk/{ids['p1']}", f"/mulk/{ids['p1']}/duzenle", "/aktivite?user=2"]:
        r = o.get(path)
        assert r.status_code == 200, path
    a = app.test_client()
    login(a, "mehmet", "mehmet-pass-1")
    for path in ["/", "/takip", "/ilanlar", "/ekle", "/hesap", f"/mulk/{ids['p1']}", f"/mulk/{ids['p1']}/duzenle"]:
        assert a.get(path).status_code == 200, path
    for path in ["/ekip", "/aktivite", "/toplu-giris", "/disa-aktar/portfoy.csv", f"/mulk/{ids['p2']}"]:
        assert a.get(path).status_code == 403, path


def test_error_pages_are_styled_and_api_errors_are_json(env):
    app, *_ = env
    c = app.test_client()
    login(c, "diren", "diren-pass-1")
    r = c.get("/mulk/99999")
    assert r.status_code == 404 and "Bulunamadı" in r.get_data(as_text=True)
    j = c.get("/api/yok")
    assert j.status_code == 404 and j.is_json


# ---------------------------------------------------------------- mevcut özelliklerin iyileştirmeleri
def test_property_edit_clears_plan_when_rent_changes_and_owner_can_transfer(env):
    app, db, ids = env
    db.update_property(ids["p1"], planned_pct=30, planned_rent=39000)
    c = app.test_client()
    login(c, "mehmet", "mehmet-pass-1")
    token = csrf_of(c, f"/mulk/{ids['p1']}/duzenle")
    r = c.post(f"/mulk/{ids['p1']}/duzenle", data={
        "csrf": token, "title": "Yahya Kaptan 3+1 (güncel)", "rent": "32.500", "renewal_date": "01.12.2026",
        "lease_start_date": "2021-12-01", "tenant_name": "Ahmet Yılmaz", "tenant_phone": "05321112233",
        "landlord_name": "", "landlord_phone": "", "notes": "", "last_increase_pct": "",
    })
    assert r.status_code == 302
    p = db.get_property(ids["p1"])
    assert p["title"] == "Yahya Kaptan 3+1 (güncel)" and p["rent"] == 32500
    assert p["renewal_date"] == "2026-12-01" and p["lease_start_date"] == "2021-12-01"
    assert p["planned_rent"] is None and p["planned_pct"] is None
    assert c.post(f"/mulk/{ids['p1']}/devret", data={"csrf": token, "to_user": ids["ayse"]}).status_code == 403

    o = app.test_client()
    login(o, "diren", "diren-pass-1")
    tok = csrf_of(o, f"/mulk/{ids['p1']}")
    assert o.post(f"/mulk/{ids['p1']}/devret", data={"csrf": tok, "to_user": ids["ayse"]}).status_code == 302
    assert db.get_property(ids["p1"])["agent_id"] == ids["ayse"]


def test_csv_export_for_owner(env):
    app, db, ids = env
    c = app.test_client()
    login(c, "diren", "diren-pass-1")
    r = c.get("/disa-aktar/portfoy.csv")
    body = r.get_data(as_text=True)
    assert r.status_code == 200 and body.startswith("﻿") and "Alikahya Dubleks" in body
    assert "attachment" in r.headers["Content-Disposition"]


def test_rented_and_sold_flows_accept_turkish_amounts(env):
    app, db, ids = env
    seed_initial_listings(db)
    kiralik = db.list_sahibinden_listings(listing_type="kiralik")[0]
    satilik = db.list_sahibinden_listings(listing_type="satilik")[0]
    c = app.test_client()
    login(c, "diren", "diren-pass-1")
    tok = csrf_of(c, "/ilanlar")
    r = c.post(f"/ilanlar/{kiralik['id']}/kiralandi", data={
        "csrf": tok, "rent": "30.000", "start_date": "2026-10-20", "renewal_date": "", "tenant_name": "Kemal Bey"})
    assert r.status_code == 302
    prop = db.get_property(db.get_sahibinden_listing(kiralik["id"])["property_id"])
    assert prop["rent"] == 30000 and prop["lease_start_date"] == "2026-10-20"
    assert prop["renewal_date"] == "2027-10-20"  # boş bırakılınca başlangıç + 1 yıl
    bad = c.post(f"/ilanlar/{kiralik['id']}/kiralandi", data={"csrf": tok, "rent": "abc", "tenant_name": "X"})
    assert bad.status_code == 302  # 500 yerine anlaşılır hata mesajı
    r = c.post(f"/ilanlar/{satilik['id']}/satildi", data={
        "csrf": tok, "price": "3.350.000", "deal_date": "2026-10-14", "buyer_name": "Selin Hanım"})
    assert r.status_code == 302
    sold = db.get_sahibinden_listing(satilik["id"])
    assert sold["status"] == "sold" and sold["deal_rent"] == 3350000 and sold["deal_tenant_name"] == "Selin Hanım"
    assert db.get_sahibinden_stats()["sold"] == 1


def test_bulk_import_does_not_duplicate_on_second_paste(env):
    app, db, ids = env
    c = app.test_client()
    login(c, "diren", "diren-pass-1")
    payload = ("Daire / Başlık\tKira\tYenileme Tarihi\tKiracı\tSözleşme Başlangıç\n"
               "Daire 101\t28000\t15.11.2026\tKiracı 1\t15.11.2020\n"
               "Daire 102\t35.000 TL\t01.12.2026\tKiracı 2\t\n")
    tok = csrf_of(c, "/toplu-giris")
    c.post("/toplu-giris", data={"csrf": tok, "mode": "paste", "paste_data": payload, "agent_id": ids["owner"]})
    page = c.post("/toplu-giris", data={"csrf": tok, "mode": "paste", "paste_data": payload,
                                       "agent_id": ids["owner"]}, follow_redirects=True).get_data(as_text=True)
    titles = [p["title"] for p in db.list_properties()]
    assert titles.count("Daire 101") == 1 and titles.count("Daire 102") == 1
    assert "Zaten kayıtlı" in page and "<br>•" in page  # hata listesi HTML olarak (kaçışsız) gösterilir
    d101 = next(p for p in db.list_properties() if p["title"] == "Daire 101")
    assert d101["lease_start_date"] == "2020-11-15"


def test_phantom_consultant_user_is_merged_into_owner(tmp_path):
    db = Database(str(tmp_path / "merge.db"))
    seed_initial_listings(db)  # "Diren Doğan" adına şifresiz danışman kaydı açar
    phantom = db.one("SELECT id FROM users WHERE name='Diren Doğan'")["id"]
    pid = db.add_property(agent_id=phantom, title="Hayalet mülk", rent=1, renewal_date="2027-01-01")
    owner = db.seed_admin("direndogann", "Sifre12345!")
    assert db.one("SELECT COUNT(*) AS n FROM users WHERE name='Diren Doğan'")["n"] == 1
    assert db.get_property(pid)["agent_id"] == owner
    mine = db.list_sahibinden_listings(consultant="Diren Doğan")
    assert mine and all(it["consultant_id"] == owner for it in mine)
    # sonraki senkronlar da gerçek hesabı kullanır
    db.upsert_sahibinden_listing({"cid": "999", "title": "Yeni", "consultant_name": "Diren Doğan", "price": 1})
    assert db.one("SELECT consultant_id FROM sahibinden_listings WHERE classified_id='999'")["consultant_id"] == owner


def test_fmt_date_accepts_datetime_strings():
    assert R.fmt_date("2026-10-15T12:28:20") == "15.10.2026"
    assert R.fmt_date(date(2026, 1, 2)) == "02.01.2026"
