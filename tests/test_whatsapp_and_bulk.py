import io
from datetime import date
from zoneinfo import ZoneInfo
import pytest
from werkzeug.security import generate_password_hash

from emlak_bot.config import Config
from emlak_bot.db import Database
from emlak_bot.web import create_app, parse_tabular_input
from emlak_bot.whatsapp import (
    clean_phone_for_whatsapp,
    get_whatsapp_chat_url,
    build_tenant_message,
    build_agent_escalation_message,
)
from emlak_bot.reminders import run_reminders


def test_whatsapp_helpers():
    # Telefon temizleme
    assert clean_phone_for_whatsapp("0532 123 45 67") == "905321234567"
    assert clean_phone_for_whatsapp("+90 532 123 45 67") == "905321234567"
    assert clean_phone_for_whatsapp("5321234567") == "905321234567"
    assert clean_phone_for_whatsapp("0542-333-22-11") == "905423332211"

    # WhatsApp sohbet linki
    url = get_whatsapp_chat_url("0532 123 45 67", "Merhaba test")
    assert url.startswith("https://wa.me/905321234567?text=")
    assert "Merhaba%20test" in url

    # Kiracı mesajı
    t_msg = build_tenant_message("Yahya Kaptan 3+1", "15.11.2024", 30, "Diren Doğan", "Gold Nicomedia")
    assert "Gold Nicomedia" in t_msg
    assert "Yahya Kaptan 3+1" in t_msg
    assert "30 gün sonra" in t_msg

    # Kademe mesajı
    esc_msg = build_agent_escalation_message("Yahya Kaptan 3+1", "15.11.2024", 10, 25000, "Ahmet Yılmaz", "05321112233")
    assert "🚨 ACİL" in esc_msg
    assert "Müşteri henüz aranmadı!" in esc_msg


def test_parse_tabular_input():
    # Excel'den tab ile kopyalanmış veri (başlıklı)
    excel_text = (
        "Daire / Başlık\tKira Tutarı\tYenileme Tarihi\tKiracı Adı\tKiracı Tel\tEv Sahibi\tEv Sahibi Tel\n"
        "Yahya Kaptan 3+1\t32000\t15.11.2024\tAhmet Yılmaz\t05321112233\tMehmet Demir\t05423334455\n"
        "İzmit Merkez 2+1\t24.000 TL\t01.12.2024\tAyşe Kaya\t05051234567\tAli Veli\t05559876543\n"
    )
    items = parse_tabular_input(excel_text)
    assert len(items) == 2
    assert items[0]["title"] == "Yahya Kaptan 3+1"
    assert items[0]["rent"] == "32000"
    assert items[0]["tenant_phone"] == "05321112233"
    assert items[1]["title"] == "İzmit Merkez 2+1"
    assert items[1]["rent"] == "24.000 TL"

    # Başlıksız virgülle veya noktalı virgülle ayrılmış veri
    csv_text = "Kartepe Villa;75000;20.01.2025;Can Özkan;05339998877;Hasan Çelik;05301119988"
    items_csv = parse_tabular_input(csv_text)
    assert len(items_csv) == 1
    assert items_csv[0]["title"] == "Kartepe Villa"
    assert items_csv[0]["rent"] == "75000"


@pytest.fixture
def web_env(tmp_path):
    db = Database(str(tmp_path / "bulk_test.db"))
    owner_id = db.seed_admin("direndogann", "Diren12345!")
    agent_id = db.create_or_update_agent("Burkay", "burkay", generate_password_hash("Burkay123!"), "agent", 0)

    cfg = Config(
        bot_token="",
        owner_ids=frozenset({owner_id}),
        db_path=db.path,
        tz=ZoneInfo("Europe/Istanbul"),
        reminder_hour=9,
        inactivity_days=7,
        secret_key="secret-key",
        port=8000,
        web_enabled=True,
        cookie_secure=False,
        web_url="",
        brand_name="Gold Nicomedia",
    )
    app = create_app(db, cfg)
    app.config["TESTING"] = True
    return app, db, owner_id, agent_id


def test_bulk_import_and_security(web_env):
    app, db, owner_id, agent_id = web_env
    client = app.test_client()

    # 1. Normal danışman (Burkay) toplu giriş sayfasına erişemez (403)
    with client.session_transaction() as sess:
        sess["csrf"] = "c1"
    client.post("/login", data={"username": "burkay", "password": "Burkay123!", "csrf": "c1"})

    resp_forbidden = client.get("/toplu-giris")
    assert resp_forbidden.status_code == 403

    resp_dl_forbidden = client.get("/toplu-giris/sablon-indir")
    assert resp_dl_forbidden.status_code == 403

    # 2. Diren Doğan (owner) giriş yapar
    with client.session_transaction() as sess:
        sess.clear()
        sess["csrf"] = "c2"
    client.post("/login", data={"username": "direndogann", "password": "Diren12345!", "csrf": "c2"})

    resp_ok = client.get("/toplu-giris")
    assert resp_ok.status_code == 200
    assert "Toplu Portföy & Kira Girişi" in resp_ok.get_data(as_text=True)

    # Şablon indirme testi
    resp_dl = client.get("/toplu-giris/sablon-indir")
    assert resp_dl.status_code == 200
    assert "gold_nicomedia_kira_sablon.csv" in resp_dl.headers.get("Content-Disposition", "")
    assert "Yahya Kaptan 3+1 Daire" in resp_dl.get_data(as_text=True)

    # 3. Toplu veri yapıştırma (POST /toplu-giris)
    paste_payload = (
        "Daire 101\t28000\t15.11.2024\tKiracı 1\t05321111111\tEv Sahibi 1\t05421111111\n"
        "Daire 102\t35000\t01.12.2024\tKiracı 2\t05322222222\tEv Sahibi 2\t05422222222\n"
        "Daire 103\t42000\t20.01.2025\tKiracı 3\t05323333333\tEv Sahibi 3\t05423333333\n"
    )
    with client.session_transaction() as sess:
        sess["csrf"] = "csrf_bulk"
    post_resp = client.post(
        "/toplu-giris",
        data={
            "csrf": "csrf_bulk",
            "mode": "paste",
            "paste_data": paste_payload,
            "agent_id": str(owner_id),
        },
        follow_redirects=True,
    )
    assert post_resp.status_code == 200
    assert "3 adet kiralık portföy kaydı" in post_resp.get_data(as_text=True)

    # Veritabanında kontrol et
    props = db.list_properties()
    assert len(props) == 3
    titles = [p["title"] for p in props]
    assert "Daire 101" in titles and "Daire 102" in titles and "Daire 103" in titles


def test_aradim_stops_reminders(web_env):
    import asyncio
    app, db, owner_id, agent_id = web_env
    client = app.test_client()

    today = date(2024, 10, 15)
    # Yenileme 15 gün sonra (30 gün sınırının içinde)
    pid = db.add_property(
        agent_id=owner_id,
        title="Daire Test 200",
        rent=30000,
        renewal_date="2024-10-30",
        tenant_name="Test Kiracı",
        tenant_phone="05320000000",
    )

    # Henüz aranmadı: acked olmamalı
    assert db.is_reminder_acked(pid, "2024-10-30") is False

    # Hatırlatmayı çalıştır -> 1 hatırlatma işaretlenmeli
    n = asyncio.run(run_reminders(db, messenger=None, today=today))
    assert n == 1

    # Giriş yap ve "Müşteriyi Aradım" butonuna tıkla
    with client.session_transaction() as sess:
        sess.clear()
        sess["csrf"] = "c3"
    client.post("/login", data={"username": "direndogann", "password": "Diren12345!", "csrf": "c3"})

    with client.session_transaction() as sess:
        sess["csrf"] = "csrf_aradim"
    resp_aradim = client.post(
        f"/mulk/{pid}/aradim",
        data={"csrf": "csrf_aradim", "notes": "Kiracıyla anlaşıldı"},
        follow_redirects=True,
    )
    assert resp_aradim.status_code == 200
    assert "Müşteri arandı olarak kaydedildi" in resp_aradim.get_data(as_text=True)

    # Şimdi kontrol et:
    assert db.is_reminder_acked(pid, "2024-10-30") is True

    # Tekrar hatırlatmayı çalıştır -> KULLANICI ARADIM DEDİĞİ İÇİN 0 GÖNDERİLMELİ, BİLDİRİM DURMALI!
    n_after = asyncio.run(run_reminders(db, messenger=None, today=today))
    assert n_after == 0
