"""Virtual device model, GPIO adapter stub and sensor simulator.

All actuator state lives in SQLite so it survives service restarts.  The GPIO
adapter is optional (GPIO_ENABLED=1) and falls back to virtual mode when the
hardware libraries are unavailable.
"""

import logging
import math
import os
import random
import threading
import time
from datetime import datetime, timezone

from .db import get_db, record_event, utc_now

log = logging.getLogger(__name__)

SENSOR_NAME = "sensor-suhu"
BUZZER_AUTO_OFF_SECONDS = 5
SENSOR_REFRESH_SECONDS = 10

_gpio = None
_timers: dict[str, threading.Timer] = {}
_generations: dict[str, int] = {}
_timer_lock = threading.Lock()


def _gpio_adapter():
    """Return a GPIO adapter or None.  Stub only; never required by the app."""
    global _gpio

    if os.getenv("GPIO_ENABLED") != "1":
        return None
    if _gpio is not None:
        return _gpio

    try:
        from gpiozero import Buzzer, LED  # type: ignore

        pin_map = {"led-1": 17, "relay-1": 27, "buzzer-1": 22}
        _gpio = {
            "LED": LED,
            "Buzzer": Buzzer,
            "pin_map": pin_map,
            "handles": {},
        }
        log.info("GPIO adapter enabled (pins=%s)", pin_map)
    except Exception as exc:  # pragma: no cover - hardware only
        log.warning("GPIO requested but unavailable (%s); using virtual mode", exc)
        _gpio = None
    return _gpio


def _apply_gpio(name: str, state: str) -> None:
    adapter = _gpio_adapter()
    if not adapter:
        return
    try:
        pin = adapter["pin_map"].get(name)
        if pin is None:
            return
        handle = adapter["handles"].get(name)
        if handle is None:
            cls = adapter["Buzzer"] if name.startswith("buzzer") else adapter["LED"]
            handle = cls(pin)
            adapter["handles"][name] = handle
        if state == "on":
            handle.on()
        else:
            handle.off()
    except Exception as exc:  # pragma: no cover - hardware only
        log.warning("GPIO write failed for %s: %s", name, exc)


def list_devices(include_sensor: bool = False) -> list[dict]:
    rows = get_db().execute(
        "SELECT name, type, location, state, value, updated_at"
        " FROM devices ORDER BY id"
    ).fetchall()
    devices = [dict(row) for row in rows]
    if not include_sensor:
        devices = [d for d in devices if d["type"] != "sensor"]
    return devices


def get_device(name: str) -> dict | None:
    row = get_db().execute(
        "SELECT name, type, location, state, value, updated_at"
        " FROM devices WHERE name = ?",
        (name,),
    ).fetchone()
    return dict(row) if row else None


def _store(name: str, state: str, value: float, *, commit: bool = True) -> None:
    db = get_db()
    db.execute(
        "UPDATE devices SET state = ?, value = ?, updated_at = ? WHERE name = ?",
        (state, float(value), utc_now(), name),
    )
    if commit:
        db.commit()


def refresh_sensor() -> dict | None:
    """Update the simulated temperature if the stored value is stale."""
    device = get_device(SENSOR_NAME)
    if device is None:
        return None

    row = get_db().execute(
        "SELECT updated_at FROM devices WHERE name = ?", (SENSOR_NAME,)
    ).fetchone()
    updated = row["updated_at"] if row else None

    stale = True
    if updated:
        try:
            then = datetime.strptime(updated, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
            stale = (datetime.now(timezone.utc) - then).total_seconds() >= (
                SENSOR_REFRESH_SECONDS
            )
        except ValueError:
            stale = True

    if stale:
        value = 27.0 + 0.5 * math.sin(time.time() / 60.0) + random.uniform(-0.3, 0.3)
        _store(SENSOR_NAME, "on", round(value, 1))
        device = get_device(SENSOR_NAME)

    return device


def _cancel_buzzer_timer(name: str) -> None:
    with _timer_lock:
        timer = _timers.pop(name, None)
        _generations[name] = _generations.get(name, 0) + 1
        if timer is not None:
            timer.cancel()


def _schedule_buzzer_off(app, name: str) -> None:
    with _timer_lock:
        old = _timers.pop(name, None)
        if old is not None:
            old.cancel()
        generation = _generations.get(name, 0) + 1
        _generations[name] = generation
        timer = threading.Timer(
            BUZZER_AUTO_OFF_SECONDS,
            _buzzer_auto_off,
            args=(app, name, generation),
        )
        timer.daemon = True
        _timers[name] = timer
        timer.start()


def _buzzer_auto_off(app, name: str, generation: int) -> None:
    with app.app_context():
        try:
            with _timer_lock:
                if _generations.get(name) != generation:
                    return
                _timers.pop(name, None)

            device = get_device(name)
            if device is None or device["state"] != "on":
                return

            _store(name, "off", 0, commit=False)
            record_event(
                "control",
                device=name,
                detail="auto-off",
                commit=False,
            )
            get_db().commit()
            _apply_gpio(name, "off")
            log.info("buzzer %s auto-off after %ss", name, BUZZER_AUTO_OFF_SECONDS)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("buzzer auto-off failed: %s", exc)


def control_device(
    name: str,
    action: str,
    value: float | None = None,
    *,
    username: str | None = None,
    source_ip: str | None = None,
) -> dict:
    """Apply an action and return the updated device.

    State change and audit event are committed in one transaction.  Raises
    KeyError for unknown devices and ValueError for invalid actions.
    """
    device = get_device(name)
    if device is None:
        raise LookupError("device not found")
    if device["type"] == "sensor":
        raise ValueError("sensor is read-only")

    action = (action or "").lower()
    state = device["state"]
    new_value = float(device["value"])

    if action == "on":
        state = "on"
        new_value = 100 if device["type"] == "led" else 0
    elif action == "off":
        state = "off"
        new_value = 0
    elif action == "set" and device["type"] == "led":
        if value is None:
            raise ValueError("value required for set")
        new_value = max(0.0, min(100.0, float(value)))
        state = "on" if new_value > 0 else "off"
    else:
        raise ValueError("unknown action")

    detail = f"{action}" + (f" value={new_value:g}" if action == "set" else "")
    _store(name, state, new_value, commit=False)
    record_event(
        "control",
        username=username,
        device=name,
        source_ip=source_ip,
        detail=detail,
        commit=False,
    )
    get_db().commit()

    _apply_gpio(name, state)

    if name.startswith("buzzer"):
        if state == "on":
            from flask import current_app

            app = current_app._get_current_object()
            _schedule_buzzer_off(app, name)
        else:
            _cancel_buzzer_timer(name)

    return get_device(name)
