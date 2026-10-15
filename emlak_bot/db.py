"""SQLite veri katmanı."""
from __future__ import annotations

import secrets
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime
from typing import Any, Iterable, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT NOT NULL DEFAULT 'telegram',
    chat_id TEXT NOT NULL,
    name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('owner','agent')),
    can_see_all INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    last_active TEXT,
    username TEXT UNIQUE,
    password_hash TEXT,
    UNIQUE (channel, chat_id)
);
CREATE TABLE IF NOT EXISTS invites (
    code TEXT PRIMARY KEY,
    created_by INTEGER NOT NULL,
    used_by INTEGER,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS properties (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id INTEGER NOT NULL REFERENCES users(id),
    title TEXT NOT NULL,
    landlord_name TEXT,
    landlord_phone TEXT,
    tenant_name TEXT,
    tenant_phone TEXT,
    rent REAL NOT NULL,
    renewal_date TEXT NOT NULL,
    last_increase_pct REAL,
    last_increase_amount REAL,
    planned_pct REAL,
    planned_rent REAL,
    notes TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    last_contacted_at TEXT,
    last_contacted_due TEXT,
    last_contacted_notes TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rent_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    property_id INTEGER NOT NULL REFERENCES properties(id),
    effective_date TEXT NOT NULL,
    old_rent REAL NOT NULL,
    new_rent REAL NOT NULL,
    pct REAL NOT NULL,
    created_by INTEGER,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reminder_log (
    property_id INTEGER NOT NULL,
    due_date TEXT NOT NULL,
    stage INTEGER NOT NULL,
    sent_at TEXT NOT NULL,
    acked INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (property_id, due_date, stage)
);
CREATE TABLE IF NOT EXISTS activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    property_id INTEGER,
    action TEXT NOT NULL,
    detail TEXT,
    ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sahibinden_listings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    classified_id TEXT UNIQUE,
    title TEXT NOT NULL,
    normalized_title TEXT NOT NULL,
    url TEXT NOT NULL,
    image_url TEXT,
    price REAL,
    price_str TEXT,
    listing_type TEXT NOT NULL DEFAULT 'kiralik',
    location TEXT,
    publish_date TEXT,
    consultant_name TEXT NOT NULL,
    consultant_phone TEXT,
    consultant_id INTEGER REFERENCES users(id),
    property_id INTEGER REFERENCES properties(id),
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    republish_count INTEGER NOT NULL DEFAULT 1,
    deal_date TEXT,
    deal_notes TEXT,
    deal_rent REAL,
    deal_tenant_name TEXT,
    deal_tenant_phone TEXT,
    status TEXT NOT NULL DEFAULT 'active'
);
CREATE INDEX IF NOT EXISTS ix_shbdn_norm ON sahibinden_listings(consultant_name, listing_type, normalized_title);
-- Türk kira hukuku modülü
CREATE TABLE IF NOT EXISTS cpi_rates (
    period TEXT PRIMARY KEY,             -- veri ayı (YYYY-MM)
    avg12 REAL NOT NULL,                 -- 12 aylık ortalamalara göre TÜFE değişimi (%)
    annual REAL,                         -- yıllık değişim (%), biliniyorsa
    announced_on TEXT NOT NULL,          -- TÜİK açıklama tarihi
    source TEXT NOT NULL,                -- tuik | evds | manual | builtin
    fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS eviction_commitments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    property_id INTEGER NOT NULL REFERENCES properties(id),
    tenant_name TEXT,
    signed_date TEXT NOT NULL,           -- taahhütnamenin düzenlenme (imza) tarihi
    eviction_date TEXT NOT NULL,         -- taahhüt edilen tahliye tarihi
    notice_date TEXT,                    -- TBK 353 yazılı bildirim (ihtarname) tarihi
    status TEXT NOT NULL DEFAULT 'active',
    action_date TEXT,                    -- icra takibi / dava / tahliye tarihi
    notes TEXT,
    created_by INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_evict_prop ON eviction_commitments(property_id);
CREATE TABLE IF NOT EXISTS legal_alert_log (
    property_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    ref TEXT NOT NULL,
    stage TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    PRIMARY KEY (property_id, kind, ref, stage)
);
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_no TEXT UNIQUE NOT NULL,         -- GN-2026-000042
    property_id INTEGER REFERENCES properties(id),
    kind TEXT NOT NULL,                  -- kira_sozlesmesi | tahliye_taahhutnamesi | teslim_tutanagi
    verify_code TEXT NOT NULL,           -- belgede basılı doğrulama kodu (HMAC)
    sha256 TEXT NOT NULL,                -- PDF dosyasının özeti
    payload TEXT NOT NULL,               -- belgede kullanılan alanlar (JSON)
    pdf BLOB NOT NULL,
    created_by INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_documents_prop ON documents(property_id);
-- Müşteri Talep Havuzu (Matching CRM)
CREATE TABLE IF NOT EXISTS client_demands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    phone TEXT,
    deal_type TEXT NOT NULL DEFAULT 'kiralik',   -- kiralik | satilik
    category TEXT NOT NULL DEFAULT 'konut',       -- konut | isyeri | arsa
    kinds TEXT,                                   -- daire,rezidans (boş: kategorideki her tür)
    rooms TEXT,                                   -- 3+1,2+1
    rooms_min INTEGER,                            -- en az yatak odası ("3+1 ve üzeri" → 3)
    budget_min REAL,
    budget_max REAL,
    districts TEXT,                               -- izmit,kartepe
    hoods TEXT,                                   -- yahya kaptan,alikahya
    amenities TEXT,                               -- site,otopark,!esyali (! = istenmiyor)
    urgency TEXT NOT NULL DEFAULT 'normal',       -- acil | normal | esnek
    move_date TEXT,
    source TEXT,
    notes TEXT,                                   -- müşterinin isteği (serbest metin)
    status TEXT NOT NULL DEFAULT 'active',        -- active | paused | won | lost
    agent_id INTEGER REFERENCES users(id),
    created_by INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT,
    last_contact_at TEXT,
    closed_at TEXT,
    close_note TEXT
);
CREATE INDEX IF NOT EXISTS ix_demands_status ON client_demands(status, agent_id);
CREATE TABLE IF NOT EXISTS demand_matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    demand_id INTEGER NOT NULL REFERENCES client_demands(id) ON DELETE CASCADE,
    target_type TEXT NOT NULL,                    -- listing | property
    target_id INTEGER NOT NULL,
    score INTEGER NOT NULL,
    reasons TEXT,                                 -- JSON [(metin, sınıf)]
    status TEXT NOT NULL DEFAULT 'new',           -- new | sent | interested | viewing | rejected | dismissed
    active INTEGER NOT NULL DEFAULT 1,            -- aday hâlâ uygun mu (ilan yayında / daire boş)
    created_at TEXT NOT NULL,
    updated_at TEXT,
    notified_at TEXT,
    sent_at TEXT,
    sent_by INTEGER,
    viewing_at TEXT,
    note TEXT,
    UNIQUE (demand_id, target_type, target_id)
);
CREATE INDEX IF NOT EXISTS ix_matches_target ON demand_matches(target_type, target_id);
CREATE TABLE IF NOT EXISTS demand_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    demand_id INTEGER NOT NULL REFERENCES client_demands(id) ON DELETE CASCADE,
    user_id INTEGER,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_demand_notes ON demand_notes(demand_id);
-- Kapanan işlemler (performans panosu): kiralama ve satışlar
CREATE TABLE IF NOT EXISTS deals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,                           -- kiralama | satis
    agent_id INTEGER REFERENCES users(id),
    listing_id INTEGER,
    property_id INTEGER,
    demand_id INTEGER,
    amount REAL NOT NULL,                         -- aylık kira veya satış bedeli
    fee REAL,                                     -- gerçekleşen hizmet bedeli (boşsa tahmini hesaplanır)
    deal_date TEXT NOT NULL,
    listed_on TEXT,                               -- ilan / boşalma tarihi
    days_on_market INTEGER,
    client_name TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_deals_date ON deals(deal_date);
CREATE TABLE IF NOT EXISTS legal_acks (
    property_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    ref TEXT NOT NULL,
    action TEXT NOT NULL,
    notes TEXT,
    user_id INTEGER,
    created_at TEXT NOT NULL,
    PRIMARY KEY (property_id, kind, ref)
);
"""

EDITABLE_PROPERTY_COLUMNS = {
    "title", "landlord_name", "landlord_phone", "tenant_name", "tenant_phone",
    "rent", "renewal_date", "last_increase_pct", "last_increase_amount",
    "planned_pct", "planned_rent", "notes", "status", "agent_id",
    "exit_date", "deposit_notes", "handover_notes",
    "last_contacted_at", "last_contacted_due", "last_contacted_notes",
    "lease_start_date", "lease_term_months", "property_kind", "delivery_date",
    "address", "unit_desc", "landlord_tc", "landlord_address", "landlord_iban", "tenant_tc", "tenant_address",
    "guarantor_name", "guarantor_tc", "guarantor_phone", "guarantor_address", "guarantor_limit",
    "deposit_amount", "payment_day", "usage_purpose", "property_condition", "fixtures", "special_terms",
}

# Sonradan eklenen sütunlar: eski veritabanlarına PRAGMA table_info kontrolüyle ALTER TABLE uygulanır.
MIGRATION_COLUMNS: dict[str, dict[str, str]] = {
    "users": {
        "username": "TEXT",
        "password_hash": "TEXT",
        "can_see_all": "INTEGER NOT NULL DEFAULT 0",
    },
    "properties": {
        # kiracı çıkışı ve görüşme takibi
        "exit_date": "TEXT",
        "deposit_notes": "TEXT",
        "handover_notes": "TEXT",
        "last_contacted_at": "TEXT",
        "last_contacted_due": "TEXT",
        "last_contacted_notes": "TEXT",
        # mevcut kiracıyla ilk sözleşmenin başlangıcı (5. yıl / 10. yıl hesapları için)
        "lease_start_date": "TEXT",
        "lease_term_months": "INTEGER",      # ilk sözleşme süresi (ay), boşsa 12
        "property_kind": "TEXT",             # konut | isyeri (çatılı işyeri)
        "delivery_date": "TEXT",             # kiralananın kiracıya teslim tarihi
        # resmi belge (sözleşme / taahhütname / tutanak) bilgileri
        "address": "TEXT",                   # kiralananın açık adresi
        "unit_desc": "TEXT",                 # niteliği: 3+1 daire, kat, m²
        "landlord_tc": "TEXT",
        "landlord_address": "TEXT",
        "landlord_iban": "TEXT",
        "tenant_tc": "TEXT",
        "tenant_address": "TEXT",
        "guarantor_name": "TEXT",
        "guarantor_tc": "TEXT",
        "guarantor_phone": "TEXT",
        "guarantor_address": "TEXT",
        "guarantor_limit": "REAL",
        "deposit_amount": "REAL",
        "payment_day": "INTEGER",
        "usage_purpose": "TEXT",
        "property_condition": "TEXT",
        "fixtures": "TEXT",                  # demirbaşlar (her satıra bir kalem)
        "special_terms": "TEXT",             # özel şartlar (her satıra bir madde)
    },
    "sahibinden_listings": {
        # kiralama / satış işlemi (deal_rent satılıkta satış bedelini, deal_tenant_* alıcıyı tutar)
        "deal_date": "TEXT",
        "deal_notes": "TEXT",
        "deal_rent": "REAL",
        "deal_tenant_name": "TEXT",
        "deal_tenant_phone": "TEXT",
    },
}


# users.id'ye referans veren sütunlar (kullanıcı birleştirmede taşınır; modüller ekleme yapabilir)
USER_REF_COLUMNS: list[tuple[str, str]] = [
    ("properties", "agent_id"),
    ("client_demands", "agent_id"),
    ("client_demands", "created_by"),
    ("demand_matches", "sent_by"),
    ("demand_notes", "user_id"),
    ("deals", "agent_id"),
    ("eviction_commitments", "created_by"),
    ("documents", "created_by"),
    ("legal_acks", "user_id"),
    ("sahibinden_listings", "consultant_id"),
    ("activity", "user_id"),
    ("rent_history", "created_by"),
    ("invites", "created_by"),
]


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def ensure_columns(c: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    """Eksik sütunları ekler (geriye dönük uyumlu şema güncellemesi)."""
    existing = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
    for name, decl in columns.items():
        if name not in existing:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


class Database:
    def __init__(self, path: str):
        self.path = path
        with closing(self._conn()) as c:
            # WAL: web paneli, bot ve zamanlayıcı aynı anda okur/yazar; okuyucular yazanı beklemez
            try:
                c.execute("PRAGMA journal_mode=WAL")
            except sqlite3.OperationalError:
                pass
            c.executescript(SCHEMA)
            had_username = "username" in {r["name"] for r in c.execute("PRAGMA table_info(users)")}
            for table, columns in MIGRATION_COLUMNS.items():
                ensure_columns(c, table, columns)
            if not had_username:  # eski veritabanı
                c.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_users_username ON users(username)")
            c.commit()
        from .tufe import ensure_builtin
        ensure_builtin(self)  # TÜFE tavanı internet olmasa da hesaplanabilsin
        self.backfill_deals()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = ON")
        c.execute("PRAGMA synchronous = NORMAL")   # WAL ile güvenli ve hızlı
        return c

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Birden çok komutu tek işlemde (transaction) çalıştırır."""
        c = self._conn()
        try:
            yield c
            c.commit()
        finally:
            c.close()

    def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        with closing(self._conn()) as c:
            cur = c.execute(sql, tuple(params))
            c.commit()
            return cur.lastrowid or cur.rowcount

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with closing(self._conn()) as c:
            return c.execute(sql, tuple(params)).fetchall()

    def one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    # ---- kullanıcılar ----
    def add_user(self, chat_id: int | str, name: str, role: str, channel: str = "telegram") -> int:
        return self.execute(
            "INSERT INTO users (channel, chat_id, name, role, created_at) VALUES (?,?,?,?,?)",
            (channel, str(chat_id), name, role, now_iso()),
        )

    def get_user(self, user_id: int):
        return self.one("SELECT * FROM users WHERE id=?", (user_id,))

    def get_user_by_chat(self, chat_id: int | str, channel: str = "telegram"):
        return self.one(
            "SELECT * FROM users WHERE channel=? AND chat_id=?", (channel, str(chat_id))
        )

    def list_users(self, role: str | None = None, active_only: bool = True):
        sql, params = "SELECT * FROM users WHERE 1=1", []
        if role:
            sql += " AND role=?"
            params.append(role)
        if active_only:
            sql += " AND active=1"
        return self.query(sql + " ORDER BY id", params)

    def touch_user(self, user_id: int) -> None:
        self.execute("UPDATE users SET last_active=? WHERE id=?", (now_iso(), user_id))

    def deactivate_user(self, user_id: int) -> None:
        self.execute("UPDATE users SET active=0 WHERE id=?", (user_id,))

    # ---- web girişi ----
    def get_user_by_username(self, username: str):
        return self.one("SELECT * FROM users WHERE username=?", (username.lower(),))

    def set_credentials(self, user_id: int, username: str | None, password_hash: str) -> None:
        if username is None:
            self.execute("UPDATE users SET password_hash=? WHERE id=?", (password_hash, user_id))
        else:
            self.execute(
                "UPDATE users SET username=?, password_hash=? WHERE id=?",
                (username.lower(), password_hash, user_id),
            )

    def seed_admin(self, username: str, password: str, name: str = "Diren Doğan") -> int:
        from werkzeug.security import generate_password_hash

        u = self.get_user_by_username(username)
        pwd_hash = generate_password_hash(password)
        if u:
            self.execute(
                "UPDATE users SET password_hash=?, role='owner', can_see_all=1, active=1 WHERE id=?",
                (pwd_hash, u["id"]),
            )
            self.consolidate_duplicate_users()
            return u["id"]
        uid = self.execute(
            "INSERT INTO users (channel, chat_id, name, role, can_see_all, active, created_at, username, password_hash) "
            "VALUES (?, ?, ?, 'owner', 1, 1, ?, ?, ?)",
            ("web", username.lower(), name, now_iso(), username.lower(), pwd_hash),
        )
        self.consolidate_duplicate_users()
        return uid

    @staticmethod
    def _is_phantom(u) -> bool:
        """Sahibinden botunun danışman adına açtığı, giriş yapamayan yer tutucu kullanıcı."""
        return not u["password_hash"] and u["channel"] == "web" and u["chat_id"] == u["name"]

    def consolidate_duplicate_users(self) -> int:
        """Aynı isimli 'hayalet' kullanıcıları gerçek hesapla birleştirir.

        Sahibinden senkronu her danışman adı için şifresiz bir kullanıcı açar; ofis sahibi
        ADMIN_USERNAME ile ayrıca oluşturulunca aynı kişi iki kez görünür ve kendi ilanlarından
        kiralanan mülkler hayalet hesaba yazılırdı. Mülk/ilan/aktivite bağları gerçek hesaba
        taşınır, hayalet kayıt silinir. Birleştirilen kullanıcı sayısını döner.
        """
        merged = 0
        with closing(self._conn()) as c:
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for d in c.execute("SELECT name FROM users GROUP BY name HAVING COUNT(*) > 1").fetchall():
                users = c.execute(
                    "SELECT * FROM users WHERE name=? ORDER BY (password_hash IS NULL), (role != 'owner'), id",
                    (d["name"],),
                ).fetchall()
                real = [u for u in users if not self._is_phantom(u)]
                if not real:
                    continue
                keeper = real[0]
                for u in users:
                    if u["id"] == keeper["id"] or not self._is_phantom(u):
                        continue
                    for table, col in USER_REF_COLUMNS:
                        if table in tables:
                            c.execute(f"UPDATE {table} SET {col}=? WHERE {col}=?", (keeper["id"], u["id"]))
                    c.execute("DELETE FROM users WHERE id=?", (u["id"],))
                    merged += 1
            c.commit()
        return merged

    def create_or_update_agent(
        self,
        name: str,
        username: str,
        password_hash: str,
        role: str = "agent",
        can_see_all: int = 0,
    ) -> int:
        """Yeni danışman/kullanıcı ekler veya mevcut kullanıcıyı günceller."""
        u = self.get_user_by_username(username)
        if not u:
            # İsme göre kontrol et (scraper tarafından açılmış olabilir)
            u = self.one("SELECT * FROM users WHERE name=?", (name,))
        if u:
            self.execute(
                "UPDATE users SET name=?, username=?, password_hash=?, role=?, can_see_all=?, active=1 WHERE id=?",
                (name, username.lower(), password_hash, role, can_see_all, u["id"]),
            )
            self.consolidate_duplicate_users()
            return u["id"]
        uid = self.execute(
            "INSERT INTO users (channel, chat_id, name, role, can_see_all, active, created_at, username, password_hash) "
            "VALUES ('web', ?, ?, ?, ?, 1, ?, ?, ?)",
            (username.lower(), name, role, can_see_all, now_iso(), username.lower(), password_hash),
        )
        self.consolidate_duplicate_users()
        return uid

    def update_user_permission(self, user_id: int, can_see_all: int, role: str | None = None) -> None:
        """Kullanıcının herkesin ilanlarını görme yetkisini veya rolünü günceller."""
        if role:
            self.execute("UPDATE users SET can_see_all=?, role=? WHERE id=?", (can_see_all, role, user_id))
        else:
            self.execute("UPDATE users SET can_see_all=? WHERE id=?", (can_see_all, user_id))

    def set_user_active(self, user_id: int, active: int) -> None:
        """Kullanıcıyı aktif veya pasif yapar."""
        self.execute("UPDATE users SET active=? WHERE id=?", (active, user_id))

    # ---- davetler ----
    def create_invite(self, created_by: int) -> str:
        code = secrets.token_hex(4).upper()
        self.execute(
            "INSERT INTO invites (code, created_by, created_at) VALUES (?,?,?)",
            (code, created_by, now_iso()),
        )
        return code

    def pop_invite(self, code: str) -> bool:
        """Kullanılmamış davet kodunu tüketir."""
        n = self.execute(
            "UPDATE invites SET used_by=0 WHERE code=? AND used_by IS NULL", (code.upper(),)
        )
        return n == 1

    def set_invite_user(self, code: str, user_id: int) -> None:
        self.execute("UPDATE invites SET used_by=? WHERE code=?", (user_id, code.upper()))

    # ---- portföy ----
    def add_property(self, **f: Any) -> int:
        f.setdefault("created_at", now_iso())
        cols = ", ".join(f)
        marks = ", ".join("?" for _ in f)
        return self.execute(f"INSERT INTO properties ({cols}) VALUES ({marks})", f.values())

    def get_property(self, pid: int):
        return self.one("SELECT * FROM properties WHERE id=?", (pid,))

    def list_properties(self, agent_id: int | None = None, status: str = "active"):
        sql, params = "SELECT * FROM properties WHERE status=?", [status]
        if agent_id is not None:
            sql += " AND agent_id=?"
            params.append(agent_id)
        return self.query(sql + " ORDER BY renewal_date, id", params)

    def update_property(self, pid: int, **f: Any) -> None:
        bad = set(f) - EDITABLE_PROPERTY_COLUMNS
        if bad:
            raise ValueError(f"Güncellenemez alan: {bad}")
        sets = ", ".join(f"{k}=?" for k in f)
        self.execute(f"UPDATE properties SET {sets} WHERE id=?", (*f.values(), pid))
        if f.get("status") not in (None, "vacant"):
            self.deactivate_matches("property", pid)

    def transfer_properties(self, from_user: int, to_user: int) -> int:
        return self.execute(
            "UPDATE properties SET agent_id=? WHERE agent_id=? AND status='active'",
            (to_user, from_user),
        )

    def add_history(self, pid, effective_date, old, new, pct, created_by) -> None:
        self.execute(
            "INSERT INTO rent_history (property_id, effective_date, old_rent, new_rent, pct,"
            " created_by, created_at) VALUES (?,?,?,?,?,?,?)",
            (pid, effective_date, old, new, pct, created_by, now_iso()),
        )

    def history(self, pid: int):
        return self.query(
            "SELECT * FROM rent_history WHERE property_id=? ORDER BY effective_date DESC", (pid,)
        )

    # ---- hatırlatma kaydı ----
    def reminder_exists(self, pid: int, due: str, stage: int) -> bool:
        return self.one(
            "SELECT 1 FROM reminder_log WHERE property_id=? AND due_date=? AND stage=?",
            (pid, due, stage),
        ) is not None

    def is_reminder_acked(self, pid: int, due: str) -> bool:
        """Kullanıcı bu dönem için 'Müşteriyi Aradım' dedi mi / onayladı mı kontrol eder."""
        row = self.one(
            "SELECT 1 FROM reminder_log WHERE property_id=? AND due_date=? AND acked=1 LIMIT 1",
            (pid, due),
        )
        if row:
            return True
        prop = self.one("SELECT last_contacted_due FROM properties WHERE id=?", (pid,))
        if prop and prop["last_contacted_due"] == due:
            return True
        return False

    def acked_pairs(self) -> set[tuple[int, str]]:
        """'Müşteriyi aradım' denmiş (mülk, yenileme tarihi) çiftleri — panelde tek sorguda kontrol için."""
        pairs = {
            (r["property_id"], r["due_date"])
            for r in self.query("SELECT DISTINCT property_id, due_date FROM reminder_log WHERE acked=1")
        }
        for r in self.query("SELECT id, last_contacted_due FROM properties WHERE last_contacted_due IS NOT NULL"):
            pairs.add((r["id"], r["last_contacted_due"]))
        return pairs

    def mark_reminder(self, pid: int, due: str, stage: int) -> None:
        self.execute(
            "INSERT OR IGNORE INTO reminder_log (property_id, due_date, stage, sent_at)"
            " VALUES (?,?,?,?)",
            (pid, due, stage, now_iso()),
        )

    def ack_reminders(self, pid: int, due: str, notes: str = "") -> None:
        """Kullanıcının 'Müşteriyi Aradım' onayını kaydeder ve sonraki bildirimleri durdurur."""
        self.execute(
            "UPDATE reminder_log SET acked=1 WHERE property_id=? AND due_date=?", (pid, due)
        )
        self.execute(
            "INSERT OR IGNORE INTO reminder_log (property_id, due_date, stage, sent_at, acked)"
            " VALUES (?, ?, 0, ?, 1)",
            (pid, due, now_iso()),
        )
        now = now_iso()
        self.execute(
            "UPDATE properties SET last_contacted_at=?, last_contacted_due=?, last_contacted_notes=COALESCE(?, last_contacted_notes) WHERE id=?",
            (now, due, notes if notes else None, pid),
        )

    def bulk_add_properties(
        self, items: list[dict], default_agent_id: int, skip_duplicates: bool = True
    ) -> tuple[int, list[str]]:
        """Toplu mülk/kira kontratı içe aktarır. (başarılı_sayı, hatalar_listesi) döner.

        skip_duplicates: aynı başlık + aynı kiracıya sahip aktif/boş kayıt varsa (veya aynı
        dosyada iki kez geçiyorsa) satırı atlar; aynı Excel'in yanlışlıkla iki kez
        yapıştırılması portföyü çiftlemez.
        """
        from . import rent as R

        agents = {u["name"].strip().lower(): u["id"] for u in self.list_users()}
        success_count = 0
        errors: list[str] = []
        seen: dict[tuple[str, str], int] = {}
        if skip_duplicates:
            for st in ("active", "vacant"):
                for p in self.list_properties(status=st):
                    seen[(normalize_title(p["title"]), normalize_title(p["tenant_name"] or ""))] = p["id"]

        for idx, item in enumerate(items, 1):
            title = str(item.get("title") or item.get("baslik") or item.get("daire") or "").strip()
            if not title:
                errors.append(f"Satır #{idx}: Başlık/Daire bilgisi boş olamaz.")
                continue

            raw_rent = str(item.get("rent") or item.get("kira") or item.get("tutar") or "").strip()
            try:
                rent = R.parse_amount(raw_rent)
            except Exception as e:
                errors.append(f"Satır #{idx} ({title}): Kira tutarı geçersiz ({raw_rent}): {e}")
                continue

            raw_date = str(
                item.get("renewal_date")
                or item.get("tarih")
                or item.get("yenileme_tarihi")
                or item.get("sozlesme_tarihi")
                or ""
            ).strip()
            try:
                dt = R.parse_date(raw_date)
                renewal_date = dt.isoformat()
            except Exception as e:
                errors.append(f"Satır #{idx} ({title}): Tarih geçersiz ({raw_date}): {e}")
                continue

            # Danışman eşleme
            agent_id = default_agent_id
            consultant_name = str(item.get("consultant") or item.get("danisman") or "").strip().lower()
            if consultant_name and consultant_name in agents:
                agent_id = agents[consultant_name]

            landlord_name = str(item.get("landlord_name") or item.get("ev_sahibi") or "").strip() or None
            landlord_phone = str(item.get("landlord_phone") or item.get("ev_sahibi_tel") or "").strip() or None
            tenant_name = str(item.get("tenant_name") or item.get("kiraci") or "").strip() or None
            tenant_phone = str(item.get("tenant_phone") or item.get("kiraci_tel") or "").strip() or None
            notes = str(item.get("notes") or item.get("notlar") or "").strip() or None

            lease_start = None
            raw_start = str(item.get("lease_start_date") or item.get("baslangic") or "").strip()
            if raw_start:
                try:
                    lease_start = R.parse_date(raw_start).isoformat()
                except ValueError:
                    errors.append(f"Satır #{idx} ({title}): Sözleşme başlangıç tarihi okunamadı ({raw_start}); "
                                  "kayıt başlangıç tarihi olmadan eklendi.")

            key = (normalize_title(title), normalize_title(tenant_name or ""))
            if skip_duplicates and key in seen:
                errors.append(f"Satır #{idx} ({title}): Zaten kayıtlı (#{seen[key]}), tekrar eklenmedi.")
                continue

            extra, warnings = _contract_fields(item)
            errors.extend(f"Satır #{idx} ({title}): {w}" for w in warnings)
            try:
                pid = self.add_property(
                    agent_id=agent_id,
                    title=title,
                    landlord_name=landlord_name,
                    landlord_phone=landlord_phone,
                    tenant_name=tenant_name,
                    tenant_phone=tenant_phone,
                    rent=rent,
                    renewal_date=renewal_date,
                    notes=notes,
                    lease_start_date=lease_start,
                    **extra,
                )
                seen[key] = pid
                success_count += 1
            except Exception as e:
                errors.append(f"Satır #{idx} ({title}): Kayıt hatası: {e}")

        return success_count, errors

    # ---- aktivite ----
    def log(self, user_id: int, action: str, property_id: int | None = None, detail: str = ""):
        self.execute(
            "INSERT INTO activity (user_id, property_id, action, detail, ts) VALUES (?,?,?,?,?)",
            (user_id, property_id, action, detail, now_iso()),
        )

    def recent_activity(self, user_id: int | None = None, limit: int = 25):
        sql, params = (
            "SELECT a.*, u.name AS user_name FROM activity a JOIN users u ON u.id=a.user_id", [],
        )
        if user_id is not None:
            sql += " WHERE a.user_id=?"
            params.append(user_id)
        return self.query(sql + " ORDER BY a.id DESC LIMIT ?", (*params, limit))

    def activity_count_since(self, user_id: int, since_iso: str) -> int:
        row = self.one(
            "SELECT COUNT(*) AS n FROM activity WHERE user_id=? AND ts>=?", (user_id, since_iso)
        )
        return row["n"]

    # ---- ayarlar ----
    def get_setting(self, key: str) -> str | None:
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return row["value"] if row else None

    def set_setting(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO settings (key, value) VALUES (?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    # ---- sahibinden ilanları & 30 günlük tekrar yayın koruması ----
    def upsert_sahibinden_listing(self, item: dict) -> tuple[int, str]:
        """
        Sahibinden ilanını kaydeder veya günceller.
        30 günlük yenilemelerde aynı başlık + danışman eşleştiğinde mükerrer kayıt oluşturmaz,
        mevcut kaydı yeni classified_id ile günceller ve republish_count'u artırır.
        """
        now = now_iso()
        cid = str(item.get("cid") or item.get("classified_id") or "").strip()
        title = (item.get("title") or "").strip()
        norm = normalize_title(title)
        c_name = (item.get("consultant_name") or "Gold Nicomedia").strip()
        c_phone = (item.get("consultant_phone") or "").strip()
        ltype = item.get("listing_type", "kiralik")
        price = float(item.get("price") or 0.0)
        price_str = item.get("price_raw") or item.get("price_str") or f"{price:,.0f} TL"
        url = item.get("url", "")
        img = item.get("img") or item.get("image_url") or ""
        loc = item.get("location", "")
        pdate = item.get("date") or item.get("publish_date") or ""

        # Danışmanı users tablosunda bul veya oluştur (giriş yapabilen gerçek hesap öncelikli)
        agent = self.one(
            "SELECT id FROM users WHERE name=? ORDER BY (password_hash IS NULL), (role != 'owner'), id LIMIT 1",
            (c_name,),
        )
        if not agent:
            agent_id = self.add_user(chat_id=c_name, name=c_name, role="agent", channel="web")
        else:
            agent_id = agent["id"]

        # 1. Classified ID birebir eşleşiyor mu?
        existing = self.one("SELECT * FROM sahibinden_listings WHERE classified_id=?", (cid,))
        if existing:
            self.execute(
                "UPDATE sahibinden_listings SET title=?, normalized_title=?, url=?, image_url=?, price=?, "
                "price_str=?, location=?, publish_date=?, consultant_name=?, consultant_phone=?, consultant_id=?, "
                "last_seen=?, status='active' WHERE id=?",
                (title, norm, url, img, price, price_str, loc, pdate, c_name, c_phone, agent_id, now, existing["id"]),
            )
            return existing["id"], "updated"

        # 2. 30 günlük tekrar yayın tespiti:
        # Aynı danışman, aynı ilan türü ve aynı normalize edilmiş başlık var mı?
        republished = self.one(
            "SELECT * FROM sahibinden_listings WHERE consultant_name=? AND listing_type=? AND normalized_title=?",
            (c_name, ltype, norm),
        )
        if republished:
            # 30 GÜNLÜK TEKRAR YAYINLANDI! Mevcut kaydı yeni ilan numarası ile güncelle
            self.execute(
                "UPDATE sahibinden_listings SET classified_id=?, url=?, image_url=?, price=?, price_str=?, "
                "publish_date=?, last_seen=?, republish_count=republish_count+1, status='active' WHERE id=?",
                (cid, url, img, price, price_str, pdate, now, republished["id"]),
            )
            if republished["property_id"]:
                self.execute("UPDATE properties SET rent=? WHERE id=?", (price, republished["property_id"]))
            return republished["id"], "republished"

        # 3. Yepyeni bir ilan
        new_id = self.execute(
            "INSERT INTO sahibinden_listings (classified_id, title, normalized_title, url, image_url, price, "
            "price_str, listing_type, location, publish_date, consultant_name, consultant_phone, consultant_id, "
            "first_seen, last_seen, republish_count, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,'active')",
            (cid, title, norm, url, img, price, price_str, ltype, loc, pdate, c_name, c_phone, agent_id, now, now),
        )
        return new_id, "created"

    def list_sahibinden_listings(
        self,
        consultant: str | None = None,
        listing_type: str | None = None,
        search: str | None = None,
        status: str = "active",
    ) -> list[sqlite3.Row]:
        sql = "SELECT s.*, p.id AS linked_prop_id FROM sahibinden_listings s LEFT JOIN properties p ON p.id=s.property_id WHERE 1=1"
        params: list[Any] = []
        if status != "all":
            sql += " AND s.status=?"
            params.append(status)
        if consultant:
            sql += " AND s.consultant_name=?"
            params.append(consultant)
        if listing_type:
            sql += " AND s.listing_type=?"
            params.append(listing_type)
        if search:
            sql += " AND (s.title LIKE ? OR s.location LIKE ? OR s.classified_id LIKE ?)"
            pat = f"%{search}%"
            params.extend([pat, pat, pat])
        sql += " ORDER BY s.republish_count DESC, s.id DESC"
        return self.query(sql, params)

    def get_sahibinden_listing(self, lid: int):
        return self.one("SELECT * FROM sahibinden_listings WHERE id=?", (lid,))

    def get_sahibinden_stats(self, consultant: str | None = None) -> dict[str, Any]:
        where = "WHERE status='active'"
        params = [consultant] if consultant else []
        if consultant:
            where += " AND consultant_name=?"

        total = self.one(f"SELECT COUNT(*) AS c FROM sahibinden_listings {where}", params)["c"]
        kiralik = self.one(f"SELECT COUNT(*) AS c FROM sahibinden_listings {where} AND listing_type='kiralik'", params)["c"]
        satilik = self.one(f"SELECT COUNT(*) AS c FROM sahibinden_listings {where} AND listing_type='satilik'", params)["c"]
        republished = self.one(f"SELECT COUNT(*) AS c FROM sahibinden_listings {where} AND republish_count > 1", params)["c"]

        r_where = "WHERE status='rented'"
        r_params = [consultant] if consultant else []
        if consultant:
            r_where += " AND consultant_name=?"
        rented = self.one(f"SELECT COUNT(*) AS c FROM sahibinden_listings {r_where}", r_params)["c"]
        sold = self.one(
            f"SELECT COUNT(*) AS c FROM sahibinden_listings {r_where.replace('rented', 'sold')}", r_params
        )["c"]

        consultants = self.query(
            "SELECT consultant_name AS name, consultant_phone AS phone, COUNT(*) AS count "
            "FROM sahibinden_listings WHERE status='active' GROUP BY consultant_name ORDER BY count DESC"
        )
        return {
            "total": total,
            "kiralik": kiralik,
            "satilik": satilik,
            "republished": republished,
            "rented": rented,
            "sold": sold,
            "consultants": [dict(c) for c in consultants],
        }

    def link_sahibinden_to_property(self, listing_id: int, property_id: int) -> None:
        self.execute("UPDATE sahibinden_listings SET property_id=? WHERE id=?", (property_id, listing_id))

    def deactivate_matches(self, target_type: str, target_id: int) -> None:
        """İlan kapandığında / daire kiralandığında müşteri eşleşmeleri pasifleşir (tekrar önerilmez)."""
        self.execute("UPDATE demand_matches SET active=0, updated_at=? WHERE target_type=? AND target_id=? AND active=1",
                     (now_iso(), target_type, target_id))

    # ---- kapanan işlemler (performans panosu) ----
    def record_deal(self, kind: str, agent_id: int | None, amount: float, deal_date: str, *,
                    listing_id: int | None = None, property_id: int | None = None, listed_on: str | None = None,
                    client_name: str | None = None, demand_id: int | None = None) -> int:
        """Kiralama / satış işlemini kaydeder; piyasada kalma süresini (gün) hesaplar."""
        days = None
        if listed_on and deal_date:
            try:
                days = max(0, (datetime.fromisoformat(deal_date[:10]) - datetime.fromisoformat(listed_on[:10])).days)
            except ValueError:
                days = None
        return self.execute(
            "INSERT INTO deals (kind, agent_id, listing_id, property_id, demand_id, amount, deal_date, listed_on,"
            " days_on_market, client_name, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (kind, agent_id, listing_id, property_id, demand_id, amount, deal_date[:10],
             listed_on[:10] if listed_on else None, days, client_name or None, now_iso()))

    def backfill_deals(self) -> int:
        """Performans panosundan önce kapanmış ilanları işlem kaydına aktarır (bir kez, eksik olanlar)."""
        rows = self.query(
            "SELECT s.* FROM sahibinden_listings s WHERE s.status IN ('rented','sold') AND s.deal_date IS NOT NULL"
            " AND NOT EXISTS (SELECT 1 FROM deals d WHERE d.listing_id=s.id)")
        for r in rows:
            self.record_deal("kiralama" if r["status"] == "rented" else "satis", r["consultant_id"],
                             float(r["deal_rent"] or r["price"] or 0), r["deal_date"], listing_id=r["id"],
                             property_id=r["property_id"], listed_on=r["first_seen"], client_name=r["deal_tenant_name"])
        return len(rows)

    def mark_listing_rented(
        self,
        lid: int,
        agent_id: int,
        title: str,
        rent: float,
        start_date: str,
        renewal_date: str,
        tenant_name: str = "",
        tenant_phone: str = "",
        landlord_name: str = "",
        landlord_phone: str = "",
        notes: str = "",
    ) -> int:
        """İlanı 'kiralandı' yapar ve kira artış takvimine mülk olarak ekler."""
        now = now_iso()
        note_parts = [f"Sahibinden ilanı kiralandı. Başlangıç: {start_date}."]
        if notes:
            note_parts.append(notes)
        prop_id = self.add_property(
            agent_id=agent_id,
            title=title,
            rent=rent,
            renewal_date=renewal_date,
            tenant_name=tenant_name or None,
            tenant_phone=tenant_phone or None,
            landlord_name=landlord_name or None,
            landlord_phone=landlord_phone or None,
            notes=" ".join(note_parts),
            status="active",
            lease_start_date=start_date or None,
        )
        self.execute(
            "UPDATE sahibinden_listings SET status='rented', property_id=?, deal_date=?, "
            "deal_rent=?, deal_tenant_name=?, deal_tenant_phone=?, deal_notes=? WHERE id=?",
            (prop_id, start_date or now[:10], rent, tenant_name, tenant_phone, notes, lid),
        )
        listing = self.get_sahibinden_listing(lid)
        self.record_deal("kiralama", agent_id, rent, start_date or now[:10], listing_id=lid, property_id=prop_id,
                         listed_on=listing["first_seen"] if listing else None, client_name=tenant_name)
        self.deactivate_matches("listing", lid)
        return prop_id

    def mark_listing_sold(
        self,
        lid: int,
        price: float,
        deal_date: str,
        buyer_name: str = "",
        buyer_phone: str = "",
        notes: str = "",
    ) -> None:
        """Satılık ilanı 'satıldı' yapar (satış bedeli ve alıcı bilgisi deal_* alanlarına yazılır)."""
        self.execute(
            "UPDATE sahibinden_listings SET status='sold', deal_date=?, deal_rent=?, deal_tenant_name=?, "
            "deal_tenant_phone=?, deal_notes=? WHERE id=?",
            (deal_date, price, buyer_name or None, buyer_phone or None, notes or None, lid),
        )
        listing = self.get_sahibinden_listing(lid)
        if listing and not self.one("SELECT 1 FROM deals WHERE listing_id=? AND kind='satis'", (lid,)):
            self.record_deal("satis", listing["consultant_id"], price, deal_date, listing_id=lid,
                             listed_on=listing["first_seen"], client_name=buyer_name)
        self.deactivate_matches("listing", lid)

    def close_or_delete_listing(self, lid: int, action: str = "archive", reason: str = "") -> None:
        """İlanı yayından kaldırır veya tamamen siler."""
        now = now_iso()
        self.deactivate_matches("listing", lid)
        if action == "delete":
            self.execute("DELETE FROM sahibinden_listings WHERE id=?", (lid,))
        else:
            self.execute(
                "UPDATE sahibinden_listings SET status='inactive', deal_notes=? WHERE id=?",
                (f"Yayından kaldırıldı ({now[:10]}): {reason}".strip(), lid),
            )

    def mark_property_vacant(
        self,
        pid: int,
        exit_date: str,
        deposit_notes: str = "",
        handover_notes: str = "",
        next_step: str = "vacant",
    ) -> None:
        """Kiracı çıktığında mülkü boş / kiracı aranıyor durumuna alır."""
        st = "vacant" if next_step == "vacant" else "archived"
        self.execute(
            "UPDATE properties SET status=?, exit_date=?, deposit_notes=?, handover_notes=? WHERE id=?",
            (st, exit_date, deposit_notes, handover_notes, pid),
        )

    def reassign_tenant(
        self,
        pid: int,
        tenant_name: str,
        tenant_phone: str,
        rent: float,
        start_date: str,
        renewal_date: str,
        notes: str = "",
    ) -> None:
        """Boşalan mülke yeni kiracı girildiğinde aktif kira takibine döndürür."""
        old = self.get_property(pid)
        hist = f"Önceki Kiracı: {old['tenant_name'] or '-'} (Çıkış: {old['exit_date'] or '-'}). " if old else ""
        comb = (hist + (notes or "")).strip()
        self.execute(
            "UPDATE properties SET status='active', tenant_name=?, tenant_phone=?, rent=?, "
            "renewal_date=?, notes=?, exit_date=NULL, planned_pct=NULL, planned_rent=NULL, "
            "lease_start_date=?, lease_term_months=NULL, delivery_date=NULL, tenant_tc=NULL, tenant_address=NULL, guarantor_name=NULL, "
            "guarantor_tc=NULL, guarantor_phone=NULL, guarantor_address=NULL, guarantor_limit=NULL WHERE id=?",
            (tenant_name, tenant_phone, rent, renewal_date, comb, start_date or None, pid),
        )
        if old:
            self.record_deal("kiralama", old["agent_id"], rent, start_date or now_iso()[:10], property_id=pid,
                             listed_on=old["exit_date"], client_name=tenant_name)
        self.deactivate_matches("property", pid)


def _contract_fields(item: dict) -> tuple[dict[str, Any], list[str]]:
    """Toplu girişteki isteğe bağlı sözleşme/belge sütunları: doğrulanır, hatalı olan atlanıp not edilir."""
    from . import rent as R
    from .validators import clean_tc, iban_clean, iban_valid, tc_kimlik_valid

    out: dict[str, Any] = {}
    warnings: list[str] = []
    for col, label in (("tenant_tc", "Kiracı T.C."), ("landlord_tc", "Ev sahibi T.C.")):
        raw = str(item.get(col) or "").strip()
        if raw:
            if tc_kimlik_valid(raw):
                out[col] = clean_tc(raw)
            else:
                warnings.append(f"{label} geçersiz ({raw}); boş bırakıldı, kayıt eklendi.")
    raw = str(item.get("landlord_iban") or "").strip()
    if raw:
        if iban_valid(raw):
            out["landlord_iban"] = iban_clean(raw)
        else:
            warnings.append(f"IBAN geçersiz ({raw}); boş bırakıldı, kayıt eklendi.")
    raw = str(item.get("deposit_amount") or "").strip()
    if raw:
        try:
            out["deposit_amount"] = R.parse_amount(raw)
        except ValueError:
            warnings.append(f"Depozito okunamadı ({raw}).")
    raw = str(item.get("delivery_date") or "").strip()
    if raw:
        try:
            out["delivery_date"] = R.parse_date(raw).isoformat()
        except ValueError:
            warnings.append(f"Teslim tarihi okunamadı ({raw}).")
    if str(item.get("address") or "").strip():
        out["address"] = str(item["address"]).strip()
    kind = normalize_title(str(item.get("property_kind") or ""))
    if kind:
        out["property_kind"] = "isyeri" if any(w in kind for w in ("isyeri", "is yeri", "dukkan", "ofis", "magaza",
                                                                    "depo", "ticari")) else "konut"
    return out, warnings


def normalize_title(title: str) -> str:
    import re
    tr_map = str.maketrans({
        "İ": "i", "I": "i", "ı": "i", "i": "i",
        "Ğ": "g", "ğ": "g",
        "Ü": "u", "ü": "u",
        "Ş": "s", "ş": "s",
        "Ö": "o", "ö": "o",
        "Ç": "c", "ç": "c",
    })
    s = (title or "").translate(tr_map).lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return " ".join(s.split())

