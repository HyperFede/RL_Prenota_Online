"""SQLite storage of the master (WAL mode, file readable only by the service user)."""
import os
import sqlite3
import threading
from contextlib import contextmanager

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    display_name TEXT NOT NULL,
    login_hash TEXT UNIQUE,             -- keyed HMAC of the login name chosen by the admin
    login_enc BLOB,
    username_hash TEXT UNIQUE,          -- keyed HMAC of the Telegram username (lookup without storing it in clear)
    username_enc BLOB,
    chat_hash TEXT UNIQUE,
    chat_enc BLOB,
    is_admin INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'invited',   -- invited | active | disabled
    created_at REAL NOT NULL,
    last_login_at REAL
);
CREATE TABLE IF NOT EXISTS invites (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at REAL NOT NULL,
    used_at REAL
);
CREATE TABLE IF NOT EXISTS login_requests (
    id TEXT PRIMARY KEY,                -- appears in the Telegram "approve" button
    browser_hash TEXT NOT NULL UNIQUE,  -- binds the request to the browser that started it
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    code_hash TEXT,
    device TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | approved | denied | used
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf_secret TEXT NOT NULL,
    device TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    last_seen_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    auth_at REAL NOT NULL,              -- last Telegram confirmation (sensitive actions need a recent one)
    revoked_at REAL
);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);
CREATE TABLE IF NOT EXISTS rate_limits (
    key TEXT PRIMARY KEY,
    window_start REAL NOT NULL,
    count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS searches (
    id TEXT PRIMARY KEY,                -- random, not guessable
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    status TEXT NOT NULL,               -- active | paused | waiting | error | booked | expired
    settings_json TEXT NOT NULL,
    secrets_enc BLOB,                   -- AES-GCM, wiped when the search ends
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    next_run_at REAL NOT NULL DEFAULT 0,
    last_run_at REAL,
    last_result TEXT NOT NULL DEFAULT '',
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    current_appointment TEXT NOT NULL DEFAULT '',
    effective_interval REAL
);
CREATE INDEX IF NOT EXISTS searches_user ON searches(user_id);
CREATE INDEX IF NOT EXISTS searches_due ON searches(status, next_run_at);
CREATE TABLE IF NOT EXISTS proposals (
    search_id TEXT NOT NULL REFERENCES searches(id) ON DELETE CASCADE,
    slot_id TEXT NOT NULL,
    slot_json TEXT NOT NULL,
    message_id INTEGER,
    status TEXT NOT NULL,
    session_expired INTEGER NOT NULL DEFAULT 0,
    ts REAL NOT NULL,
    PRIMARY KEY (search_id, slot_id)
);
CREATE INDEX IF NOT EXISTS proposals_message ON proposals(message_id);
CREATE TABLE IF NOT EXISTS discarded (
    search_id TEXT NOT NULL REFERENCES searches(id) ON DELETE CASCADE,
    slot_id TEXT NOT NULL,
    slot_json TEXT NOT NULL,
    ts REAL NOT NULL,
    PRIMARY KEY (search_id, slot_id)
);
CREATE TABLE IF NOT EXISTS leases (
    search_id TEXT PRIMARY KEY REFERENCES searches(id) ON DELETE CASCADE,
    lease_id TEXT NOT NULL UNIQUE,
    worker TEXT NOT NULL,
    started_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    user_id INTEGER,
    action TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path):
        self.path = path
        self._local = threading.local()
        new = not os.path.exists(path) and path != ":memory:"
        if new:
            # Create the file private before SQLite writes anything into it
            os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        conn = self._conn()
        conn.executescript(SCHEMA)  # runs in its own transaction (executescript commits first)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _conn(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 10000")
            if self.path != ":memory:":
                conn.execute("PRAGMA journal_mode = WAL")
                conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute("PRAGMA secure_delete = ON")  # overwrite deleted personal data on disk
            self._local.conn = conn
        return conn

    @contextmanager
    def tx(self):
        """One transaction (BEGIN IMMEDIATE: writers are serialized, no lost updates)."""
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    def query(self, sql, params=()):
        return self._conn().execute(sql, params).fetchall()

    def query_one(self, sql, params=()):
        return self._conn().execute(sql, params).fetchone()

    def get_setting(self, key, default=None):
        row = self.query_one("SELECT value FROM settings WHERE key = ?", (key,))
        return row["value"] if row else default

    def set_setting(self, key, value):
        with self.tx() as conn:
            conn.execute("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                         (key, str(value)))

    def audit(self, action, user_id=None, detail="", conn=None):
        sql, params = "INSERT INTO audit(ts, user_id, action, detail) VALUES (strftime('%s','now'), ?, ?, ?)", (user_id, action, detail[:500])
        if conn is not None:
            conn.execute(sql, params)
        else:
            with self.tx() as c:
                c.execute(sql, params)
