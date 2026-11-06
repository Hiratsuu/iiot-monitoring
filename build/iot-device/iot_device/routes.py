"""HTTP routes for the IIoT Smart Controller baseline."""

import platform
import socket
import subprocess
import time
from datetime import datetime, timezone

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
from .auth import login_required, login_user, logout_user, verify_login
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
    except (TypeError, ValueError):
        return "1970-01-01T00:00:00Z"


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
                    "login.html", error="Username atau password salah", username=username
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
        nxt = request.args.get("next", "")
        return redirect(nxt if nxt.startswith("/") else url_for("routes.dashboard"))

    return render_template("login.html", error=None, username="")


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
@login_required
def events():
    rows = get_db().execute(
        "SELECT ts, username, device, action, source_ip, detail"
        " FROM events ORDER BY id DESC LIMIT 50"
    ).fetchall()
    return render_template("events.html", events=[dict(r) for r in rows])


@bp.get("/about")
@login_required
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
    payload = request.get_json(silent=True) or request.form
    name = payload.get("device", "")
    action = payload.get("action", "")
    value = payload.get("value")
    if value is not None:
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
@login_required
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
@login_required
def api_about():
    return jsonify(_about_payload())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_diagnostics(host: str, *, user: str | None = None) -> tuple[str, int]:
    """Run ping for the supplied host.

    V1: the host is interpolated into a shell command without validation or
    quoting.  ``shell=True`` makes ``; id`` and friends execute arbitrary
    commands as the service user.
    """
    cmd = f"ping -c 2 -W 2 {host}"  # noqa: S608 - intentionally vulnerable (V1)

    record_event(
        "diagnostics",
        username=user,
        source_ip=_source_ip(),
        detail=cmd[:500],
    )

    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=PING_TIMEOUT_SECONDS,
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        status = 200
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", "replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", "replace")
        output = stdout + stderr + "\n[timeout] command exceeded 10 seconds\n"
        status = 200

    return output[:OUTPUT_LIMIT], status


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
