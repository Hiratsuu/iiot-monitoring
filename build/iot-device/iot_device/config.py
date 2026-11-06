"""Configuration for the IIoT Smart Controller.

Baseline values are intentionally weak where the attack surface requires it
(hardcoded secret key, weak session cookie flags).  Deployed values are
supplied through /etc/iot-device/iot-device.env by the systemd unit.
"""

import os
from datetime import timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = "/var/lib/iot-device/iot.db"


def _default_db_path() -> str:
    """Use the documented production path when possible, else a local one.

    This keeps ``python app.py`` usable on a developer laptop without touching
    /var/lib.  Deployment always sets DB_PATH explicitly.
    """
    parent = Path(DEFAULT_DB_PATH).parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
        if os.access(parent, os.W_OK):
            return DEFAULT_DB_PATH
    except OSError:
        pass

    local = BASE_DIR / "data"
    local.mkdir(parents=True, exist_ok=True)
    return str(local / "iot.db")


class Config:
    # V4: hardcoded fallback secret key, shared by every install.
    SECRET_KEY = os.environ.get("SECRET_KEY", "iot-device-secret-2026")
    DB_PATH = os.environ.get("DB_PATH") or _default_db_path()
    BIND_ADDR = os.environ.get("BIND_ADDR", "127.0.0.1")
    PORT = int(os.environ.get("PORT", "8080"))
    GPIO_ENABLED = os.environ.get("GPIO_ENABLED", "0")
    FIRMWARE = os.environ.get("FIRMWARE", "1.0.0")
    DEVICE_NAME = os.environ.get("DEVICE_NAME", "IIoT-SmartController-01")

    # V4: session cookie without HttpOnly / Secure / SameSite, long lifetime.
    SESSION_COOKIE_HTTPONLY = False
    SESSION_COOKIE_SECURE = False
    SESSION_COOKIE_SAMESITE = None
    PERMANENT_SESSION_LIFETIME = timedelta(days=31)

    # Baseline deliberately uses the Flask development server (V7).
    DEBUG = False
    TESTING = False
