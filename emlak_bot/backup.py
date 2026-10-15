"""Şifreli gece yedeği ("sıfır veri kaybı").

Akış: SQLite çevrimiçi yedek API'siyle tutarlı anlık görüntü (WAL dahil) → PRAGMA integrity_check →
gzip → AES-256-GCM (anahtar: scrypt ile parola) → yerel klasör (son N yedek) + Diren Doğan'ın kişisel
Telegram sohbeti (sessiz bildirim, 45 MB'lık parçalar) + isteğe bağlı S3 / R2 uyumlu depo.

Dosya biçimi (.gnbak):
    MAGIC "GNBAK1" | sürüm (1) | tuz (16) | scrypt log2(n), r, p (3) | nonce öneki (7) | parça boyu (4) |
    meta uzunluğu (4) | meta JSON
    ardından her parça için: uzunluk (4) | AES-GCM şifreli metin
Her parçanın nonce'u = önek + sıra no (4) + son-parça bayrağı (1); başlığın SHA-256'sı ek doğrulama
verisidir (AAD). Böylece parça sırası değiştirilemez, kesik dosya ve bozulma tespit edilir.

Geri yükleme:  python -m emlak_bot.backup decrypt gn-yedek-....gnbak -o emlak.db
"""
from __future__ import annotations

import argparse
import getpass
import gzip
import hashlib
import hmac
import json
import logging
import os
import shutil
import sqlite3
import struct
import sys
import tempfile
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import quote, urlsplit

log = logging.getLogger(__name__)

MAGIC = b"GNBAK1"
VERSION = 1
CHUNK = 4 * 1024 * 1024
TELEGRAM_PART = 45 * 1024 * 1024      # Telegram bot belge sınırı 50 MB
SCRYPT = (15, 8, 1)                   # n = 2^15, r = 8, p = 1 (~32 MB bellek)
COUNT_TABLES = ("properties", "sahibinden_listings", "client_demands", "documents", "eviction_commitments",
                "rent_history", "users", "deals")
TABLE_LABELS = {"properties": "mülk", "sahibinden_listings": "ilan", "client_demands": "talep",
                "documents": "belge", "eviction_commitments": "taahhüt", "rent_history": "kira kaydı",
                "users": "kullanıcı", "deals": "işlem"}


class BackupError(Exception):
    pass


# ---------------------------------------------------------------- anahtar
def passphrase_for(cfg: Any) -> tuple[str | None, str]:
    """Şifreleme parolası ve kaynağı: BACKUP_PASSPHRASE > ortamda tanımlı SECRET_KEY > yok."""
    if getattr(cfg, "backup_passphrase", ""):
        return cfg.backup_passphrase, "BACKUP_PASSPHRASE"
    if getattr(cfg, "secret_key_from_env", False) and getattr(cfg, "secret_key", ""):
        return cfg.secret_key, "SECRET_KEY"
    return None, ""


def derive_key(passphrase: str, salt: bytes, n_log2: int = SCRYPT[0], r: int = SCRYPT[1], p: int = SCRYPT[2]) -> bytes:
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

    return Scrypt(salt=salt, length=32, n=2 ** n_log2, r=r, p=p).derive(passphrase.encode("utf-8"))


# ---------------------------------------------------------------- şifreleme
def _nonce(prefix: bytes, index: int, final: bool) -> bytes:
    return prefix + struct.pack(">I", index) + (b"\x01" if final else b"\x00")


class _Encryptor:
    def __init__(self, out: BinaryIO, passphrase: str, meta: dict):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        salt, prefix = os.urandom(16), os.urandom(7)
        meta_bytes = json.dumps(meta, ensure_ascii=False, sort_keys=True).encode()
        header = (MAGIC + bytes([VERSION]) + salt + bytes(SCRYPT) + prefix + struct.pack(">I", CHUNK)
                  + struct.pack(">I", len(meta_bytes)) + meta_bytes)
        out.write(header)
        self.out, self.prefix, self.index, self.buf = out, prefix, 0, bytearray()
        self.aad = hashlib.sha256(header).digest()
        self.aead = AESGCM(derive_key(passphrase, salt))

    def _emit(self, data: bytes, final: bool) -> None:
        ct = self.aead.encrypt(_nonce(self.prefix, self.index, final), bytes(data), self.aad)
        self.out.write(struct.pack(">I", len(ct)) + ct)
        self.index += 1

    def write(self, data: bytes) -> None:
        self.buf += data
        while len(self.buf) > CHUNK:      # son parça her zaman close() ile (boş olsa bile) yazılır
            self._emit(self.buf[:CHUNK], False)
            del self.buf[:CHUNK]

    def close(self) -> None:
        self._emit(self.buf, True)
        self.buf = bytearray()


def read_header(f: BinaryIO) -> tuple[dict, bytes, bytes, tuple[int, int, int], bytes]:
    fixed = f.read(len(MAGIC) + 1 + 16 + 3 + 7 + 4 + 4)
    if len(fixed) < len(MAGIC) + 35 or not fixed.startswith(MAGIC):
        raise BackupError("Bu dosya bir Gold Nicomedia yedeği değil (.gnbak).")
    pos = len(MAGIC)
    version = fixed[pos]
    if version != VERSION:
        raise BackupError(f"Desteklenmeyen yedek sürümü: {version}")
    salt = fixed[pos + 1: pos + 17]
    params = tuple(fixed[pos + 17: pos + 20])
    prefix = fixed[pos + 20: pos + 27]
    meta_len = struct.unpack(">I", fixed[pos + 31: pos + 35])[0]
    meta_bytes = f.read(meta_len)
    if len(meta_bytes) != meta_len:
        raise BackupError("Yedek başlığı eksik.")
    header = fixed + meta_bytes
    return json.loads(meta_bytes), salt, prefix, params, hashlib.sha256(header).digest()


def decrypt_stream(f: BinaryIO, passphrase: str, out: BinaryIO) -> dict:
    """Şifreli akışı çözer, gzip'i açar ve düz SQLite dosyasını yazar; meta döner."""
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    meta, salt, prefix, params, aad = read_header(f)
    aead = AESGCM(derive_key(passphrase, salt, *params))
    inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
    digest = hashlib.sha256()
    index = 0
    pending = f.read(4)
    finished = False
    while pending:
        if finished:
            raise BackupError("Son parçadan sonra beklenmeyen veri var (dosya değiştirilmiş).")
        if len(pending) < 4:
            raise BackupError("Yedek dosyası kesik.")
        (n,) = struct.unpack(">I", pending)
        ct = f.read(n)
        if len(ct) != n:
            raise BackupError("Yedek dosyası kesik (parça eksik).")
        pending = f.read(4)
        final = not pending
        try:
            chunk = aead.decrypt(_nonce(prefix, index, final), ct, aad)
        except InvalidTag as e:
            raise BackupError("Parola yanlış veya yedek bozulmuş/kesik (doğrulama başarısız).") from e
        data = inflater.decompress(chunk)
        digest.update(data)
        out.write(data)
        index += 1
        finished = final
    if not finished:
        raise BackupError("Yedek dosyası kesik (son parça yok).")
    tail = inflater.flush()
    digest.update(tail)
    out.write(tail)
    if meta.get("sha256") and digest.hexdigest() != meta["sha256"]:
        raise BackupError("Çözülen veritabanının özeti (SHA-256) yedekteki kayıtla uyuşmuyor.")
    return meta


# ---------------------------------------------------------------- anlık görüntü
def snapshot(db_path: str, dest: str) -> dict[str, Any]:
    """Çalışan veritabanının tutarlı kopyası (SQLite backup API; yazmalar sürerken de güvenli)."""
    src = sqlite3.connect(db_path, timeout=30)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
    finally:
        src.close()
    try:
        check = dst.execute("PRAGMA integrity_check").fetchone()[0]
        if check != "ok":
            raise BackupError(f"Veritabanı bütünlük kontrolü başarısız: {check}")
        tables = {r[0] for r in dst.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        counts = {t: dst.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in COUNT_TABLES if t in tables}
        dst.execute("PRAGMA journal_mode=DELETE")   # tek dosya: -wal/-shm olmadan taşınabilir
    finally:
        dst.close()
    return counts


def file_sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            s = f"{n:.1f}".rstrip("0").rstrip(".") if unit != "B" else str(n)
            return f"{s.replace('.', ',')} {unit}"
        n /= 1024
    return f"{n} B"


# ---------------------------------------------------------------- S3 / R2 (AWS Signature V4)
def sigv4_headers(method: str, url: str, *, region: str, access_key: str, secret_key: str, payload_sha256: str,
                  now: datetime, extra: dict[str, str] | None = None, service: str = "s3") -> dict[str, str]:
    """AWS SigV4 imzalı istek başlıkları (S3, Cloudflare R2, MinIO, Backblaze B2 S3 uyumlu uç noktalar)."""
    parts = urlsplit(url)
    amz_date, datestamp = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    headers = {"host": parts.netloc, "x-amz-content-sha256": payload_sha256, "x-amz-date": amz_date}
    headers.update({k.lower(): v for k, v in (extra or {}).items()})
    signed = ";".join(sorted(headers))
    canonical_headers = "".join(f"{k}:{' '.join(str(headers[k]).split())}\n" for k in sorted(headers))
    query = "&".join(sorted(q for q in parts.query.split("&") if q))
    canonical = "\n".join([method, quote(parts.path or "/", safe="/-_.~%"), query, canonical_headers, signed,
                           payload_sha256])
    scope = f"{datestamp}/{region}/{service}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])

    def h(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode(), hashlib.sha256).digest()

    k = h(h(h(h(("AWS4" + secret_key).encode(), datestamp), region), service), "aws4_request")
    signature = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
    out = {k_: v for k_, v in headers.items() if k_ != "host"}
    out["Authorization"] = (f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={signed}, "
                            f"Signature={signature}")
    return out


def upload_s3(cfg: Any, path: Path) -> str:
    import httpx

    base = cfg.backup_s3_url.rstrip("/")
    key = (getattr(cfg, "backup_s3_prefix", "") or "").strip("/")
    url = f"{base}/{key + '/' if key else ''}{quote(path.name)}"
    data = path.read_bytes()
    headers = sigv4_headers("PUT", url, region=cfg.backup_s3_region or "auto", access_key=cfg.backup_s3_access_key,
                            secret_key=cfg.backup_s3_secret_key, payload_sha256=hashlib.sha256(data).hexdigest(),
                            now=datetime.now(timezone.utc), extra={"content-type": "application/octet-stream"})
    r = httpx.put(url, content=data, headers=headers, timeout=120)
    if r.status_code not in (200, 201):
        raise BackupError(f"S3 yükleme hatası {r.status_code}: {r.text[:160]}")
    return url


# ---------------------------------------------------------------- hedefler
def telegram_targets(db, cfg: Any) -> list[str]:
    """Yedekler SADECE ofis sahibinin kişisel sohbetine gider (danışmanların olduğu gruba asla)."""
    explicit = getattr(cfg, "backup_chat_id", "")
    if explicit:
        return [explicit]
    out: list[str] = []
    for u in db.list_users("owner"):
        if u["channel"] == "telegram" and str(u["chat_id"]).lstrip("-").isdigit() and not str(u["chat_id"]).startswith("-"):
            out.append(str(u["chat_id"]))
    out += [str(i) for i in getattr(cfg, "owner_ids", ()) if int(i) > 0]
    return list(dict.fromkeys(out))


def backup_dir(cfg: Any) -> Path:
    d = getattr(cfg, "backup_dir", "") or str(Path(os.path.abspath(cfg.db_path)).parent / "backups")
    p = Path(d)
    p.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(p, 0o700)
    except OSError:
        pass
    return p


def list_local(cfg: Any) -> list[dict[str, Any]]:
    d = backup_dir(cfg)
    rows = []
    for f in sorted(d.glob("gn-yedek-*"), reverse=True):
        if f.suffix in (".gnbak", ".gz"):
            st = f.stat()
            rows.append({"name": f.name, "size": st.st_size,
                         "mtime": datetime.fromtimestamp(st.st_mtime, getattr(cfg, "tz", None)),
                         "encrypted": f.suffix == ".gnbak"})
    return rows


def prune(cfg: Any, keep: int) -> int:
    files = [f for f in sorted(backup_dir(cfg).glob("gn-yedek-*"), reverse=True) if f.suffix in (".gnbak", ".gz")]
    removed = 0
    for f in files[max(keep, 1):]:
        f.unlink(missing_ok=True)
        removed += 1
    return removed


# ---------------------------------------------------------------- yedek al
@dataclass
class BackupResult:
    ok: bool
    file: str = ""
    size: int = 0
    sha256: str = ""
    encrypted: bool = False
    key_source: str = ""
    counts: dict[str, int] = field(default_factory=dict)
    destinations: dict[str, str] = field(default_factory=dict)
    error: str = ""
    created_at: str = ""

    def as_json(self) -> str:
        return json.dumps(self.__dict__, ensure_ascii=False)


def counts_text(counts: dict[str, int]) -> str:
    return ", ".join(f"{counts[t]} {TABLE_LABELS[t]}" for t in ("sahibinden_listings", "properties", "client_demands",
                                                               "documents") if t in counts)


def run_backup(db, cfg: Any, *, now: datetime | None = None, send: bool = True, reason: str = "gece") -> BackupResult:
    """Yedeği alır, şifreler, saklar ve gönderir. Sonucu ayarlara yazar; hata olursa ofis sahibini uyarır."""
    tz = getattr(cfg, "tz", None)
    now = now or datetime.now(tz)
    res = BackupResult(ok=False, created_at=now.isoformat(timespec="seconds"))
    db.set_setting("backup_last_attempt", res.created_at)
    passphrase, res.key_source = passphrase_for(cfg)
    res.encrypted = bool(passphrase)
    stamp = now.strftime("%Y-%m-%d-%H%M")
    out_dir = backup_dir(cfg)
    try:
        with tempfile.TemporaryDirectory(dir=out_dir) as tmp:
            snap = os.path.join(tmp, "snapshot.db")
            res.counts = snapshot(cfg.db_path, snap)
            plain_sha = file_sha256(snap)
            size = os.path.getsize(snap)
            if passphrase:
                target = out_dir / f"gn-yedek-{stamp}.gnbak"
                meta = {"app": "Gold Nicomedia OS", "format": "sqlite3+gzip", "created": res.created_at,
                        "sha256": plain_sha, "size": size, "cipher": "AES-256-GCM", "kdf": "scrypt"}
                with open(target, "wb") as out:
                    enc = _Encryptor(out, passphrase, meta)
                    compressor = zlib.compressobj(6, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
                    with open(snap, "rb") as src:
                        for block in iter(lambda: src.read(1024 * 1024), b""):
                            enc.write(compressor.compress(block))
                    enc.write(compressor.flush())
                    enc.close()
            else:
                # Parola yoksa veri sunucudan ÇIKMAZ: sadece yerel, sıkıştırılmış kopya (yine de felaket koruması)
                target = out_dir / f"gn-yedek-{stamp}.db.gz"
                with open(snap, "rb") as src, gzip.open(target, "wb", compresslevel=6) as out:
                    shutil.copyfileobj(src, out)
            try:
                os.chmod(target, 0o600)
            except OSError:
                pass
        res.file, res.size, res.sha256 = target.name, target.stat().st_size, file_sha256(target)
        res.destinations["yerel"] = "ok"
        prune(cfg, int(getattr(cfg, "backup_keep", 14) or 14))
        if send and passphrase:
            _send(db, cfg, target, res, now)
        elif send and not passphrase:
            res.destinations["telegram"] = "atlandı: BACKUP_PASSPHRASE tanımlı değil (şifresiz yedek sunucudan çıkmaz)"
        offsite = [k for k, v in res.destinations.items() if k != "yerel" and v == "ok"]
        res.ok = True
        db.set_setting("backup_last_ok", res.created_at)
        if offsite:
            db.set_setting("backup_last_offsite", res.created_at)
    except Exception as e:  # noqa: BLE001 - yedek hatası raporlanmalı, uygulamayı düşürmemeli
        log.exception("Yedekleme başarısız")
        res.error = str(e) or e.__class__.__name__
        _alert_failure(db, cfg, res)
    db.set_setting("backup_last_result", res.as_json())
    try:
        db.log(_system_user(db), "yedek_aldi" if res.ok else "yedek_hatasi", None,
               f"{reason}: {res.file or '-'} {human_size(res.size) if res.size else ''} "
               + " ".join(f"{k}={v}" for k, v in res.destinations.items()) + (f" HATA: {res.error}" if res.error else ""))
    except Exception:  # noqa: BLE001
        log.exception("Yedek kaydı aktiviteye yazılamadı")
    return res


def _system_user(db) -> int:
    owners = db.list_users("owner")
    return owners[0]["id"] if owners else 0


def _send(db, cfg: Any, target: Path, res: BackupResult, now: datetime) -> None:
    from .messenger import send_telegram_document_sync

    token = getattr(cfg, "bot_token", "")
    chats = telegram_targets(db, cfg)
    if token and chats:
        caption = (f"🔐 <b>{getattr(cfg, 'brand_name', 'Gold Nicomedia')} gece yedeği</b> — {now:%d.%m.%Y %H:%M}\n"
                   f"Şifreli (AES-256-GCM) · {human_size(res.size)} · {counts_text(res.counts)}\n"
                   f"SHA-256: <code>{res.sha256[:16]}…</code>\n"
                   f"Geri yükleme: <code>python -m emlak_bot.backup decrypt {target.name} -o emlak.db</code>")
        data = target.read_bytes()
        parts = [data[i:i + TELEGRAM_PART] for i in range(0, len(data), TELEGRAM_PART)] or [b""]
        delivered = 0
        for chat in chats:
            ok = True
            for i, part in enumerate(parts, 1):
                name = target.name if len(parts) == 1 else f"{target.name}.part{i}of{len(parts)}"
                cap = caption if i == 1 else f"Parça {i}/{len(parts)} — {target.name}"
                ok &= send_telegram_document_sync(token, chat, name, part, caption=cap, silent=True)
            delivered += int(ok)
        res.destinations["telegram"] = "ok" if delivered == len(chats) else f"{delivered}/{len(chats)} sohbet"
        if not delivered:
            raise BackupError("Yedek Telegram'a gönderilemedi (bot token / sohbet kimliği kontrol edin).")
    elif token:
        res.destinations["telegram"] = "atlandı: ofis sahibinin Telegram sohbeti yok (OWNER_TELEGRAM_IDS / BACKUP_CHAT_ID)"
    else:
        res.destinations["telegram"] = "atlandı: TELEGRAM_BOT_TOKEN yok"
    if getattr(cfg, "backup_s3_url", "") and getattr(cfg, "backup_s3_access_key", ""):
        try:
            upload_s3(cfg, target)
            res.destinations["s3"] = "ok"
        except Exception as e:  # noqa: BLE001
            res.destinations["s3"] = f"hata: {e}"
            if res.destinations.get("telegram") != "ok":
                raise


def _alert_failure(db, cfg: Any, res: BackupResult) -> None:
    from .messenger import send_telegram_sync

    token = getattr(cfg, "bot_token", "")
    if not token:
        return
    text = (f"🚨 <b>Gece yedeği alınamadı!</b>\nHata: {res.error[:300]}\n"
            "Veri kaybı riskine karşı web panelinden “Yedekleme” sayfasını kontrol edin ve elle yedek alın.")
    for chat in telegram_targets(db, cfg):
        send_telegram_sync(token, chat, text)


def last_result(db) -> dict[str, Any] | None:
    raw = db.get_setting("backup_last_result")
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


def health(db, cfg: Any, now: datetime) -> dict[str, Any]:
    """Takip Merkezi ve yedek sayfası için durum özeti."""
    last_ok = db.get_setting("backup_last_ok")
    hours = None
    if last_ok:
        try:
            dt = datetime.fromisoformat(last_ok)
            dt = dt if dt.tzinfo else dt.replace(tzinfo=now.tzinfo)
            hours = (now - dt).total_seconds() / 3600
        except ValueError:
            hours = None
    passphrase, source = passphrase_for(cfg)
    return {"last_ok": last_ok, "hours": hours, "encrypted": bool(passphrase), "key_source": source,
            "targets": telegram_targets(db, cfg) if getattr(cfg, "bot_token", "") else [],
            "s3": bool(getattr(cfg, "backup_s3_url", "") and getattr(cfg, "backup_s3_access_key", "")),
            "last": last_result(db)}


# ---------------------------------------------------------------- komut satırı (geri yükleme)
def _read_parts(path: str) -> BinaryIO:
    """'.part1of3' verilirse kardeş parçaları birleştirir."""
    p = Path(path)
    if ".part" in p.name and "of" in p.name.rsplit(".part", 1)[1]:
        base, tail = p.name.rsplit(".part", 1)
        total = int(tail.split("of")[1])
        tmp = tempfile.TemporaryFile()
        for i in range(1, total + 1):
            part = p.with_name(f"{base}.part{i}of{total}")
            if not part.exists():
                raise BackupError(f"Eksik parça: {part.name}")
            tmp.write(part.read_bytes())
        tmp.seek(0)
        return tmp
    return open(p, "rb")


def _passphrase_cli(args) -> str:
    env = os.getenv("BACKUP_PASSPHRASE") or (os.getenv("SECRET_KEY") if args.secret_key else "")
    return env or getpass.getpass("Yedek parolası (BACKUP_PASSPHRASE): ")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m emlak_bot.backup", description="Gold Nicomedia şifreli yedek aracı")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("decrypt", help="Şifreli yedeği çöz ve SQLite dosyası olarak yaz")
    d.add_argument("file")
    d.add_argument("-o", "--out", default="emlak-geri-yuklenen.db")
    d.add_argument("--secret-key", action="store_true", help="Parola olarak SECRET_KEY ortam değişkenini kullan")
    v = sub.add_parser("verify", help="Yedeği çöz ve bütünlüğünü doğrula (dosya yazmadan)")
    v.add_argument("file")
    v.add_argument("--secret-key", action="store_true")
    sub.add_parser("run", help="Ortam değişkenlerindeki ayarlarla şimdi yedek al ve gönder")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "run":
            from .config import load_config
            from .db import Database

            cfg = load_config()
            res = run_backup(Database(cfg.db_path), cfg, reason="elle (komut satırı)")
            print(("✅ " if res.ok else "❌ ") + (res.file or res.error), res.destinations)
            return 0 if res.ok else 1
        with _read_parts(args.file) as f:
            if args.cmd == "verify":
                with tempfile.TemporaryDirectory() as tmp:
                    out_path = os.path.join(tmp, "v.db")
                    with open(out_path, "wb") as out:
                        meta = decrypt_stream(f, _passphrase_cli(args), out)
                    check = sqlite3.connect(out_path).execute("PRAGMA integrity_check").fetchone()[0]
                print(f"✅ Yedek sağlam: {meta.get('created')} · {human_size(meta.get('size', 0))} · integrity={check}")
                return 0 if check == "ok" else 2
            if os.path.exists(args.out):
                raise BackupError(f"{args.out} zaten var; üzerine yazılmaz. Başka bir -o adı verin.")
            with open(args.out, "wb") as out:
                meta = decrypt_stream(f, _passphrase_cli(args), out)
            print(f"✅ Geri yüklendi: {args.out} ({meta.get('created')}, {human_size(meta.get('size', 0))})")
            print("Uygulamada kullanmak için DB_PATH bu dosyayı gösterecek şekilde ayarlayın veya /data/emlak.db ile değiştirin.")
            return 0
    except BackupError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
