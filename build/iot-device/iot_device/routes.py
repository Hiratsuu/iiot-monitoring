"""HTTP routes for the IIoT Smart Controller baseline."""

import os
import platform
import signal
import socket
import subprocess
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

from flask import (
    Blueprint,
    current_app,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from . import devices
from .auth import (
    admin_required,
    login_required,
    login_user,
    logout_user,
    verify_login,
)
from .db import get_db, get_setting, record_event

bp = Blueprint("routes", __name__)

PING_TIMEOUT_SECONDS = 10
OUTPUT_LIMIT = 4096


def _source_ip() -> str:
    return request.remote_addr or "-"


def _uptime_seconds() -> int:
    boot = get_setting("boot_ts")
    try:
        return max(0, int(time.time()) - int(boot))
    except (TypeError, ValueError):
        return 0


def _boot_iso() -> str:
    boot = get_setting("boot_ts")
    try:
        return datetime.fromtimestamp(int(boot), tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    except (TypeError, ValueError, OverflowError, OSError):
        return "1970-01-01T00:00:00Z"


def _safe_next(target: str | None) -> str | None:
    """Only allow local absolute paths; block scheme-relative open redirects."""
    if not target or "\\" in target or not target.startswith("/"):
        return None
    if target.startswith("//"):
        return None
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc:
        return None
    return target


# ---------------------------------------------------------------------------
# Public endpoints
# ---------------------------------------------------------------------------


@bp.get("/health")
def health():
    return jsonify(
        {
            "status": "ok",
            "device": current_app.config["DEVICE_NAME"],
            "firmware": current_app.config["FIRMWARE"],
            "uptime_s": _uptime_seconds(),
        }
    )


@bp.route("/login", methods=["GET", "POST"])
def login():
    if "user" in session:
        return redirect(url_for("routes.dashboard"))

    next_url = _safe_next(request.values.get("next"))

    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        row = verify_login(username, password)

        if row is None:
            # V3: no delay, no counter, generic message.
            record_event(
                "login_fail",
                username=username or None,
                source_ip=_source_ip(),
                detail="bad credentials",
            )
            return (
                render_template(
                    "login.html",
                    error="Username atau password salah",
                    username=username,
                    next_url=next_url,
                ),
                401,
            )

        record_event(
            "login_ok",
            username=row["username"],
            source_ip=_source_ip(),
            detail="login",
        )
        login_user(row)
        return redirect(next_url or url_for("routes.dashboard"))

    return render_template(
        "login.html", error=None, username="", next_url=next_url
    )


@bp.get("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("routes.login"))


# ---------------------------------------------------------------------------
# HTML pages
# ---------------------------------------------------------------------------


@bp.get("/")
@login_required
def dashboard():
    sensor = devices.refresh_sensor()
    rows = get_db().execute(
        "SELECT ts, username, device, action, source_ip, detail"
        " FROM events ORDER BY id DESC LIMIT 10"
    ).fetchall()
    return render_template(
        "dashboard.html",
        devices=devices.list_devices(),
        sensor=sensor,
        events=[dict(r) for r in rows],
        uptime_s=_uptime_seconds(),
    )


@bp.route("/diagnostics", methods=["GET", "POST"])
@login_required
def diagnostics():
    host = request.values.get("host", "")
    output = None
    if host:
        output, _ = _run_diagnostics(host, user=session.get("user"))
    return render_template("diagnostics.html", host=host, output=output)


@bp.get("/events")
@admin_required
def events():
    rows = get_db().execute(
        "SELECT ts, username, device, action, source_ip, detail"
        " FROM events ORDER BY id DESC LIMIT 50"
    ).fetchall()
    return render_template("events.html", events=[dict(r) for r in rows])


@bp.get("/about")
@admin_required
def about():
    return render_template("about.html", info=_about_payload())


# ---------------------------------------------------------------------------
# JSON API
# ---------------------------------------------------------------------------


@bp.get("/api/status")
@login_required
def api_status():
    sensor = devices.refresh_sensor()
    return jsonify(
        {
            "device": current_app.config["DEVICE_NAME"],
            "sensor": {
                "temp_c": sensor["value"] if sensor else None,
                "updated_at": sensor["updated_at"] if sensor else None,
            },
            "devices": devices.list_devices(),
            "uptime_s": _uptime_seconds(),
        }
    )


@bp.post("/api/control")
@login_required
def api_control():
    payload = request.get_json(silent=True)
    if payload is None:
        payload = request.form.to_dict()
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "invalid payload"}), 400

    name = payload.get("device")
    action = payload.get("action")
    if not isinstance(name, str) or not name:
        return jsonify({"ok": False, "error": "device required"}), 400
    if not isinstance(action, str) or not action:
        return jsonify({"ok": False, "error": "action required"}), 400

    value = payload.get("value")
    if value is None or value == "":
        value = None
    else:
        if isinstance(value, bool):
            return jsonify({"ok": False, "error": "value must be numeric"}), 400
        try:
            value = float(value)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "value must be numeric"}), 400

    try:
        device = devices.control_device(
            name,
            action,
            value,
            username=session.get("user"),
            source_ip=_source_ip(),
        )
    except LookupError:
        return jsonify({"ok": False, "error": "device not found"}), 404
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    return jsonify({"ok": True, "device": device})


@bp.get("/api/diagnostics")
@login_required
def api_diagnostics():
    host = request.args.get("host", "127.0.0.1")
    output, status = _run_diagnostics(host, user=session.get("user"))
    return output, status, {"Content-Type": "text/plain; charset=utf-8"}


@bp.get("/api/events")
@admin_required
def api_events():
    try:
        limit = int(request.args.get("limit", "50"))
    except ValueError:
        limit = 50
    limit = max(1, min(200, limit))
    rows = get_db().execute(
        "SELECT ts, username, device, action, source_ip, detail"
        " FROM events ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@bp.get("/api/about")
@admin_required
def api_about():
    return jsonify(_about_payload())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _terminate_process(proc: subprocess.Popen) -> None:
    try:
        if os.name == "posix":
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


def _run_diagnostics(host: str, *, user: str | None = None) -> tuple[str, int]:
    """Run ping for the supplied host.

    V1: the host is interpolated into a shell command without validation or
    quoting. ``shell=True`` makes ``; id`` and friends execute arbitrary
    commands as the service user.

    Output is read incrementally and capped at OUTPUT_LIMIT bytes so a noisy
    or malicious command cannot exhaust service memory.
    """
    cmd = f"ping -c 2 -W 2 {host}"  # noqa: S608 - intentionally vulnerable (V1)

    record_event(
        "diagnostics",
        username=user,
        source_ip=_source_ip(),
        detail=cmd[:500],
    )

    kwargs = {}
    if os.name == "posix":
        kwargs["start_new_session"] = True

    try:
        proc = subprocess.Popen(
            cmd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            **kwargs,
        )
    except OSError as exc:
        return f"[error] failed to start command: {exc}\n", 200

    state = {"chunks": [], "size": 0, "truncated": False}

    def _reader() -> None:
        try:
            while True:
                chunk = proc.stdout.read(512)
                if not chunk:
                    break
                remaining = OUTPUT_LIMIT - state["size"]
                if remaining > 0:
                    state["chunks"].append(chunk[:remaining])
                    state["size"] += min(len(chunk), remaining)
                if state["size"] >= OUTPUT_LIMIT:
                    state["truncated"] = True
                    _terminate_process(proc)
                    break
        except (ValueError, OSError):
            pass

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()

    timed_out = False
    try:
        proc.wait(timeout=PING_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_process(proc)
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
    reader.join(timeout=2)

    output = "".join(state["chunks"])
    result = output[:OUTPUT_LIMIT]
    if timed_out:
        result += "\n[timeout] command exceeded 10 seconds\n"
    if state["truncated"]:
        result += f"\n[output truncated at {OUTPUT_LIMIT} bytes]\n"

    return result, 200


def _about_payload() -> dict:
    try:
        from importlib.metadata import version

        flask_version = version("flask")
    except Exception:  # pragma: no cover
        flask_version = "unknown"

    return {
        "device": current_app.config["DEVICE_NAME"],
        "hostname": socket.gethostname(),
        "ip": current_app.config["BIND_ADDR"],
        "firmware": current_app.config["FIRMWARE"],
        "python": platform.python_version(),
        "flask": flask_version,
        "boot": _boot_iso(),
    }
