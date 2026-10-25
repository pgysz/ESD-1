"""SQLite store: the single source of truth for both processes.

Why SQLite: two separate processes (Flask + gRPC) must see the same data, so
the A1 in-memory dict cannot be used. SQLite in WAL mode allows both processes
to read and write the same file safely, with no extra server to run.

Cross-process safety lives in the database, not in Python locks:
  * a partial UNIQUE index guarantees at most ONE open session per user, even
    if an HTTP and a gRPC check-in race each other;
  * BEGIN IMMEDIATE transactions make "look up idempotency key -> write
    result" atomic.
"""

import functools
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager

from . import config
from .metrics import STORE_OPERATION_DURATION_SECONDS

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id    TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS attendance (
    session_id       TEXT PRIMARY KEY,
    user_id          TEXT NOT NULL REFERENCES users(user_id),
    state            TEXT NOT NULL CHECK (state IN ('open', 'closed')),
    status           TEXT NOT NULL CHECK (status IN ('on_time', 'late')),
    check_in_method  TEXT NOT NULL,
    checked_in_at    REAL NOT NULL,
    checked_out_at   REAL,
    check_out_method TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS one_open_session_per_user
    ON attendance(user_id) WHERE state = 'open';
CREATE TABLE IF NOT EXISTS idempotency_keys (
    key          TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    response     TEXT NOT NULL,
    created_at   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL NOT NULL,
    type       TEXT NOT NULL,
    user_id    TEXT,
    method     TEXT,
    status     TEXT,
    reason     TEXT,
    request_id TEXT
);
"""

IDEMPOTENCY_TTL_SECONDS = 24 * 3600


class AlreadyOpenSessionError(Exception):
    """The user already has an open session (unique-index violation)."""


def timed(operation):
    """Record store_operation_duration_seconds{operation=...} for a method."""
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(self, *args, **kwargs):
            start = time.perf_counter()
            try:
                return fn(self, *args, **kwargs)
            finally:
                STORE_OPERATION_DURATION_SECONDS.labels(operation).observe(
                    time.perf_counter() - start)
        return wrapper
    return decorator


class Store:
    def __init__(self, path=None):
        self.path = path or config.db_path()
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        self._local = threading.local()      # one connection per thread
        conn = self._conn()
        conn.executescript(SCHEMA)
        for user_id, name in config.SEED_USERS:   # no-op if already present
            conn.execute(
                "INSERT OR IGNORE INTO users(user_id, name, created_at) VALUES (?, ?, ?)",
                (user_id, name, time.time()))

    # -- connection / transaction plumbing ---------------------------------

    def _conn(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    @contextmanager
    def transaction(self):
        """BEGIN IMMEDIATE ... COMMIT (ROLLBACK on error). Store methods called
        inside the block share this thread's connection, so they join it."""
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    # -- users --------------------------------------------------------------

    @timed("list_users")
    def list_users(self):
        rows = self._conn().execute(
            "SELECT user_id, name FROM users ORDER BY CAST(SUBSTR(user_id, 2) AS INTEGER)")
        return [dict(r) for r in rows]

    @timed("get_user")
    def get_user(self, user_id):
        row = self._conn().execute(
            "SELECT user_id, name FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return dict(row) if row else None

    @timed("create_user")
    def create_user(self, name, now):
        with self.transaction():
            conn = self._conn()
            number = conn.execute(
                "SELECT COALESCE(MAX(CAST(SUBSTR(user_id, 2) AS INTEGER)), 0) + 1 FROM users"
            ).fetchone()[0]
            user_id = f"u{number}"
            conn.execute("INSERT INTO users(user_id, name, created_at) VALUES (?, ?, ?)",
                         (user_id, name, now))
        return {"user_id": user_id, "name": name}

    # -- attendance sessions -----------------------------------------------

    @timed("insert_session")
    def insert_session(self, session_id, user_id, status, method, checked_in_at):
        try:
            self._conn().execute(
                "INSERT INTO attendance(session_id, user_id, state, status, check_in_method, "
                "checked_in_at) VALUES (?, ?, 'open', ?, ?, ?)",
                (session_id, user_id, status, method, checked_in_at))
        except sqlite3.IntegrityError as exc:
            if "UNIQUE" in str(exc):
                raise AlreadyOpenSessionError(user_id) from exc
            raise

    @timed("get_session")
    def get_session(self, session_id):
        row = self._conn().execute(
            "SELECT * FROM attendance WHERE session_id = ?", (session_id,)).fetchone()
        return dict(row) if row else None

    @timed("get_open_session")
    def get_open_session_for_user(self, user_id):
        row = self._conn().execute(
            "SELECT * FROM attendance WHERE user_id = ? AND state = 'open'",
            (user_id,)).fetchone()
        return dict(row) if row else None

    @timed("list_sessions")
    def list_sessions(self, state=None, user_id=None, limit=100):
        sql, params = "SELECT * FROM attendance WHERE 1=1", []
        if state:
            sql += " AND state = ?"
            params.append(state)
        if user_id:
            sql += " AND user_id = ?"
            params.append(user_id)
        sql += " ORDER BY checked_in_at DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self._conn().execute(sql, params)]

    @timed("close_session")
    def close_session(self, session_id, method, now):
        """Close an open session. Returns True if THIS call closed it."""
        cur = self._conn().execute(
            "UPDATE attendance SET state='closed', checked_out_at=?, check_out_method=? "
            "WHERE session_id = ? AND state = 'open'", (now, method, session_id))
        return cur.rowcount == 1

    def count_open(self):
        # Called by Prometheus on every scrape (see metrics.bind_store).
        return self._conn().execute(
            "SELECT COUNT(*) FROM attendance WHERE state = 'open'").fetchone()[0]

    # -- idempotency keys --------------------------------------------------

    @timed("get_idempotency")
    def get_idempotency(self, key):
        row = self._conn().execute(
            "SELECT request_hash, response FROM idempotency_keys WHERE key = ?",
            (key,)).fetchone()
        if not row:
            return None
        return {"request_hash": row["request_hash"], "response": json.loads(row["response"])}

    @timed("save_idempotency")
    def save_idempotency(self, key, request_hash, response, now):
        conn = self._conn()
        conn.execute("DELETE FROM idempotency_keys WHERE created_at < ?",
                     (now - IDEMPOTENCY_TTL_SECONDS,))
        conn.execute(
            "INSERT INTO idempotency_keys(key, request_hash, response, created_at) "
            "VALUES (?, ?, ?, ?)", (key, request_hash, json.dumps(response), now))

    # -- events (activity feed) --------------------------------------------

    @timed("add_event")
    def add_event(self, type_, now, user_id=None, method=None, status=None,
                  reason=None, request_id=None):
        self._conn().execute(
            "INSERT INTO events(ts, type, user_id, method, status, reason, request_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (now, type_, user_id, method, status, reason, request_id))

    @timed("recent_events")
    def recent_events(self, limit=20):
        rows = self._conn().execute(
            "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]
