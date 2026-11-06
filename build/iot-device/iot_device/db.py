"""SQLite access, schema and audit helpers."""

import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  username   TEXT NOT NULL UNIQUE,
  password   TEXT NOT NULL,            -- plaintext (V2)
  role       TEXT NOT NULL DEFAULT 'operator',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS devices (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  name       TEXT NOT NULL UNIQUE,     -- led-1, relay-1, buzzer-1, sensor-suhu
  type       TEXT NOT NULL,            -- led|relay|buzzer|sensor
  location   TEXT,
  state      TEXT NOT NULL DEFAULT 'off',
  value      REAL NOT NULL DEFAULT 0,  -- brightness 0-100 / temperature C
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  ts         TEXT NOT NULL,
  username   TEXT,
  device     TEXT,
  action     TEXT NOT NULL,            -- login_ok|login_fail|control|diagnostics
  source_ip  TEXT,
  detail     TEXT
);

CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _enable_wal(conn: sqlite3.Connection) -> None:
    """Switch the database to WAL, tolerating a concurrent first-time switch.

    Converting a fresh database needs an exclusive lock and does not always
    invoke SQLite's busy handler, so retry briefly instead of failing the
    request.  Once the database is in WAL the pragma is a cheap no-op.
    """
    mode = None
    for attempt in range(5):
        try:
            mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            break
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) or attempt == 4:
                raise
            time.sleep(0.05 * (attempt + 1))
    if mode != "wal":
        current_app.logger.warning(
            "journal_mode is %r; WAL unavailable on this filesystem?", mode
        )


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        db_path = current_app.config["DB_PATH"]
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        # Busy handler first: timers/sensors write concurrently with requests.
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        _enable_wal(conn)
        g.db = conn
    return g.db


def close_db(exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_schema(db: sqlite3.Connection | None = None) -> None:
    conn = db or get_db()
    conn.executescript(SCHEMA)
    conn.commit()


def record_event(
    action: str,
    *,
    username: str | None = None,
    device: str | None = None,
    source_ip: str | None = None,
    detail: str | None = None,
    commit: bool = True,
) -> None:
    db = get_db()
    db.execute(
        "INSERT INTO events (ts, username, device, action, source_ip, detail)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (utc_now(), username, device, action, source_ip, detail),
    )
    if commit:
        db.commit()


def set_setting(key: str, value: str) -> None:
    db = get_db()
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
    db.commit()



def get_setting(key: str, default: str | None = None) -> str | None:
    row = get_db().execute(
        "SELECT value FROM settings WHERE key = ?", (key,)
    ).fetchone()
    return row["value"] if row else default
