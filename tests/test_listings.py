from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from emlak_bot.config import Config
from emlak_bot.db import Database, normalize_title
from emlak_bot.scraper import parse_listings_from_html, seed_initial_listings, sync_listings_data
from emlak_bot.web import create_app


@pytest.fixture
def tmp_db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    db = Database(path)
    yield db
    Path(path).unlink(missing_ok=True)


def test_normalize_title():
    assert normalize_title("İZMİT YENİ MAHALLE RADAR'DA 2+1 ÇATI DUBLEX") == "izmit yeni mahalle radar da 2 1 cati dublex"
    assert normalize_title("Şık & Lüks Daire #12345") == "sik luks daire 12345"


def test_parse_html():
    sample_html = """
    <div class="classified " data-box-url="https://sahibinden.com/ilan/emlak-konut-kiralik-izmit-deniz-manzarali-daire-1234567890/detay">
      <img src="https://i0.shbdn.com/photo.jpg" alt="İzmit Deniz Manzaralı Daire #1234">
      <p class="location">Kocaeli / İzmit</p>
      <p class="date">15 Ekim 2026</p>
      <p class="price">30.000 TL</p>
    </div>
    """
    items = parse_listings_from_html(sample_html, default_consultant="Diren Doğan", default_phone="05320000000")
    assert len(items) == 1
    it = items[0]
    assert it["cid"] == "1234567890"
    assert it["title"] == "İzmit Deniz Manzaralı Daire"
    assert it["price"] == 30000.0
    assert it["price_raw"] == "30.000 TL"
    assert it["listing_type"] == "kiralik"
    assert it["consultant_name"] == "Diren Doğan"


def test_seed_initial_listings(tmp_db):
    res = seed_initial_listings(tmp_db)
    assert res["total"] == 64
    assert res["created"] == 64

    # Tekrar çağrıldığında mükerrer eklemez
    res2 = seed_initial_listings(tmp_db)
    assert res2["created"] == 0

    stats = tmp_db.get_sahibinden_stats()
    assert stats["total"] == 64
    assert stats["kiralik"] > 0
    assert stats["satilik"] > 0
    assert len(stats["consultants"]) >= 4


def test_30_day_republication_deduplication(tmp_db):
    item1 = {
        "cid": "111111",
        "title": "İzmit Yahya Kaptan 3+1 Kiralık Daire",
        "url": "https://sahibinden.com/ilan/111111/detay",
        "price": 35000.0,
        "price_raw": "35.000 TL",
        "listing_type": "kiralik",
        "consultant_name": "Diren Doğan",
        "consultant_phone": "05320602802",
        "location": "Kocaeli / İzmit",
        "date": "01 Ekim 2026",
    }
    lid1, st1 = tmp_db.upsert_sahibinden_listing(item1)
    assert st1 == "created"

    # 30 gün sonra aynı ilan yeni bir classified_id ile tekrar giriliyor:
    item2 = {
        "cid": "222222",  # YENİ İLAN NO
        "title": "İzmit Yahya Kaptan 3+1 Kiralık Daire",  # AYNI BAŞLIK & DANIŞMAN
        "url": "https://sahibinden.com/ilan/222222/detay",
        "price": 38000.0,  # Fiyat güncellenmiş olabilir
        "price_raw": "38.000 TL",
        "listing_type": "kiralik",
        "consultant_name": "Diren Doğan",
        "consultant_phone": "05320602802",
        "location": "Kocaeli / İzmit",
        "date": "01 Kasım 2026",
    }
    lid2, st2 = tmp_db.upsert_sahibinden_listing(item2)
    assert st2 == "republished"
    assert lid2 == lid1  # AYNI ID, yeni satır açılmadı!

    rec = tmp_db.get_sahibinden_listing(lid1)
    assert rec["classified_id"] == "222222"
    assert rec["republish_count"] == 2
    assert rec["price"] == 38000.0

    # Toplam kayıt sayısı hala 1 olmalı
    all_items = tmp_db.list_sahibinden_listings()
    assert len(all_items) == 1


def test_listings_web_and_tracking(tmp_db):
    from zoneinfo import ZoneInfo
    seed_initial_listings(tmp_db)
    admin_id = tmp_db.seed_admin("direndogann", "SertSifre123!")

    cfg = Config(
        bot_token="",
        owner_ids=frozenset({admin_id}),
        db_path=tmp_db.path,
        tz=ZoneInfo("Europe/Istanbul"),
        reminder_hour=9,
        inactivity_days=7,
        secret_key="test-secret",
        port=8000,
        web_enabled=True,
        cookie_secure=False,
        web_url="",
        brand_name="Gold Nicomedia Test",
    )
    app = create_app(tmp_db, cfg)
    app.config["TESTING"] = True
    client = app.test_client()

    # Giriş yap
    with client.session_transaction() as sess:
        sess["csrf"] = "test-csrf-token"

    client.post("/login", data={"username": "direndogann", "password": "SertSifre123!", "csrf": "test-csrf-token"})

    # İlanlar sayfası
    resp = client.get("/ilanlar")
    assert resp.status_code == 200
    assert "Gold Nicomedia İlan Portföyü" in resp.get_data(as_text=True)

    # Danışman filtresi
    resp_filter = client.get("/ilanlar?danisman=Burkay+Yıldırım")
    assert resp_filter.status_code == 200

    # Kiralık bir ilanı kira takibine al
    kiralik_items = [it for it in tmp_db.list_sahibinden_listings(listing_type="kiralik")]
    first = kiralik_items[0]

    with client.session_transaction() as sess:
        csrf_tok = sess.get("csrf")

    track_resp = client.post(
        f"/ilanlar/{first['id']}/takibe-al",
        data={"csrf": csrf_tok},
        follow_redirects=True,
    )
    assert track_resp.status_code == 200

    # Properties tablosuna eklendi mi?
    updated_item = tmp_db.get_sahibinden_listing(first["id"])
    assert updated_item["property_id"] is not None

    prop = tmp_db.get_property(updated_item["property_id"])
    assert prop["title"] == first["title"]
    assert prop["rent"] == first["price"]


def test_mark_listing_rented_and_closure(tmp_db):
    seed_initial_listings(tmp_db)
    admin_id = tmp_db.seed_admin("direndogann", "SertSifre123!")

    kiralik_items = [it for it in tmp_db.list_sahibinden_listings(listing_type="kiralik")]
    target = kiralik_items[1]

    # Kiralandı işlemi
    prop_id = tmp_db.mark_listing_rented(
        lid=target["id"],
        agent_id=admin_id,
        title=target["title"],
        rent=30000.0,
        start_date="2026-10-15",
        renewal_date="2027-10-15",
        tenant_name="Kemal Demir",
        tenant_phone="05321112233",
        landlord_name="Ayşe Kaya",
        landlord_phone="05423334455",
        notes="1 kira depozito alındı.",
    )
    assert prop_id > 0

    # İlan durumu 'rented' olmalı
    updated_listing = tmp_db.get_sahibinden_listing(target["id"])
    assert updated_listing["status"] == "rented"
    assert updated_listing["deal_tenant_name"] == "Kemal Demir"
    assert updated_listing["deal_rent"] == 30000.0

    # Mülk 'active' olmalı
    prop = tmp_db.get_property(prop_id)
    assert prop["status"] == "active"
    assert prop["tenant_name"] == "Kemal Demir"
    assert prop["renewal_date"] == "2027-10-15"

    # İlanı kapat / arşive al
    third = kiralik_items[2]
    tmp_db.close_or_delete_listing(third["id"], action="archive", reason="Mülk sahibi vazgeçti")
    closed = tmp_db.get_sahibinden_listing(third["id"])
    assert closed["status"] == "inactive"


def test_vacant_and_new_tenant_lifecycle(tmp_db):
    admin_id = tmp_db.seed_admin("direndogann", "SertSifre123!")
    pid = tmp_db.add_property(
        agent_id=admin_id,
        title="Daire 12 İzmit",
        rent=25000.0,
        renewal_date="2027-01-01",
        tenant_name="Eski Kiracı",
        status="active",
    )

    # Kiracı çıktı
    tmp_db.mark_property_vacant(
        pid=pid,
        exit_date="2026-10-15",
        deposit_notes="Hasarsız 25.000 TL iade edildi",
        handover_notes="2 anahtar teslim alındı, boyalı teslim edildi",
        next_step="vacant",
    )

    vacant_prop = tmp_db.get_property(pid)
    assert vacant_prop["status"] == "vacant"
    assert vacant_prop["exit_date"] == "2026-10-15"
    assert "25.000 TL" in vacant_prop["deposit_notes"]

    # Yeni kiracı girdi
    tmp_db.reassign_tenant(
        pid=pid,
        tenant_name="Yeni Kiracı Ali",
        tenant_phone="05559998877",
        rent=32000.0,
        start_date="2026-11-01",
        renewal_date="2027-11-01",
        notes="Kefil alındı.",
    )

    reactivated = tmp_db.get_property(pid)
    assert reactivated["status"] == "active"
    assert reactivated["tenant_name"] == "Yeni Kiracı Ali"
    assert reactivated["rent"] == 32000.0
    assert reactivated["renewal_date"] == "2027-11-01"
