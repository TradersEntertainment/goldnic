"""SQLite veri katmanı."""
from __future__ import annotations

import secrets
import sqlite3
from contextlib import closing
from datetime import datetime
from typing import Any, Iterable

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
"""

EDITABLE_PROPERTY_COLUMNS = {
    "title", "landlord_name", "landlord_phone", "tenant_name", "tenant_phone",
    "rent", "renewal_date", "last_increase_pct", "last_increase_amount",
    "planned_pct", "planned_rent", "notes", "status", "agent_id",
    "exit_date", "deposit_notes", "handover_notes",
    "last_contacted_at", "last_contacted_due", "last_contacted_notes",
}


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str):
        self.path = path
        with closing(self._conn()) as c:
            c.executescript(SCHEMA)
            cols = {r["name"] for r in c.execute("PRAGMA table_info(users)")}
            if "username" not in cols:  # eski veritabanı
                c.execute("ALTER TABLE users ADD COLUMN username TEXT")
                c.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_users_username ON users(username)")
            if "password_hash" not in cols:
                c.execute("ALTER TABLE users ADD COLUMN password_hash TEXT")
            if "can_see_all" not in cols:
                c.execute("ALTER TABLE users ADD COLUMN can_see_all INTEGER NOT NULL DEFAULT 0")

            # Mülk tahliye / kiracı çıkışı ve arama sütunları
            p_cols = {r["name"] for r in c.execute("PRAGMA table_info(properties)")}
            if "exit_date" not in p_cols:
                c.execute("ALTER TABLE properties ADD COLUMN exit_date TEXT")
            if "deposit_notes" not in p_cols:
                c.execute("ALTER TABLE properties ADD COLUMN deposit_notes TEXT")
            if "handover_notes" not in p_cols:
                c.execute("ALTER TABLE properties ADD COLUMN handover_notes TEXT")
            if "last_contacted_at" not in p_cols:
                c.execute("ALTER TABLE properties ADD COLUMN last_contacted_at TEXT")
            if "last_contacted_due" not in p_cols:
                c.execute("ALTER TABLE properties ADD COLUMN last_contacted_due TEXT")
            if "last_contacted_notes" not in p_cols:
                c.execute("ALTER TABLE properties ADD COLUMN last_contacted_notes TEXT")

            # Sahibinden kiralama / işlem sütunları
            s_cols = {r["name"] for r in c.execute("PRAGMA table_info(sahibinden_listings)")}
            if "deal_date" not in s_cols:
                c.execute("ALTER TABLE sahibinden_listings ADD COLUMN deal_date TEXT")
            if "deal_notes" not in s_cols:
                c.execute("ALTER TABLE sahibinden_listings ADD COLUMN deal_notes TEXT")
            if "deal_rent" not in s_cols:
                c.execute("ALTER TABLE sahibinden_listings ADD COLUMN deal_rent REAL")
            if "deal_tenant_name" not in s_cols:
                c.execute("ALTER TABLE sahibinden_listings ADD COLUMN deal_tenant_name TEXT")
            if "deal_tenant_phone" not in s_cols:
                c.execute("ALTER TABLE sahibinden_listings ADD COLUMN deal_tenant_phone TEXT")
            c.commit()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = ON")
        return c

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
            return u["id"]
        return self.execute(
            "INSERT INTO users (channel, chat_id, name, role, can_see_all, active, created_at, username, password_hash) "
            "VALUES (?, ?, ?, 'owner', 1, 1, ?, ?, ?)",
            ("web", username.lower(), name, now_iso(), username.lower(), pwd_hash),
        )

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
            return u["id"]
        return self.execute(
            "INSERT INTO users (channel, chat_id, name, role, can_see_all, active, created_at, username, password_hash) "
            "VALUES ('web', ?, ?, ?, ?, 1, ?, ?, ?)",
            (username.lower(), name, role, can_see_all, now_iso(), username.lower(), password_hash),
        )

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
        self, items: list[dict], default_agent_id: int
    ) -> tuple[int, list[str]]:
        """Toplu mülk/kira kontratı içe aktarır. (başarılı_sayı, hatalar_listesi) döner."""
        from . import rent as R

        agents = {u["name"].strip().lower(): u["id"] for u in self.list_users()}
        success_count = 0
        errors: list[str] = []

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

            try:
                self.add_property(
                    agent_id=agent_id,
                    title=title,
                    landlord_name=landlord_name,
                    landlord_phone=landlord_phone,
                    tenant_name=tenant_name,
                    tenant_phone=tenant_phone,
                    rent=rent,
                    renewal_date=renewal_date,
                    notes=notes,
                )
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

        # Danışmanı users tablosunda bul veya oluştur
        agent = self.one("SELECT id FROM users WHERE name=?", (c_name,))
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
            "consultants": [dict(c) for c in consultants],
        }

    def link_sahibinden_to_property(self, listing_id: int, property_id: int) -> None:
        self.execute("UPDATE sahibinden_listings SET property_id=? WHERE id=?", (property_id, listing_id))

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
        )
        self.execute(
            "UPDATE sahibinden_listings SET status='rented', property_id=?, deal_date=?, "
            "deal_rent=?, deal_tenant_name=?, deal_tenant_phone=?, deal_notes=? WHERE id=?",
            (prop_id, start_date or now[:10], rent, tenant_name, tenant_phone, notes, lid),
        )
        return prop_id

    def close_or_delete_listing(self, lid: int, action: str = "archive", reason: str = "") -> None:
        """İlanı yayından kaldırır veya tamamen siler."""
        now = now_iso()
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
            "renewal_date=?, notes=?, exit_date=NULL, planned_pct=NULL, planned_rent=NULL WHERE id=?",
            (tenant_name, tenant_phone, rent, renewal_date, comb, pid),
        )


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

