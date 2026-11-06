"""Seed data for the IIoT Smart Controller baseline.

Kept inside the package so the application factory never depends on a
top-level script import.  Idempotent: safe to run multiple times.
"""

import time

from .db import get_db, init_schema, utc_now


def seed(app) -> None:
    with app.app_context():
        init_schema()
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
