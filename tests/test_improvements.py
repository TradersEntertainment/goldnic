"""Mevcut özelliklerin geliştirmeleri: bildirim alıcıları, ofis özeti, toplu giriş, CSV, boş mülk, kanallar."""
from __future__ import annotations

import asyncio
import re
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from werkzeug.security import generate_password_hash

from emlak_bot import crm
from emlak_bot import matching as M
from emlak_bot.config import Config
from emlak_bot.db import Database
from emlak_bot.messenger import Messenger, TelegramMessenger, WhatsAppMessenger
from emlak_bot.reminders import build_owner_report, collect_issues, run_reminders
from emlak_bot.services import CSV_HEADER, portfolio_csv
from emlak_bot.web import create_app, parse_tabular_input

TZ = ZoneInfo("Europe/Istanbul")
TODAY = date(2026, 10, 15)


class Recorder(Messenger):
    channel = "telegram"

    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text, buttons=None):
        self.sent.append(str(chat_id))
        return True


def cfg(**kw) -> Config:
    base = dict(bot_token="", owner_ids=frozenset(), db_path="", tz=TZ, reminder_hour=9, inactivity_days=7,
                secret_key="s", port=8000, web_enabled=True, cookie_secure=False, web_url="")
    base.update(kw)
    return Config(**base)


def test_reminders_skip_web_only_accounts_and_phantoms(tmp_path):
    db = Database(str(tmp_path / "r.db"))
    tg_owner = db.add_user(1001, "Diren Doğan", "owner")                     # Telegram'daki ofis sahibi
    web_owner = db.seed_admin("direndogann", "Sifre12345!", name="Diren D.")   # web'den açılmış (chat_id = kullanıcı adı)
    agent = db.add_user(2002, "Burkay", "agent")
    db.add_user("Erkan Kartay", "Erkan Kartay", "agent", channel="web")        # Sahibinden'in açtığı hayalet kayıt
    db.add_property(agent_id=agent, title="Yahya Kaptan 3+1", rent=30000,
                    renewal_date=(TODAY + timedelta(days=30)).isoformat())
    m = Recorder()
    assert asyncio.run(run_reminders(db, m, TODAY)) == 1
    assert set(m.sent) == {"1001", "2002"}                                    # "direndogann" denenmez
    assert db.get_user(web_owner)["chat_id"] == "direndogann" and tg_owner
    inactive = {u["name"] for u in collect_issues(db, TODAY, 7)["inactive"]}
    assert inactive == {"Burkay"}                                             # hayalet danışman uyarı üretmez
    report = build_owner_report(db, TODAY, 7, full=True)
    assert "Erkan Kartay" not in report


def test_owner_report_includes_crm_deals_and_backup(tmp_path):
    db = Database(str(tmp_path / "o.db"))
    owner = db.add_user(1, "Diren Doğan", "owner")
    p = M.parse("Mehmet Bey 0532 111 22 33 3+1 kiralık Kartepe acil")
    did = crm.create_demand(db, name=p.name, phone=p.phone, criteria=p.criteria, agent_id=owner, created_by=owner)
    old = (TODAY - timedelta(days=3)).isoformat() + "T09:00:00"
    db.execute("UPDATE client_demands SET last_contact_at=?, created_at=? WHERE id=?", (old, old, did))
    db.record_deal("kiralama", owner, 30000, (TODAY - timedelta(days=2)).isoformat())
    db.set_setting("backup_last_attempt", "2026-10-15T03:00:00")
    db.set_setting("backup_last_ok", "2026-10-14T03:00:00")
    report = build_owner_report(db, TODAY, 7, full=True)
    assert "🎯 <b>Müşteri talepleri:</b> 1 aktif" in report
    assert "Acil müşteri 3 gündür aranmadı: Mehmet Bey" in report
    assert "Son 7 gün: 1 kiralama, 0 satış" in report
    assert "Son yedekleme başarısız" in report


def test_bulk_import_contract_columns_validated(tmp_path):
    db = Database(str(tmp_path / "b.db"))
    owner = db.add_user(1, "Diren Doğan", "owner")
    raw = ("Daire\tKira\tYenileme\tKiracı\tKiracı TC\tEv Sahibi\tEv Sahibi T.C.\tIBAN\tAdres\tTeslim Tarihi\tDepozito\tTür\n"
           "Yahya Kaptan 3+1\t32.000\t15.11.2027\tAhmet Yılmaz\t10000000146\tMehmet Demir\t12345678901\t"
           "TR33 0006 1005 1978 6457 8413 26\tYahya Kaptan Mah. Gül Sk. 12\t15.11.2021\t64.000\tKonut\n"
           "Fethiye Cd. Dükkan\t45.000\t20.01.2027\tCan Özkan\t\t\t\t\t\t\t\tİşyeri\n")
    items = parse_tabular_input(raw)
    assert "tenant_phone" not in items[0]                     # eksik sütun sıradan tahmin edilmez
    n, errors = db.bulk_add_properties(items, owner)
    assert n == 2 and len(errors) == 1 and "Ev sahibi T.C. geçersiz" in errors[0]
    p = db.one("SELECT * FROM properties WHERE title='Yahya Kaptan 3+1'")
    assert p["tenant_tc"] == "10000000146" and p["landlord_tc"] is None
    assert p["landlord_iban"] == "TR330006100519786457841326" and p["deposit_amount"] == 64000
    assert p["address"] == "Yahya Kaptan Mah. Gül Sk. 12" and p["delivery_date"] == "2021-11-15"
    assert db.one("SELECT property_kind FROM properties WHERE title='Fethiye Cd. Dükkan'")[0] == "isyeri"


def test_csv_export_roundtrips_through_bulk_import(tmp_path):
    db = Database(str(tmp_path / "c.db"))
    owner = db.add_user(1, "Diren Doğan", "owner")
    db.add_property(agent_id=owner, title="Kent Konut 2+1", rent=21500.5, renewal_date="2027-02-01",
                    tenant_name="Elif Şahin", tenant_phone="0544 555 66 77", landlord_name="Kemal Arslan",
                    lease_start_date="2022-02-01", property_kind="konut", address="Kent Konut 2. Etap B Blok",
                    deposit_amount=43000, tenant_tc="10000000146", landlord_iban="TR330006100519786457841326",
                    notes="Satır 1\nSatır 2")
    text = portfolio_csv(db, TODAY)
    assert text.startswith("﻿") and text.splitlines()[0].lstrip("﻿") == ";".join(CSV_HEADER)
    assert "10000000146" not in text and "TR33" not in text                  # KVKK: kimlik/IBAN dışa aktarılmaz
    assert "21500,5" in text and "Satır 1 Satır 2" in text
    items = parse_tabular_input(text.lstrip("﻿"))
    it = items[0]
    assert it["title"] == "Kent Konut 2+1" and it["rent"] == "21500,5" and it["renewal_date"] == "01.02.2027"
    assert it["tenant_name"] == "Elif Şahin" and it["lease_start_date"] == "01.02.2022"
    assert it["address"] == "Kent Konut 2. Etap B Blok" and it["deposit_amount"] == "43000"
    assert it["property_kind"] == "Konut" and it["consultant"] == "Diren Doğan"


def test_vacant_agenda_item_shows_matching_clients(tmp_path):
    db = Database(str(tmp_path / "v.db"))
    owner = db.add_user(1, "Diren Doğan", "owner")
    db.set_credentials(owner, "diren", generate_password_hash("diren-pass-1"))
    pid = db.add_property(agent_id=owner, title="Topçular Tuana Evleri 2+1", rent=38000, renewal_date="2027-01-01",
                          address="Topçular, İzmit")
    db.mark_property_vacant(pid, (TODAY - timedelta(days=40)).isoformat())
    p = M.parse("Selda Hanım 0505 444 33 22 2+1 kiralık Topçular 35-42 bin")
    did = crm.create_demand(db, name=p.name, phone=p.phone, criteria=p.criteria, agent_id=owner, created_by=owner)
    crm.refresh(db, TODAY, demand_ids=[did])
    app = create_app(db, cfg())
    c = app.test_client()
    html = c.get("/login").get_data(as_text=True)
    tok = re.search(r'name="csrf" value="([^"]+)"', html).group(1)
    c.post("/login", data={"username": "diren", "password": "diren-pass-1", "csrf": tok})
    page = c.get("/takip").get_data(as_text=True)
    assert "🎯 1 uygun müşteri" in page and f"/mulk/{pid}/musteriler" in page


def test_messenger_channels():
    class Bot:
        def __init__(self):
            self.calls = []

        async def send_message(self, **kw):
            self.calls.append(kw)

    bot = Bot()
    tm = TelegramMessenger(bot)
    assert asyncio.run(tm.send("direndogann", "x")) is False and not bot.calls   # sayısal olmayan kimlik atlanır
    assert asyncio.run(tm.send("1001", "<b>x</b>")) is True and bot.calls[0]["chat_id"] == 1001

    sent = []
    import emlak_bot.whatsapp as W

    orig = W.send_whatsapp_notification
    W.send_whatsapp_notification = lambda phone, text, c=None: sent.append((phone, text)) or True
    try:
        wm = WhatsAppMessenger(cfg(web_url="https://os.example"))
        assert asyncio.run(wm.send("05321112233", "<b>Kira</b> yenileme", [[("Aradım", "ack:1:2026-11-01")]]))
    finally:
        W.send_whatsapp_notification = orig
    assert sent == [("05321112233", "Kira yenileme\n\nOnaylamak için: https://os.example/takip")]
