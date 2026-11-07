"""HTTP routes for the IIoT Smart Controller baseline."""

import os
import platform
import select
import signal
import socket
import subprocess
import threading
import time
from contextlib import contextmanager
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

# C-06: the control API only accepts a tiny JSON/form object.  The global
# MAX_CONTENT_LENGTH still caps every other endpoint at 64 KiB.
CONTROL_MAX_BODY = 1024
CONTROL_MAX_DEVICE = 32
CONTROL_MAX_ACTION = 16


_diag_init_lock = threading.Lock()


class DiagnosticsBusy(Exception):
    """Raised when the diagnostics concurrency cap is already reached."""


def init_diagnostics_limiter(app) -> None:
    """Install the per-app diagnostics concurrency limiter (C-06).

    A ``BoundedSemaphore`` is used instead of a counter so a double release
    fails loudly during development.  The limit is per process; the baseline
    runs a single Flask dev-server process (V7), so it is the effective cap.
    """
    raw = app.config.get("DIAG_CONCURRENCY", 3)
    try:
        limit = int(raw)
    except (TypeError, ValueError, OverflowError):
        app.logger.warning("invalid DIAG_CONCURRENCY=%r; using 3", raw)
        limit = 3
    if limit < 1:
        app.logger.warning("DIAG_CONCURRENCY=%r < 1; using 1", limit)
        limit = 1
    app.extensions.setdefault("iot_device", {})["diag_semaphore"] = (
        threading.BoundedSemaphore(limit)
    )


def _diag_semaphore() -> threading.BoundedSemaphore:
    ext = current_app.extensions.setdefault("iot_device", {})
    sem = ext.get("diag_semaphore")
    if sem is None:
        # Defensive fallback for callers that register the blueprint without
        # create_app(); the factory always installs the configured limiter.
        with _diag_init_lock:
            sem = ext.get("diag_semaphore")
            if sem is None:
                sem = threading.BoundedSemaphore(3)
                ext["diag_semaphore"] = sem
    return sem


@contextmanager
def _diag_slot():
    """Hold one diagnostics slot, or raise DiagnosticsBusy immediately."""
    sem = _diag_semaphore()
    if not sem.acquire(blocking=False):
        raise DiagnosticsBusy
    try:
        yield
    finally:
        sem.release()


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
            # C-06: the failed-login audit row keeps at most 64 characters of
            # the submitted username; the login check above still uses the
            # full value.
            record_event(
                "login_fail",
                username=username[:64] or None,
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
        try:
            with _diag_slot():
                output, _ = _run_diagnostics(host, user=session.get("user"))
        except DiagnosticsBusy:
            return (
                render_template(
                    "diagnostics.html",
                    host=host,
                    output="[busy] terlalu banyak permintaan diagnostics; "
                    "coba lagi.\n",
                ),
                429,
            )
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
    # C-06: control payloads are tiny.  Reject a declared oversized body
    # before parsing, then lower the per-request cap so a chunked body cannot
    # bypass the check either.  The global MAX_CONTENT_LENGTH still applies to
    # every other endpoint.
    declared = request.content_length
    if declared is not None and declared > CONTROL_MAX_BODY:
        return jsonify({"ok": False, "error": "payload too large"}), 413
    request.max_content_length = CONTROL_MAX_BODY

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
    if len(name) > CONTROL_MAX_DEVICE:
        return jsonify({"ok": False, "error": "device too long"}), 400
    if len(action) > CONTROL_MAX_ACTION:
        return jsonify({"ok": False, "error": "action too long"}), 400

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
    try:
        with _diag_slot():
            output, status = _run_diagnostics(host, user=session.get("user"))
    except DiagnosticsBusy:
        return jsonify({"ok": False, "error": "busy"}), 429
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


def _terminate_process(proc: subprocess.Popen, pgid: int | None = None) -> None:
    """Kill the command's whole process group if it is still running.

    ``start_new_session=True`` makes the shell a session and group leader, so
    signalling the group also kills children that inherited the output pipe.
    ``pgid`` is captured right after ``Popen``: a background child can still be
    killed after the shell itself has been reaped, because the group id stays
    valid while the group has members.  If the group is already empty,
    ``killpg`` raises ``ProcessLookupError`` and the helper falls back to
    ``proc.kill()`` only while ``proc`` is alive (signalling a reaped PID could
    hit a recycled process).

    A child that detached into a new session (``setsid``) is outside this
    group; it can outlive the request, but the reader/pipe teardown still
    bounds the request, thread and FD.
    """
    if os.name == "posix" and pgid is not None:
        try:
            os.killpg(pgid, signal.SIGKILL)
            return
        except ProcessLookupError:
            pass
        except OSError:
            pass
    if proc.poll() is not None:
        return
    try:
        proc.kill()
    except OSError:
        pass


def _diag_limits() -> tuple[int, int]:
    """Return the configured (timeout seconds, output byte cap) for C-10.

    Invalid overrides fall back to the baseline values instead of turning a
    diagnostics request into a 500; the warning makes the misconfiguration
    visible in the service log.
    """
    config = current_app.config
    try:
        timeout_seconds = int(config.get("PING_TIMEOUT_SECONDS", 10))
    except (TypeError, ValueError, OverflowError):
        current_app.logger.warning(
            "invalid PING_TIMEOUT_SECONDS=%r; using 10",
            config.get("PING_TIMEOUT_SECONDS"),
        )
        timeout_seconds = 10
    try:
        output_limit = int(config.get("OUTPUT_LIMIT", 4096))
    except (TypeError, ValueError, OverflowError):
        current_app.logger.warning(
            "invalid OUTPUT_LIMIT=%r; using 4096",
            config.get("OUTPUT_LIMIT"),
        )
        output_limit = 4096
    return max(1, timeout_seconds), max(0, output_limit)


def _run_diagnostics(host: str, *, user: str | None = None) -> tuple[str, int]:
    """Run ping for the supplied host.

    V1: the host is interpolated into a shell command without validation or
    quoting. ``shell=True`` makes ``; id`` and friends execute arbitrary
    commands as the service user.

    Command output is read incrementally and capped at ``OUTPUT_LIMIT`` bytes
    (including the short timeout/truncation markers) so a noisy or malicious
    command cannot exhaust service memory.  The reader, the child process
    group and the pipe are always torn down before the response is built
    (C-10): a command that detaches itself with ``setsid`` can keep the write
    end of the pipe open, so the reader must be stoppable instead of only
    being joined.
    """
    timeout_seconds, output_limit = _diag_limits()
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
            **kwargs,
        )
    except OSError as exc:
        return f"[error] failed to start command: {exc}\n", 200

    # Capture the group id before the shell can be reaped.  A background child
    # stays in this group, so killpg() can still reach it afterwards.
    pgid = None
    if os.name == "posix":
        try:
            pgid = os.getpgid(proc.pid)
        except OSError:
            pgid = proc.pid

    state = {"chunks": [], "size": 0, "truncated": False}
    state_lock = threading.Lock()
    stop = threading.Event()

    def _reader() -> None:
        # Read raw bytes with a short poll() timeout so the thread can see
        # ``stop`` even while a detached child keeps the pipe open.  poll()
        # (unlike select()) also handles descriptors above FD_SETSIZE.  If
        # poll() is unavailable or fails, fall back to a non-blocking read so
        # the reader can still observe ``stop`` instead of blocking forever.
        fd = proc.stdout.fileno()
        poller = None
        nonblocking = False
        if os.name == "posix" and hasattr(select, "poll"):
            try:
                poller = select.poll()
                poller.register(fd, select.POLLIN)
            except (OSError, ValueError):
                poller = None
        if poller is None and os.name == "posix":
            try:
                os.set_blocking(fd, False)
                nonblocking = True
            except (OSError, ValueError):
                nonblocking = False
        while not stop.is_set():
            if poller is not None:
                try:
                    events = poller.poll(200)
                except (OSError, ValueError):
                    poller = None
                    try:
                        os.set_blocking(fd, False)
                        nonblocking = True
                    except (OSError, ValueError):
                        nonblocking = False
                    if not nonblocking:
                        break
                    continue
                if not events:
                    continue
            elif os.name == "posix" and not nonblocking:
                # No bounded wait is available; stop instead of blocking.
                break
            try:
                chunk = os.read(fd, 512)
            except BlockingIOError:
                time.sleep(0.05)
                continue
            except (OSError, ValueError):
                break
            if not chunk:
                break
            with state_lock:
                remaining = output_limit - state["size"]
                if remaining > 0:
                    state["chunks"].append(chunk[:remaining])
                    state["size"] += min(len(chunk), remaining)
                    cut = len(chunk) > remaining
                else:
                    # The cap was already reached; this chunk proves there is
                    # more output than the cap.
                    cut = True
                if cut:
                    state["truncated"] = True
            if cut:
                _terminate_process(proc, pgid)
                break

    reader = threading.Thread(target=_reader, daemon=True)
    try:
        reader.start()
    except RuntimeError:
        # Thread exhaustion must not leak the child or its pipe.
        _terminate_process(proc, pgid)
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        try:
            proc.stdout.close()
        except (OSError, ValueError):
            pass
        raise

    timed_out = False
    try:
        proc.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_process(proc, pgid)
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
    finally:
        # Let the reader drain a normal exit first; only force it when the
        # command is gone but something still holds the pipe open.
        reader.join(timeout=2)
        if reader.is_alive():
            stop.set()
            _terminate_process(proc, pgid)
            reader.join(timeout=2)
        if reader.is_alive():
            # Closing our read end makes poll()/read() fail in the reader;
            # the escaped child only sees EPIPE from here on.
            try:
                proc.stdout.close()
            except (OSError, ValueError):
                pass
            reader.join(timeout=1)
        try:
            proc.stdout.close()
        except (OSError, ValueError):
            pass

    with state_lock:
        chunks = list(state["chunks"])
        truncated = state["truncated"]
    output_bytes = b"".join(chunks)

    notes = []
    if timed_out:
        notes.append(f"\n[timeout] command exceeded {timeout_seconds} seconds\n")
    if truncated:
        notes.append(f"\n[output truncated at {output_limit} bytes]\n")
    note_text = "".join(notes)
    note_bytes = note_text.encode("utf-8")
    body_budget = max(0, output_limit - len(note_bytes))
    if len(output_bytes) > body_budget and not truncated:
        # Making room for the timeout marker cut the output; say so.
        notes.append(f"\n[output truncated at {output_limit} bytes]\n")
        note_text = "".join(notes)
        note_bytes = note_text.encode("utf-8")
        body_budget = max(0, output_limit - len(note_bytes))
    body = output_bytes[:body_budget].decode("utf-8", "replace")
    body_bytes = body.encode("utf-8")
    if len(body_bytes) > body_budget:
        # Replacement characters can expand; trim again so the cap holds.
        body = body_bytes[:body_budget].decode("utf-8", "ignore")
    result = body + note_text

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
