"""SQLite access, schema and audit helpers."""

import sqlite3
import threading
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

# C-16: prune the audit table every N committed events.  The check is cheap
# (an in-memory counter); the DELETE only runs when the cap is reached.
PRUNE_SAMPLE_EVERY = 50
PRUNE_BATCH_SIZE = 1000
_state_init_lock = threading.Lock()


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


def _prune_state():
    """Return the per-app prune bookkeeping dict and its lock.

    Keeping this in ``app.extensions`` (instead of module globals) means
    several apps built in one process -- the test suite does this -- do not
    share a sampling counter.
    """
    ext = current_app.extensions.setdefault("iot_device", {})
    lock = ext.get("events_lock")
    if lock is None:
        # Two first requests can race here; the module-level lock makes the
        # lazy initialisation safe (create_app() normally installs it first).
        with _state_init_lock:
            lock = ext.get("events_lock")
            if lock is None:
                lock = threading.Lock()
                ext["events_lock"] = lock
    return ext, lock


def _events_max_rows() -> int:
    """Return the validated retention cap, cached per app.

    Caching matters because this is called on every committed event: an
    invalid override must warn once, not flood the service log.
    """
    ext = current_app.extensions.setdefault("iot_device", {})
    cached = ext.get("events_max_rows")
    if cached is not None:
        return cached
    raw = current_app.config.get("EVENTS_MAX_ROWS", 10_000)
    try:
        value = int(raw)
    except (TypeError, ValueError, OverflowError):
        current_app.logger.warning(
            "invalid EVENTS_MAX_ROWS=%r; using 10000", raw
        )
        value = 10_000
    ext["events_max_rows"] = value
    return value


def _note_event_inserted() -> None:
    """Count every insert, including ``commit=False`` ones.

    Over-counting is safe: the counter only decides *when* to check, and the
    actual row count is always read from SQLite by ``prune_events``.  A
    rolled-back insert therefore cannot make the table shrink too far.
    """
    ext, lock = _prune_state()
    with lock:
        ext["events_since_prune"] = ext.get("events_since_prune", 0) + 1


def prune_events(
    max_rows: int,
    *,
    db: sqlite3.Connection | None = None,
    batch_size: int = PRUNE_BATCH_SIZE,
    protected_ids: set[int] | None = None,
) -> int:
    """Delete the oldest events so at most ``max_rows`` rows remain.

    Returns the number of deleted rows.  ``max_rows <= 0`` disables pruning.
    Deletes in batches and commits between them so a large backlog does not
    hold SQLite's write lock for the whole operation and trip the busy
    timeout of concurrent writers (C-16).

    The cutoff is the id of the ``max_rows + 1``-th newest row; deleting
    ``id <= cutoff`` keeps the newest rows and is equivalent to the plan's
    ``NOT IN (SELECT ... ORDER BY id DESC LIMIT ?)`` but batchable.  The
    ``events(ts)`` index is not used here (the plan already notes it is
    forward-looking for time-based retention).

    ``protected_ids`` are rows that must survive even when they are older
    than the cutoff (a pending buzzer timer's token).  The table may exceed
    ``max_rows`` by at most the number of protected ids until the timer
    fires; that is preferable to a buzzer that never turns off.
    """
    if max_rows <= 0:
        return 0
    conn = db or get_db()
    batch_size = max(1, int(batch_size))
    protected = {int(value) for value in (protected_ids or ())}

    row = conn.execute(
        "SELECT id FROM events ORDER BY id DESC LIMIT 1 OFFSET ?",
        (max_rows,),
    ).fetchone()
    if row is None:
        return 0
    cutoff = int(row[0])

    if protected:
        placeholders = ",".join("?" for _ in protected)
        delete_sql = (
            "DELETE FROM events WHERE id IN ("
            " SELECT id FROM events WHERE id <= ?"
            f" AND id NOT IN ({placeholders}) ORDER BY id LIMIT ?)"
        )
        delete_params = (cutoff, *protected, batch_size)
    else:
        delete_sql = (
            "DELETE FROM events WHERE id IN ("
            " SELECT id FROM events WHERE id <= ? ORDER BY id LIMIT ?)"
        )
        delete_params = (cutoff, batch_size)

    deleted = 0
    while True:
        cur = conn.execute(delete_sql, delete_params)
        rows = cur.rowcount
        conn.commit()
        deleted += rows
        if rows < batch_size:
            break
    return deleted


def _maybe_prune_events(db: sqlite3.Connection) -> None:
    """Sample the event count and prune when the retention cap is reached.

    The table is trimmed to ``max_rows - interval`` so it never exceeds
    ``max_rows`` between two samples: after a prune there is one full
    sampling interval of headroom, and the insert that triggers the next
    check lands exactly on the cap before the prune runs.  For caps at or
    below the sampling interval the target is floored at 1 (``prune_events``
    treats 0 as "disabled"), so those small caps can exceed the cap by one
    row until the next sample; that is preferable to never pruning.  Pruning
    is maintenance and must never fail the audit write that triggered it, so
    any error is logged and swallowed.
    """
    try:
        max_rows = _events_max_rows()
        if max_rows < 1:
            return
        interval = min(PRUNE_SAMPLE_EVERY, max_rows)

        ext, lock = _prune_state()
        with lock:
            pending = ext.get("events_since_prune", 0)
            if pending < interval:
                return
            ext["events_since_prune"] = 0

        getter = ext.get("protected_event_ids")
        protected = getter() if getter is not None else set()
        # max_rows >= 1 here; floor at 1 because prune_events(0) means
        # "disabled", not "delete everything".
        target = max(1, max_rows - interval)
        deleted = prune_events(target, db=db, protected_ids=protected)
        if deleted:
            current_app.logger.debug(
                "pruned %d audit events to keep <= %d rows", deleted, max_rows
            )
    except Exception as exc:  # noqa: BLE001 - maintenance must not break writes
        current_app.logger.warning("event pruning failed: %s", exc)


def maybe_prune_events(db: sqlite3.Connection | None = None) -> None:
    """Public retention hook for callers that commit a ``commit=False`` event.

    ``record_event(commit=True)`` already samples; the control/startup paths
    batch their audit insert into a larger transaction and commit themselves,
    so they call this immediately after their commit.
    """
    _maybe_prune_events(db or get_db())


def enforce_event_cap() -> int:
    """Trim the table to the configured cap (startup safety net).

    The sampling counter is in memory, so a restart would otherwise reset it
    and let the table grow by up to one interval per restart.  Trim to the
    same ``max_rows - interval`` target the sampler uses (floored at 1) so
    the first sampling window after a restart cannot exceed the cap either.
    This runs once at startup; normal operation is sampled.  A locked
    database must not stop the service from starting, so failures are logged
    and swallowed.
    """
    try:
        max_rows = _events_max_rows()
        if max_rows < 1:
            return 0
        interval = min(PRUNE_SAMPLE_EVERY, max_rows)
        return prune_events(max(1, max_rows - interval))
    except Exception as exc:  # noqa: BLE001 - startup must not fail on this
        current_app.logger.warning("startup event pruning failed: %s", exc)
        return 0


def record_event(
    action: str,
    *,
    username: str | None = None,
    device: str | None = None,
    source_ip: str | None = None,
    detail: str | None = None,
    commit: bool = True,
) -> int:
    """Insert an audit event and return its row id.

    The id is a monotonic token: callers can use it to make a later write
    conditional on no newer event having been recorded (C-09).  Committed
    inserts also sample the retention cap (C-16).
    """
    db = get_db()
    cur = db.execute(
        "INSERT INTO events (ts, username, device, action, source_ip, detail)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (utc_now(), username, device, action, source_ip, detail),
    )
    event_id = int(cur.lastrowid)
    _note_event_inserted()
    if commit:
        db.commit()
        _maybe_prune_events(db)
    return event_id


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
