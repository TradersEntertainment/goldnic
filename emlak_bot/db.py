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
"""

EDITABLE_PROPERTY_COLUMNS = {
    "title", "landlord_name", "landlord_phone", "tenant_name", "tenant_phone",
    "rent", "renewal_date", "last_increase_pct", "last_increase_amount",
    "planned_pct", "planned_rent", "notes", "status", "agent_id",
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

    def mark_reminder(self, pid: int, due: str, stage: int) -> None:
        self.execute(
            "INSERT OR IGNORE INTO reminder_log (property_id, due_date, stage, sent_at)"
            " VALUES (?,?,?,?)",
            (pid, due, stage, now_iso()),
        )

    def ack_reminders(self, pid: int, due: str) -> None:
        self.execute(
            "UPDATE reminder_log SET acked=1 WHERE property_id=? AND due_date=?", (pid, due)
        )

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
