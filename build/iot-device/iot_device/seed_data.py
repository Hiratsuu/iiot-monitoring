"""Seed data for the IIoT Smart Controller baseline.

Kept inside the package so the application factory never depends on a
top-level script import.  Seeding is one-shot: a ``seeded`` marker in the
settings table stops later calls from touching an existing database, so
mitigations applied at runtime (for example a changed admin password) survive
service restarts.  See audit C-02.
"""

import time

from .db import get_db, get_setting, init_schema, set_setting, utc_now

SEED_MARKER = "seeded"


def seed(app, *, force: bool = False) -> bool:
    """Insert baseline rows once and return True when they were written.

    A database that already carries the ``seeded`` marker is left untouched.
    ``force=True`` re-runs the inserts, but they use ``INSERT OR IGNORE`` so
    existing rows (and runtime changes such as a mitigated password) are kept;
    only missing baseline rows are added.  It is reserved for the installer
    against a brand-new database.
    """
    with app.app_context():
        init_schema()
        if not force and get_setting(SEED_MARKER) == "1":
            return False

        db = get_db()
        now = utc_now()

        users = [
            ("admin", "admin", "admin"),
            ("operator", "operator123", "operator"),
        ]
        for username, password, role in users:
            db.execute(
                "INSERT OR IGNORE INTO users (username, password, role, created_at)"
                " VALUES (?, ?, ?, ?)",
                (username, password, role, now),
            )

        devices = [
            ("led-1", "led", "Ruang Server", "off", 0),
            ("relay-1", "relay", "Kipas Exhaust", "off", 0),
            ("buzzer-1", "buzzer", "Pintu Ruang Server", "off", 0),
            ("sensor-suhu", "sensor", "Ruang Server", "on", 27.0),
        ]
        for name, dtype, location, state, value in devices:
            db.execute(
                "INSERT OR IGNORE INTO devices"
                " (name, type, location, state, value, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (name, dtype, location, state, value, now),
            )

        settings = {
            "firmware": app.config["FIRMWARE"],
            "device_name": app.config["DEVICE_NAME"],
            "boot_ts": str(int(time.time())),
        }
        for key, value in settings.items():
            db.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (key, value),
            )

        db.commit()
        set_setting(SEED_MARKER, "1")
        return True
