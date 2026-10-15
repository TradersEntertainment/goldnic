"""Modül 3: Tek tıkla resmi PDF motoru (Kira Sözleşmesi, Tahliye Taahhütnamesi, Teslim Tutanağı)."""
from __future__ import annotations

import io
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from werkzeug.security import generate_password_hash

from emlak_bot import docstore
from emlak_bot import documents as D
from emlak_bot.config import Config
from emlak_bot.db import Database
from emlak_bot.validators import amount_words, iban_format, iban_valid, money_exact, number_words, tc_kimlik_valid
from emlak_bot.web import create_app

pypdf = pytest.importorskip("pypdf")

VALID_TC = "10000000146"
VALID_IBAN = "TR330006100519786457841326"


def pdf_text(data: bytes) -> tuple[int, str]:
    reader = pypdf.PdfReader(io.BytesIO(data))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    return len(reader.pages), re.sub(r"\s+", " ", text)


# ---------------------------------------------------------------- doğrulayıcılar
def test_tc_kimlik_checksum():
    assert tc_kimlik_valid(VALID_TC)
    assert tc_kimlik_valid("100 000 001 46")
    assert not tc_kimlik_valid("10000000147")       # 11. hane yanlış
    assert not tc_kimlik_valid("10000000156")       # 10. hane yanlış
    assert not tc_kimlik_valid("00000000146")       # 0 ile başlayamaz
    assert not tc_kimlik_valid("1234567890")


def test_iban_mod97_and_format():
    assert iban_valid(VALID_IBAN) and iban_valid("tr33 0006 1005 1978 6457 8413 26")
    assert not iban_valid("TR330006100519786457841327")
    assert not iban_valid("TR3300061005197864578413")          # TR IBAN 26 karakter olmalı
    assert iban_format(VALID_IBAN) == "TR33 0006 1005 1978 6457 8413 26"


def test_turkish_amount_words():
    assert number_words(1000) == "bin" and number_words(1001000) == "bir milyon bin"
    assert amount_words(32000) == "Otuz İki Bin Türk Lirası"
    assert amount_words(42076.8) == "Kırk İki Bin Yetmiş Altı Türk Lirası Seksen Kuruş"
    assert amount_words(100.05) == "Yüz Türk Lirası Beş Kuruş"
    assert money_exact(1234567.5) == "1.234.567,50 TL"


# ---------------------------------------------------------------- PDF içerikleri
def meta(**kw):
    base = dict(doc_no="GN-2026-000007", verify_code="ABCD-1234", created=datetime(2026, 10, 15, 10, 0),
                office_line="İzmit / Kocaeli · 0532 060 28 02", license_no="4100123")
    base.update(kw)
    return D.DocMeta(**base)


def lease(**kw) -> D.LeaseData:
    base = dict(
        landlord=D.Party("Gülşen Çakır", VALID_TC, "Yahya Kaptan Mah. İzmit", "05423334455", VALID_IBAN),
        tenant=D.Party("Şükrü Öztürk", VALID_TC, "Alikahya Mah. İzmit", "05321112233"),
        address="Yahya Kaptan Mah. Gül Sk. No:12 D:4 İzmit/Kocaeli", rent=32000, start=date(2026, 11, 1),
        contract_date=date(2026, 10, 20), unit_desc="3+1 Daire", deposit=64000, payment_day=5,
        fixtures=["Kombi", "Ankastre fırın"], special_terms=["Evcil hayvan beslenmeyecektir."])
    base.update(kw)
    return D.LeaseData(**base)


def test_lease_pdf_contains_legal_turkish_content():
    data = D.build_lease_pdf(lease(), meta())
    assert data.startswith(b"%PDF")
    pages, text = pdf_text(data)
    assert pages >= 2
    for needle in ("KİRA SÖZLEŞMESİ", "Gülşen Çakır", "Şükrü Öztürk", "32.000,00 TL", "Otuz İki Bin Türk Lirası",
                   "TR33 0006 1005 1978 6457 8413 26", "TBK m. 344/1", "TBK m. 346", "TBK m. 347", "TBK m. 342",
                   "5490 sayılı", "arabulucuya", "binde 1,89", "GN-2026-000007", "ABCD-1234", f"Sayfa 1 / {pages}",
                   "Evcil hayvan beslenmeyecektir", "4100123", "01.11.2026 – 01.11.2027"):
        assert needle in text, needle
    assert "MÜTESELSİL KEFALET" not in text           # kefil yoksa kefalet bölümü yok
    assert "cezai şart" not in text.lower()           # TBK 346'ya aykırı madde yok


def test_lease_with_guarantor_has_handwriting_fields_and_isyeri_skips_address_clause():
    data = D.build_lease_pdf(lease(guarantor=D.Party("Kemal Yıldız", VALID_TC, "Kabaoğlu"), guarantor_limit=400000,
                                   kind="isyeri", usage_purpose="İşyeri (ofis)", deposit=None), meta())
    _, text = pdf_text(data)
    assert "MÜTESELSİL KEFALET (TBK m. 583-584)" in text and "el yazısıyla" in text
    assert "400.000,00 TL" in text and "Dört Yüz Bin Türk Lirası" in text
    assert "5490 sayılı" not in text and "Çatılı işyeri" in text
    assert "Güvence bedeli. Güvence" not in text          # depozito yoksa güvence maddesi yok


def test_eviction_pdf_enforces_signing_after_delivery():
    ll, tn = D.Party("Gülşen Çakır", VALID_TC), D.Party("Şükrü Öztürk", VALID_TC, "Alikahya")
    with pytest.raises(ValueError, match="SONRAKİ"):
        D.build_eviction_pdf(D.EvictionData(ll, tn, "Adres", date(2026, 10, 1), date(2026, 10, 1), date(2027, 10, 1),
                                            date(2026, 10, 1)), meta())
    with pytest.raises(ValueError):
        D.build_eviction_pdf(D.EvictionData(ll, tn, "Adres", date(2026, 10, 1), date(2026, 10, 1), date(2026, 10, 2),
                                            date(2026, 10, 3)), meta())
    data = D.build_eviction_pdf(D.EvictionData(ll, tn, "Adres", date(2026, 10, 1), date(2026, 10, 1),
                                               date(2027, 10, 1), date(2026, 10, 5)), meta())
    pages, text = pdf_text(data)
    assert pages == 1 and "TAHLİYE TAAHHÜTNAMESİ" in text and "01.10.2027" in text
    assert "352/1" in text and "272" in text and "tesliminden sonra" in text


def test_handover_pdf_teslim_and_iade():
    ll, tn = D.Party("Gülşen Çakır"), D.Party("Şükrü Öztürk")
    items = [D.HandoverItem("Kombi", "1", "Bakımlı"), D.HandoverItem("Klima", "2", "Sağlam")]
    _, text = pdf_text(D.build_handover_pdf(D.HandoverData(ll, tn, "Adres", date(2026, 11, 1), keys="3 anahtar",
                                                           meter_electric="45.120", items=items), meta()))
    assert "TESLİM TUTANAĞI" in text and "Kombi" in text and "45.120" in text and "TBK m. 334" in text
    _, text2 = pdf_text(D.build_handover_pdf(D.HandoverData(ll, tn, "Adres", date(2027, 11, 1), direction="iade"), meta()))
    assert "İADE (TAHLİYE) TUTANAĞI" in text2


# ---------------------------------------------------------------- belge kaydı
@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "docs.db"))


def test_registry_numbers_codes_and_lookup(db):
    uid = db.add_user(1, "Diren Doğan", "owner")
    first = docstore.create(db, kind="teslim_tutanagi", property_id=None, payload={"a": 1},
                            render=lambda m: D.build_handover_pdf(D.HandoverData(D.Party("A"), D.Party("B"), "X",
                                                                                 date(2026, 1, 1)), m),
                            user_id=uid, secret="s")
    second = docstore.create(db, kind="teslim_tutanagi", property_id=None, payload={"a": 2}, render=lambda m: b"%PDF-x",
                             user_id=uid, secret="s")
    year = datetime.now().year
    assert first["doc_no"] == f"GN-{year}-000001" and second["doc_no"] == f"GN-{year}-000002"
    assert re.fullmatch(r"[0-9A-F]{4}-[0-9A-F]{4}", first["verify_code"])
    assert first["verify_code"] in pdf_text(first["pdf"])[1]
    assert docstore.find(db, first["verify_code"].lower())["id"] == first["id"]
    assert docstore.find(db, second["doc_no"])["id"] == second["id"]
    assert docstore.find(db, "XXXX-0000") is None
    import hashlib
    assert docstore.get(db, first["id"])["sha256"] == hashlib.sha256(first["pdf"]).hexdigest()


# ---------------------------------------------------------------- web akışı
@pytest.fixture
def env(tmp_path):
    database = Database(str(tmp_path / "w.db"))
    owner = database.add_user(1, "Diren Doğan", "owner")
    agent = database.add_user(2, "Burkay Yıldırım", "agent")
    other = database.add_user(3, "Ecenaz Öztekin", "agent")
    for uid, user in ((owner, "diren"), (agent, "burkay"), (other, "ecenaz")):
        database.set_credentials(uid, user, generate_password_hash(f"{user}-pass-1"))
    today = date.today()
    pid = database.add_property(
        agent_id=agent, title="Yahya Kaptan 3+1", rent=30000, renewal_date=(today + timedelta(days=200)).isoformat(),
        landlord_name="Gülşen Çakır", tenant_name="Şükrü Öztürk", lease_start_date=(today - timedelta(days=165)).isoformat(),
        delivery_date=(today - timedelta(days=165)).isoformat())
    cfg = Config("", frozenset(), "", ZoneInfo("Europe/Istanbul"), 9, 7, "test-secret", 8000, True, False, "")
    app = create_app(database, cfg)
    app.config["TESTING"] = True
    return app, database, dict(owner=owner, agent=agent, other=other, pid=pid, today=today)


def login(client, user):
    html = client.get("/login").get_data(as_text=True)
    token = re.search(r'name="csrf" value="([^"]+)"', html).group(1)
    client.post("/login", data={"username": user, "password": f"{user}-pass-1", "csrf": token})
    return token


def csrf_of(client, path):
    return re.search(r'name="csrf" value="([^"]+)"', client.get(path).get_data(as_text=True)).group(1)


def test_details_form_validates_tc_iban_and_deposit(env):
    app, db, ids = env
    c = app.test_client()
    login(c, "burkay")
    pid = ids["pid"]
    tok = csrf_of(c, f"/mulk/{pid}/belgeler")
    bad_tc = c.post(f"/mulk/{pid}/belgeler/bilgi", data={"csrf": tok, "tenant_tc": "12345678901"}, follow_redirects=True)
    assert "T.C. Kimlik No geçersiz" in bad_tc.get_data(as_text=True)
    bad_iban = c.post(f"/mulk/{pid}/belgeler/bilgi", data={"csrf": tok, "landlord_iban": "TR000006100519786457841326"},
                      follow_redirects=True)
    assert "IBAN geçersiz" in bad_iban.get_data(as_text=True)
    too_much = c.post(f"/mulk/{pid}/belgeler/bilgi", data={"csrf": tok, "deposit_amount": "100.000"}, follow_redirects=True)
    assert "TBK m. 342" in too_much.get_data(as_text=True)
    assert db.get_property(pid)["deposit_amount"] is None
    ok = c.post(f"/mulk/{pid}/belgeler/bilgi", data={
        "csrf": tok, "landlord_tc": VALID_TC, "tenant_tc": "100 000 001 46", "landlord_iban": "tr33 0006 1005 1978 6457 8413 26",
        "address": "Yahya Kaptan Mah. Gül Sk. No:12 İzmit", "deposit_amount": "60.000", "payment_day": "5",
        "fixtures": "Kombi\nKlima"}, follow_redirects=True)
    assert "Belge bilgileri kaydedildi" in ok.get_data(as_text=True)
    p = db.get_property(pid)
    assert p["tenant_tc"] == VALID_TC and p["landlord_iban"] == VALID_IBAN and p["deposit_amount"] == 60000


def _fill(db, pid):
    db.update_property(pid, landlord_tc=VALID_TC, tenant_tc=VALID_TC, address="Yahya Kaptan Mah. Gül Sk. No:12 İzmit",
                       landlord_iban=VALID_IBAN, fixtures="Kombi\nKlima")


def test_one_click_lease_pdf_and_registry(env):
    app, db, ids = env
    pid = ids["pid"]
    c = app.test_client()
    login(c, "burkay")
    tok = csrf_of(c, f"/mulk/{pid}")
    missing = c.post(f"/mulk/{pid}/belge/kira_sozlesmesi", data={"csrf": tok})
    assert missing.status_code == 302 and "/belgeler" in missing.headers["Location"]   # eksik bilgi → forma
    _fill(db, pid)
    page = c.get(f"/mulk/{pid}").get_data(as_text=True)
    assert 'id="belgeler-panel"' in page and f'/mulk/{pid}/belge/kira_sozlesmesi' in page
    r = c.post(f"/mulk/{pid}/belge/kira_sozlesmesi", data={"csrf": tok})
    assert r.status_code == 200 and r.mimetype == "application/pdf"
    assert "Kira-Sozlesmesi-GN-" in r.headers["Content-Disposition"]
    _, text = pdf_text(r.data)
    assert "Şükrü Öztürk" in text and "30.000,00 TL" in text and "Otuz Bin Türk Lirası" in text
    docs = docstore.list_for(db, pid)
    assert len(docs) == 1 and docs[0]["kind"] == "kira_sozlesmesi"
    dl = c.get(f"/belge/{docs[0]['id']}.pdf?indir=1")
    assert dl.status_code == 200 and dl.headers["Content-Disposition"].startswith("attachment")
    v = c.get(f"/belge/dogrula?kod={docs[0]['verify_code']}").get_data(as_text=True)
    assert "Belge doğrulandı" in v
    assert c.get("/belge/dogrula?kod=YOKK-0000").get_data(as_text=True).count("Kayıt bulunamadı") == 1
    hits = c.get(f"/api/spotlight?q={docs[0]['doc_no']}").get_json()
    assert any(i["url"].endswith(f"/belge/{docs[0]['id']}.pdf") for g in hits["groups"] for i in g["items"])
    other = app.test_client()
    login(other, "ecenaz")
    assert other.get(f"/belge/{docs[0]['id']}.pdf").status_code == 403
    assert other.get(f"/mulk/{pid}/belgeler").status_code == 403


def test_eviction_commitment_document_feeds_legal_radar(env):
    app, db, ids = env
    pid, today = ids["pid"], ids["today"]
    _fill(db, pid)
    c = app.test_client()
    login(c, "burkay")
    tok = csrf_of(c, f"/mulk/{pid}/belgeler")
    delivery = db.get_property(pid)["delivery_date"]
    bad = c.post(f"/mulk/{pid}/belge/tahliye_taahhutnamesi", data={"csrf": tok, "signed_date": delivery},
                 follow_redirects=True)
    assert "tesliminden SONRA" in bad.get_data(as_text=True)
    eviction = (today + timedelta(days=200)).isoformat()
    r = c.post(f"/mulk/{pid}/belge/tahliye_taahhutnamesi",
               data={"csrf": tok, "signed_date": today.isoformat(), "eviction_date": eviction, "add_to_radar": "1"})
    assert r.mimetype == "application/pdf"
    row = db.one("SELECT * FROM eviction_commitments WHERE property_id=?", (pid,))
    assert row and row["eviction_date"] == eviction and row["signed_date"] == today.isoformat()
    assert "ile oluşturuldu" in row["notes"]


def test_handover_redirect_mode_and_telegram_without_token(env):
    app, db, ids = env
    pid = ids["pid"]
    _fill(db, pid)
    c = app.test_client()
    login(c, "diren")
    tok = csrf_of(c, f"/mulk/{pid}/belgeler")
    r = c.post(f"/mulk/{pid}/belge/teslim_tutanagi", data={
        "csrf": tok, "respond": "redirect", "keys": "3 anahtar", "items": "Kombi | 1 | Bakımlı\nKlima|2"},
        follow_redirects=True)
    html = r.get_data(as_text=True)
    assert "Teslim Tutanağı oluşturuldu" in html and "Paylaş (WhatsApp)" in html
    doc = docstore.list_for(db, pid)[0]
    payload_pdf = docstore.get(db, doc["id"])["pdf"]
    _, text = pdf_text(payload_pdf)
    assert "Kombi" in text and "Bakımlı" in text and "Klima" in text and "3 anahtar" in text
    t = c.post(f"/belge/{doc['id']}/telegram", data={"csrf": tok}, follow_redirects=True)
    assert "TELEGRAM_BOT_TOKEN" in t.get_data(as_text=True)
