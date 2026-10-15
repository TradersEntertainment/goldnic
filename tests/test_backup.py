"""Modül 5: Şifreli gece yedeği, geri yükleme ve denetim izi."""
from __future__ import annotations

import io
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from werkzeug.security import generate_password_hash

pytest.importorskip("cryptography")

from emlak_bot import backup  # noqa: E402
from emlak_bot.config import Config  # noqa: E402
from emlak_bot.db import Database  # noqa: E402
from emlak_bot.web import create_app  # noqa: E402

TZ = ZoneInfo("Europe/Istanbul")
PASS = "çok-gizli parola 2026!"


def make_cfg(db_path: str, **kw) -> Config:
    base = dict(bot_token="", owner_ids=frozenset(), db_path=db_path, tz=TZ, reminder_hour=9, inactivity_days=7,
                secret_key="test-secret", port=8000, web_enabled=True, cookie_secure=False, web_url="",
                backup_passphrase=PASS, backup_dir=str(Path(db_path).parent / "yedekler"))
    base.update(kw)
    return Config(**base)


@pytest.fixture
def env(tmp_path):
    path = str(tmp_path / "ofis.db")
    db = Database(path)
    owner = db.add_user(111, "Diren Doğan", "owner")
    agent = db.add_user(222, "Burkay Yıldırım", "agent")
    db.add_property(agent_id=agent, title="Yahya Kaptan 3+1", rent=32000, renewal_date="2027-01-01",
                    tenant_name="Ahmet Yılmaz", tenant_phone="0532 111 22 33", tenant_tc="10000000146")
    db.execute("INSERT INTO documents (doc_no, kind, verify_code, sha256, payload, pdf, created_at) VALUES"
               " ('GN-2026-000001', 'kira_sozlesmesi', 'ABCD-1234', 'x', '{}', ?, '2026-10-15')",
               (os.urandom(3000) + b"%PDF",))
    return db, path, dict(owner=owner, agent=agent)


def restore(file: Path, passphrase: str = PASS) -> tuple[dict, sqlite3.Connection]:
    out = io.BytesIO()
    with open(file, "rb") as f:
        meta = backup.decrypt_stream(f, passphrase, out)
    target = file.parent / ("restored-" + file.stem + ".db")
    target.write_bytes(out.getvalue())
    return meta, sqlite3.connect(target)


# ---------------------------------------------------------------- şifreleme ve geri yükleme
def test_encrypted_backup_roundtrip(env):
    db, path, ids = env
    res = backup.run_backup(db, make_cfg(path), now=datetime(2026, 10, 15, 3, 0, tzinfo=TZ))
    assert res.ok and res.encrypted and res.key_source == "BACKUP_PASSPHRASE"
    assert res.file == "gn-yedek-2026-10-15-0300.gnbak" and res.destinations["yerel"] == "ok"
    assert res.counts["properties"] == 1 and res.counts["documents"] == 1
    file = Path(make_cfg(path).backup_dir) / res.file
    raw = file.read_bytes()
    assert raw.startswith(backup.MAGIC)
    assert b"Ahmet" not in raw and b"10000000146" not in raw and b"Yahya" not in raw   # düz metin sızmaz
    meta, con = restore(file)
    assert meta["cipher"] == "AES-256-GCM" and meta["format"] == "sqlite3+gzip"
    assert con.execute("SELECT tenant_name, tenant_tc FROM properties").fetchone() == ("Ahmet Yılmaz", "10000000146")
    original = sqlite3.connect(path).execute("SELECT pdf FROM documents").fetchone()[0]
    assert con.execute("SELECT pdf FROM documents").fetchone()[0] == original                # BLOB birebir
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert con.execute("PRAGMA journal_mode").fetchone()[0] == "delete"                      # tek dosya
    assert db.get_setting("backup_last_ok") == res.created_at
    assert backup.last_result(db)["ok"] is True
    assert db.one("SELECT 1 FROM activity WHERE action='yedek_aldi'")


def test_wrong_passphrase_tamper_truncation_and_reorder_are_detected(env, monkeypatch):
    db, path, _ = env
    monkeypatch.setattr(backup, "CHUNK", 1024)                # birden çok parça üret
    res = backup.run_backup(db, make_cfg(path))
    file = Path(make_cfg(path).backup_dir) / res.file
    data = file.read_bytes()
    with pytest.raises(backup.BackupError, match="Parola yanlış"):
        backup.decrypt_stream(io.BytesIO(data), "yanlış parola", io.BytesIO())
    flipped = bytearray(data)
    flipped[-40] ^= 0x01
    with pytest.raises(backup.BackupError):
        backup.decrypt_stream(io.BytesIO(bytes(flipped)), PASS, io.BytesIO())
    # son parçayı at: kalan dosya "son parça" olarak doğrulanamaz
    f = io.BytesIO(data)
    backup.read_header(f)
    records = []
    while True:
        n = f.read(4)
        if not n:
            break
        size = int.from_bytes(n, "big")
        records.append(n + f.read(size))
    header = data[: len(data) - sum(len(r) for r in records)]
    assert len(records) >= 3
    with pytest.raises(backup.BackupError):
        backup.decrypt_stream(io.BytesIO(header + b"".join(records[:-1])), PASS, io.BytesIO())
    with pytest.raises(backup.BackupError):
        swapped = records[:]
        swapped[0], swapped[1] = swapped[1], swapped[0]
        backup.decrypt_stream(io.BytesIO(header + b"".join(swapped)), PASS, io.BytesIO())
    with pytest.raises(backup.BackupError):
        backup.decrypt_stream(io.BytesIO(data + records[-1]), PASS, io.BytesIO())
    with pytest.raises(backup.BackupError, match="yedeği değil"):
        backup.decrypt_stream(io.BytesIO(b"SQLite format 3\x00" + b"0" * 64), PASS, io.BytesIO())
    out = io.BytesIO()
    backup.decrypt_stream(io.BytesIO(data), PASS, out)          # bozulmamış dosya açılır
    assert out.getvalue().startswith(b"SQLite format 3")


def test_without_passphrase_backup_stays_local_and_unsent(env, monkeypatch):
    db, path, ids = env
    sent = []
    monkeypatch.setattr("emlak_bot.messenger.send_telegram_document_sync", lambda *a, **k: sent.append(a) or True)
    cfg = make_cfg(path, backup_passphrase="", bot_token="T", owner_ids=frozenset({111}))
    res = backup.run_backup(db, cfg)
    assert res.ok and not res.encrypted and res.file.endswith(".db.gz")
    assert "atlandı" in res.destinations["telegram"] and not sent
    import gzip
    assert gzip.decompress((Path(cfg.backup_dir) / res.file).read_bytes()).startswith(b"SQLite format 3")
    # SECRET_KEY ortamdan geldiyse onunla şifrelenir
    res2 = backup.run_backup(db, make_cfg(path, backup_passphrase="", secret_key_from_env=True, secret_key="S3cr3t"))
    assert res2.encrypted and res2.key_source == "SECRET_KEY"
    restore(Path(cfg.backup_dir) / res2.file, "S3cr3t")


def test_backup_goes_only_to_owner_private_chats_in_parts(env, monkeypatch):
    db, path, ids = env
    calls = []
    monkeypatch.setattr("emlak_bot.messenger.send_telegram_document_sync",
                        lambda token, chat, name, data, caption="", silent=False: calls.append(
                            (chat, name, len(data), caption, silent)) or True)
    monkeypatch.setattr(backup, "TELEGRAM_PART", 4000)
    cfg = make_cfg(path, bot_token="T", owner_ids=frozenset({111, 999}), chat_id="-100777", group_chat_id="-100777")
    assert backup.telegram_targets(db, cfg) == ["111", "999"]            # grup sohbeti (-100…) asla
    res = backup.run_backup(db, cfg, now=datetime(2026, 10, 15, 3, 0, tzinfo=TZ))
    assert res.ok and res.destinations["telegram"] == "ok"
    chats = {c[0] for c in calls}
    assert chats == {"111", "999"} and all(c[4] for c in calls)           # sessiz bildirim
    parts = [c for c in calls if c[0] == "111"]
    assert len(parts) >= 2 and parts[0][1].endswith(f".part1of{len(parts)}")
    assert "gece yedeği" in parts[0][3] and "decrypt" in parts[0][3]
    # parçaları birleştirip CLI ile geri yükle
    folder = Path(cfg.backup_dir)
    data = (folder / res.file).read_bytes()
    for i in range(len(parts)):
        (folder / f"{res.file}.part{i + 1}of{len(parts)}").write_bytes(data[i * 4000:(i + 1) * 4000])
    monkeypatch.setenv("BACKUP_PASSPHRASE", PASS)
    out = folder / "cli.db"
    assert backup.main(["decrypt", str(folder / f"{res.file}.part1of{len(parts)}"), "-o", str(out)]) == 0
    with sqlite3.connect(out) as conn:
        assert conn.execute("SELECT COUNT(*) FROM properties").fetchone()[0] == 1
    assert backup.main(["decrypt", str(folder / res.file), "-o", str(out)]) == 2      # üzerine yazmaz
    assert backup.main(["verify", str(folder / res.file)]) == 0
    assert backup.telegram_targets(db, make_cfg(path, backup_chat_id="555")) == ["555"]


def test_failure_alerts_owner_and_shows_in_agenda(env, monkeypatch):
    db, path, ids = env
    texts = []
    monkeypatch.setattr("emlak_bot.messenger.send_telegram_sync", lambda token, chat, text, url_buttons=None:
                        texts.append((chat, text)) or True)
    monkeypatch.setattr("emlak_bot.messenger.send_telegram_document_sync", lambda *a, **k: False)   # Telegram düştü
    cfg = make_cfg(path, bot_token="T", owner_ids=frozenset({111}))
    res = backup.run_backup(db, cfg)
    assert not res.ok and "gönderilemedi" in res.error
    assert texts and texts[0][0] == "111" and "Gece yedeği alınamadı" in texts[0][1]
    assert db.one("SELECT 1 FROM activity WHERE action='yedek_hatasi'")
    app = create_app(db, cfg)
    db.set_credentials(ids["owner"], "diren", generate_password_hash("diren-pass-1"))
    c = app.test_client()
    login(c, "diren")
    page = c.get("/takip").get_data(as_text=True)
    assert "Son yedekleme başarısız oldu" in page


def test_retention_keeps_newest(env):
    db, path, _ = env
    cfg = make_cfg(path, backup_keep=3)
    for h in range(5):
        backup.run_backup(db, cfg, now=datetime(2026, 10, 10 + h, 3, 0, tzinfo=TZ))
    names = [f["name"] for f in backup.list_local(cfg)]
    assert names == ["gn-yedek-2026-10-14-0300.gnbak", "gn-yedek-2026-10-13-0300.gnbak", "gn-yedek-2026-10-12-0300.gnbak"]


def test_sigv4_matches_aws_documented_example():
    # AWS S3 belgelerindeki "GET Object" örneği (Signature Version 4, başlıkta imza)
    headers = backup.sigv4_headers(
        "GET", "https://examplebucket.s3.amazonaws.com/test.txt", region="us-east-1",
        access_key="AKIAIOSFODNN7EXAMPLE", secret_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        payload_sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        now=datetime(2013, 5, 24, tzinfo=timezone.utc), extra={"Range": "bytes=0-9"})
    assert headers["Authorization"] == (
        "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request, "
        "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date, "
        "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41")


def test_wal_mode_enabled(tmp_path):
    Database(str(tmp_path / "w.db"))
    assert sqlite3.connect(tmp_path / "w.db").execute("PRAGMA journal_mode").fetchone()[0] == "wal"


# ---------------------------------------------------------------- web paneli ve denetim izi
def login(client, user):
    html = client.get("/login").get_data(as_text=True)
    token = re.search(r'name="csrf" value="([^"]+)"', html).group(1)
    client.post("/login", data={"username": user, "password": f"{user}-pass-1", "csrf": token})
    return re.search(r'name="csrf-token" content="([^"]+)"', client.get("/").get_data(as_text=True)).group(1)


@pytest.fixture
def web(env):
    db, path, ids = env
    for uid, user in ((ids["owner"], "diren"), (ids["agent"], "burkay")):
        db.set_credentials(uid, user, generate_password_hash(f"{user}-pass-1"))
    cfg = make_cfg(path)
    app = create_app(db, cfg)
    app.config["TESTING"] = True
    return app, db, cfg, ids


def test_backup_page_manual_run_and_download_owner_only(web):
    app, db, cfg, ids = web
    c = app.test_client()
    token = login(c, "diren")
    page = c.get("/yedek").get_data(as_text=True)
    assert "Yedekleme &amp; Güvenlik" in page or "Yedekleme & Güvenlik" in page
    r = c.post("/yedek/simdi", data={"csrf": token}, follow_redirects=True)
    assert "Yedek alındı" in r.get_data(as_text=True)
    name = backup.list_local(cfg)[0]["name"]
    dl = c.get(f"/yedek/indir/{name}")
    assert dl.status_code == 200 and dl.data.startswith(backup.MAGIC)
    assert db.one("SELECT 1 FROM activity WHERE action='yedek_indirdi'")
    assert c.get("/yedek/indir/..%2Fofis.db").status_code == 404
    assert c.get("/yedek/indir/gn-yedek-2026-01-01-0300.gnbak").status_code == 404
    a = app.test_client()
    login(a, "burkay")
    assert a.get("/yedek").status_code == 403 and a.get(f"/yedek/indir/{name}").status_code == 403


def test_audit_beacon_masks_numbers_and_failed_logins_are_logged(web):
    app, db, cfg, ids = web
    c = app.test_client()
    token = login(c, "burkay")
    r = c.post("/api/iz", data={"csrf": token, "kind": "tel", "target": "+90 532 111 22 33", "page": "/mulk/1"})
    assert r.get_json() == {"ok": True}
    row = db.one("SELECT * FROM activity WHERE action='telefon_aradi'")
    assert row["user_id"] == ids["agent"] and row["detail"] == "0532 *** ** 33 · /mulk/1"
    c.post("/api/iz", data={"csrf": token, "kind": "wa", "target": "905321112233", "page": "https://evil"})
    assert db.one("SELECT detail FROM activity WHERE action='whatsapp_acti'")["detail"] == "0532 *** ** 33 · -"
    assert c.post("/api/iz", data={"csrf": token, "kind": "sms"}).status_code == 400
    assert c.post("/api/iz", data={"kind": "tel"}).status_code == 400          # CSRF'siz reddedilir
    x = app.test_client()
    html = x.get("/login").get_data(as_text=True)
    tok = re.search(r'name="csrf" value="([^"]+)"', html).group(1)
    x.post("/login", data={"username": "diren", "password": "yanlis", "csrf": tok})
    assert db.one("SELECT * FROM activity WHERE action='giris_basarisiz'")["user_id"] == ids["owner"]
    o = app.test_client()
    login(o, "diren")
    audit = o.get("/yedek").get_data(as_text=True)
    assert "0532 *** ** 33" in audit and "Hatalı şifre denemesi" in audit
