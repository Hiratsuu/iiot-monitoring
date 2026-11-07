"""Pytest suite for the IIoT Smart Controller baseline (P9 / C-03).

Run from ``team-app`` or ``build/iot-device``:

    python -m pytest -q

The suite is Linux-only: the diagnostics lifecycle tests use POSIX process
groups and ``/proc`` to prove a timed-out child is gone (C-10).  The app is
intentionally vulnerable (V1-V8); these tests lock in the accidental-bug fixes
from P1-P8 and prove the demo vulnerabilities are still present.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path
from urllib.parse import quote

import pytest

APP_ROOT = Path(__file__).resolve().parent.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

# config.py resolves its default DB_PATH at import time and tries to create
# /var/lib/iot-device.  Point it at a throwaway path first so importing the
# suite never touches the production data directory (even when run as root).
os.environ.setdefault("DB_PATH", str(APP_ROOT / "data" / "pytest-import.db"))

from iot_device import create_app  # noqa: E402
from iot_device import devices  # noqa: E402
from iot_device import routes as routes_module  # noqa: E402
from iot_device.db import (  # noqa: E402
    get_db,
    get_setting,
    prune_events,
    record_event,
)

DEFAULT_CONFIG = {
    "TESTING": True,
    "SECRET_KEY": "p9-test-secret",
    "EVENTS_MAX_ROWS": 10_000,
    "PING_TIMEOUT_SECONDS": 2,
    "OUTPUT_LIMIT": 4096,
    "DIAG_CONCURRENCY": 3,
    "MAX_CONTENT_LENGTH": 64 * 1024,
}

# The diagnostics lifecycle tests (C-10) rely on POSIX process groups and
# /proc to prove the child is gone; skip instead of silently passing elsewhere.
pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="suite uses /proc and POSIX process groups",
)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def _cancel_all_timers() -> None:
    with devices._timer_lock:
        names = list(devices._timers)
    for name in names:
        devices._cancel_buzzer_timer(name)


@pytest.fixture
def make_app(tmp_path):
    """Build an app with a throwaway DB; extra kwargs override the config."""
    apps = []

    def _make(**overrides):
        config = dict(DEFAULT_CONFIG)
        config.setdefault("DB_PATH", str(tmp_path / f"iot-{len(apps)}.db"))
        config.update(overrides)
        application = create_app(config)
        apps.append(application)
        return application

    yield _make
    _cancel_all_timers()


@pytest.fixture
def app(make_app):
    return make_app()


@pytest.fixture
def client(app):
    return app.test_client()


def login(client, username="admin", password="admin", next_url=None):
    url = "/login"
    if next_url is not None:
        # Keep the percent-encoded vector intact so Werkzeug decodes it once,
        # exactly like a real request.
        url += "?next=" + quote(next_url, safe="/%")
    return client.post(url, data={"username": username, "password": password})


@pytest.fixture
def admin(client):
    response = login(client)
    assert response.status_code == 302, response.data
    return client


@pytest.fixture
def operator(client):
    response = login(client, "operator", "operator123")
    assert response.status_code == 302, response.data
    return client


def wait_for(predicate, timeout=3.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def device_state(app, name):
    with app.app_context():
        device = devices.get_device(name)
        return device["state"] if device else None


def pids_with_cmdline(marker: bytes) -> list[int]:
    """Return PIDs whose /proc cmdline contains ``marker`` (Linux only).

    /proc/<pid>/cmdline separates argv with NUL bytes, so normalise them to
    spaces before matching a human-readable marker like ``b"sleep 30.25"``.
    """
    pids = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().replace(b"\x00", b" ")
        except OSError:
            continue
        if marker in cmdline:
            pids.append(int(entry.name))
    return pids


# ---------------------------------------------------------------------------
# Public health and authentication
# ---------------------------------------------------------------------------


def test_health_is_public_json(client, app):
    response = client.get("/health")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "ok"
    assert payload["device"] == app.config["DEVICE_NAME"]
    assert payload["firmware"] == app.config["FIRMWARE"]
    assert isinstance(payload["uptime_s"], int)


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/api/status"),
        ("get", "/api/events"),
        ("get", "/api/about"),
        ("get", "/api/diagnostics"),
        ("post", "/api/control"),
    ],
)
def test_api_without_session_is_401(client, method, path):
    response = getattr(client, method)(path)
    assert response.status_code == 401
    payload = response.get_json()
    assert payload["ok"] is False
    assert payload["error"] == "authentication required"


@pytest.mark.parametrize("path", ["/", "/events", "/about", "/diagnostics"])
def test_pages_without_session_redirect_to_login(client, path):
    response = client.get(path)
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_login_rejects_bad_password(client):
    response = login(client, password="wrong")
    assert response.status_code == 401
    assert "Username atau password salah" in response.get_data(as_text=True)
    assert client.get("/api/status").status_code == 401


def test_login_default_admin_credentials_v2(client):
    # V2 stays intentionally weak: admin/admin must keep working.
    response = login(client)
    assert response.status_code == 302
    assert response.headers["Location"] == "/"


def test_login_default_operator_credentials_v2(app):
    client = app.test_client()
    response = login(client, "operator", "operator123")
    assert response.status_code == 302
    assert response.headers["Location"] == "/"


def test_session_cookie_is_weak_v4(client):
    # V4 stays intentionally weak: no HttpOnly / Secure / SameSite flags.
    response = login(client)
    cookies = response.headers.getlist("Set-Cookie")
    assert cookies
    cookie = cookies[0]
    assert cookie.startswith("session=")
    assert "HttpOnly" not in cookie
    assert "Secure" not in cookie
    assert "SameSite" not in cookie


def test_logout_clears_session(client):
    login(client)
    response = client.get("/logout")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]
    assert client.get("/api/status").status_code == 401


def test_no_rate_limit_or_lockout_v3(app):
    # V3 stays intentionally present: 25 rapid failures do not lock the user.
    client = app.test_client()
    for _ in range(25):
        assert login(client, password="wrong").status_code == 401
    assert login(client).status_code == 302


# ---------------------------------------------------------------------------
# Authorization: admin-only audit trail (C-12)
# ---------------------------------------------------------------------------


def test_admin_can_read_audit_pages(admin):
    assert admin.get("/events").status_code == 200
    assert admin.get("/about").status_code == 200
    events = admin.get("/api/events")
    assert events.status_code == 200
    assert isinstance(events.get_json(), list)
    about = admin.get("/api/about")
    assert about.status_code == 200
    assert isinstance(about.get_json(), dict)


@pytest.mark.parametrize(
    "path,is_api",
    [
        ("/events", False),
        ("/about", False),
        ("/api/events", True),
        ("/api/about", True),
    ],
)
def test_operator_forbidden_from_admin_pages(operator, path, is_api):
    response = operator.get(path)
    assert response.status_code == 403
    if is_api:
        assert response.get_json()["error"] == "forbidden"


def test_operator_dashboard_hides_audit_trail(app):
    with app.app_context():
        record_event(
            "control",
            username="admin",
            device="led-1",
            detail="SECRET-AUDIT-ROW",
        )

    operator_client = app.test_client()
    assert login(operator_client, "operator", "operator123").status_code == 302
    response = operator_client.get("/")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "10 Event Terakhir" not in body
    assert "Lihat semua" not in body
    assert "SECRET-AUDIT-ROW" not in body

    admin_client = app.test_client()
    assert login(admin_client).status_code == 302
    admin_body = admin_client.get("/").get_data(as_text=True)
    assert "10 Event Terakhir" in admin_body
    assert "SECRET-AUDIT-ROW" in admin_body


def test_operator_dashboard_does_not_query_events(app, monkeypatch):
    # The template gate alone would hide the rows; this asserts the route-level
    # gate (C-12) so an operator dashboard never runs a FROM events query.
    queries = []
    real_get_db = routes_module.get_db

    def spy_get_db():
        db = real_get_db()
        db.set_trace_callback(queries.append)
        return db

    monkeypatch.setattr(routes_module, "get_db", spy_get_db)
    operator_client = app.test_client()
    assert login(operator_client, "operator", "operator123").status_code == 302
    queries.clear()
    assert operator_client.get("/").status_code == 200
    assert not any("FROM events" in query for query in queries)


def test_operator_can_control_and_run_diagnostics(operator):
    response = operator.post("/api/control", json={"device": "led-1", "action": "on"})
    assert response.status_code == 200
    assert response.get_json()["device"]["state"] == "on"
    diag = operator.get(
        "/api/diagnostics", query_string={"host": "127.0.0.1;echo OPERATOR"}
    )
    assert diag.status_code == 200
    assert "OPERATOR" in diag.get_data(as_text=True)


def test_control_without_csrf_token_v5(admin):
    # V5 stays intentionally present: no CSRF token is required.
    response = admin.post("/api/control", json={"device": "led-1", "action": "on"})
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Device control
# ---------------------------------------------------------------------------


def test_status_lists_actuators_and_sensor(admin):
    response = admin.get("/api/status")
    assert response.status_code == 200
    payload = response.get_json()
    assert [device["name"] for device in payload["devices"]] == [
        "led-1",
        "relay-1",
        "buzzer-1",
    ]
    assert isinstance(payload["sensor"]["temp_c"], float)
    assert isinstance(payload["uptime_s"], int)


def test_led_on_set_and_off(admin):
    on = admin.post("/api/control", json={"device": "led-1", "action": "on"})
    assert on.status_code == 200
    assert on.get_json()["device"]["state"] == "on"
    assert on.get_json()["device"]["value"] == 100

    dim = admin.post(
        "/api/control", json={"device": "led-1", "action": "set", "value": 55}
    )
    assert dim.status_code == 200
    assert dim.get_json()["device"]["value"] == 55
    assert dim.get_json()["device"]["state"] == "on"

    off = admin.post(
        "/api/control", json={"device": "led-1", "action": "set", "value": 0}
    )
    assert off.status_code == 200
    assert off.get_json()["device"]["state"] == "off"
    assert off.get_json()["device"]["value"] == 0


def test_relay_on(admin):
    response = admin.post("/api/control", json={"device": "relay-1", "action": "on"})
    assert response.status_code == 200
    assert response.get_json()["device"]["state"] == "on"


def test_buzzer_on_auto_off(app, admin, monkeypatch):
    # Shorten the 5 s window so the test stays fast; the timer code path is
    # the real one.
    monkeypatch.setattr(devices, "BUZZER_AUTO_OFF_SECONDS", 0.3)
    response = admin.post("/api/control", json={"device": "buzzer-1", "action": "on"})
    assert response.status_code == 200
    assert response.get_json()["device"]["state"] == "on"

    assert wait_for(lambda: device_state(app, "buzzer-1") == "off", timeout=3.0)
    with app.app_context():
        row = get_db().execute(
            "SELECT detail FROM events"
            " WHERE device = 'buzzer-1' AND detail = 'auto-off'"
        ).fetchone()
        assert row is not None


def test_unknown_device_and_action(admin):
    unknown = admin.post("/api/control", json={"device": "nope", "action": "on"})
    assert unknown.status_code == 404
    bad_action = admin.post(
        "/api/control", json={"device": "led-1", "action": "explode"}
    )
    assert bad_action.status_code == 400
    sensor = admin.post("/api/control", json={"device": "sensor-suhu", "action": "on"})
    assert sensor.status_code == 400


# ---------------------------------------------------------------------------
# Input validation and DoS caps (C-06)
# ---------------------------------------------------------------------------


def test_control_rejects_json_list(admin):
    response = admin.post("/api/control", json=[1, 2, 3])
    assert response.status_code == 400
    assert response.get_json()["error"] == "invalid payload"


def test_control_requires_device(admin):
    response = admin.post("/api/control", json={"action": "on"})
    assert response.status_code == 400
    assert response.get_json()["error"] == "device required"


def test_control_rejects_boolean_value(admin):
    response = admin.post(
        "/api/control",
        json={"device": "led-1", "action": "set", "value": True},
    )
    assert response.status_code == 400
    assert response.get_json()["error"] == "value must be numeric"


def test_api_wrong_method_returns_json_405(admin):
    response = admin.get("/api/control")
    assert response.status_code == 405
    assert response.get_json()["error"] == "method not allowed"


def test_control_rejects_oversized_payload(admin):
    payload = json.dumps({"device": "led-1", "action": "on", "pad": "x" * 2000})
    response = admin.post(
        "/api/control", data=payload, content_type="application/json"
    )
    assert response.status_code == 413
    assert response.get_json()["error"] == "payload too large"


def test_oversized_login_body_is_413(client):
    response = client.post(
        "/login",
        data=b"x" * (70 * 1024),
        content_type="application/x-www-form-urlencoded",
    )
    assert response.status_code == 413
    assert client.get("/health").status_code == 200


def test_oversized_body_rejected_before_view_when_logged_in(admin):
    # /login short-circuits with a redirect for an existing session and never
    # reads the body, so only the before_request cap can reject it (C-06).
    response = admin.post(
        "/login",
        data=b"x" * (70 * 1024),
        content_type="application/x-www-form-urlencoded",
    )
    assert response.status_code == 413


# ---------------------------------------------------------------------------
# Login redirect safety (C-13)
# ---------------------------------------------------------------------------


def test_safe_next_allows_local_path(client):
    response = login(client, next_url="/diagnostics")
    assert response.status_code == 302
    assert response.headers["Location"] == "/diagnostics"


@pytest.mark.parametrize(
    "target", ["//evil.example", "https://evil.example", "/\\evil.example"]
)
def test_safe_next_rejects_hostile_targets(client, target):
    response = login(client, next_url=target)
    assert response.status_code == 302
    assert response.headers["Location"] == "/"


@pytest.mark.parametrize(
    "target",
    [
        "/%09///evil.example",
        "/%09////evil.example",
        "/%0a///evil.example",
        "/%0d///evil.example",
        # Two slashes were already local pre-fix (Location /evil.example);
        # post-fix the control character is rejected, so this is a strict
        # non-regression case, not proof of the bypass.
        "/%09//evil.example",
    ],
)
def test_safe_next_rejects_control_char_bypass(client, target):
    response = login(client, next_url=target)
    assert response.status_code == 302
    assert response.headers["Location"] == "/"


# ---------------------------------------------------------------------------
# Diagnostics lifecycle and V1 (C-10)
# ---------------------------------------------------------------------------


def test_diagnostics_is_text_plain(admin):
    response = admin.get("/api/diagnostics", query_string={"host": "127.0.0.1"})
    assert response.status_code == 200
    assert response.content_type.startswith("text/plain")


def test_diagnostics_command_injection_v1(admin):
    # V1 stays intentionally exploitable: the host is interpolated into a
    # shell command.  `;echo` is deterministic even when ping is unavailable.
    response = admin.get(
        "/api/diagnostics", query_string={"host": "127.0.0.1;echo MARKER"}
    )
    assert response.status_code == 200
    assert "MARKER" in response.get_data(as_text=True)


def test_diagnostics_output_is_capped(make_app):
    app = make_app(OUTPUT_LIMIT=256)
    client = app.test_client()
    assert login(client).status_code == 302
    response = client.get(
        "/api/diagnostics", query_string={"host": "127.0.0.1;yes X"}
    )
    assert response.status_code == 200
    assert len(response.data) <= 256
    assert b"[output truncated" in response.data


def test_diagnostics_timeout_is_bounded(make_app):
    app = make_app(PING_TIMEOUT_SECONDS=1)
    client = app.test_client()
    assert login(client).status_code == 302
    started = time.monotonic()
    response = client.get(
        "/api/diagnostics", query_string={"host": "127.0.0.1;sleep 30"}
    )
    elapsed = time.monotonic() - started
    assert response.status_code == 200
    assert elapsed < 8
    assert "[timeout]" in response.get_data(as_text=True)


def test_diagnostics_timeout_kills_child_process(make_app):
    # A no-op _terminate_process would still return a 200 with [timeout] but
    # leave the child behind; assert the process group is really gone (C-10).
    app = make_app(PING_TIMEOUT_SECONDS=1)
    client = app.test_client()
    assert login(client).status_code == 302
    marker = b"sleep 30.25"
    assert not pids_with_cmdline(marker)
    try:
        response = client.get(
            "/api/diagnostics", query_string={"host": ";sleep 30.25"}
        )
        assert response.status_code == 200
        assert "[timeout]" in response.get_data(as_text=True)
        assert wait_for(lambda: not pids_with_cmdline(marker), timeout=3.0)
    finally:
        for pid in pids_with_cmdline(marker):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Page rendering and V8
# ---------------------------------------------------------------------------


def test_pages_render(admin):
    for path in ("/", "/diagnostics", "/events", "/about"):
        response = admin.get(path)
        assert response.status_code == 200, path
        assert response.content_type.startswith("text/html")
    assert "led-1" in admin.get("/").get_data(as_text=True)


def test_about_discloses_runtime_v8(admin):
    # V8 stays intentionally present: runtime/host fingerprinting.
    body = admin.get("/about").get_data(as_text=True)
    assert "Hostname" in body
    assert "Python" in body
    assert "Flask" in body


# ---------------------------------------------------------------------------
# Seeding and startup reconciliation (C-02, C-17)
# ---------------------------------------------------------------------------


def test_seed_populates_baseline(app):
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"] == 2
        assert (
            get_db().execute("SELECT COUNT(*) AS c FROM devices").fetchone()["c"] == 4
        )
        # firmware, device_name, boot_ts, seeded
        assert (
            get_db().execute("SELECT COUNT(*) AS c FROM settings").fetchone()["c"] == 4
        )
        assert get_setting("seeded") == "1"


def test_database_uses_wal(app):
    # P3: concurrent readers/writers rely on WAL + busy_timeout.
    with app.app_context():
        mode = get_db().execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"


def test_seed_is_one_shot(make_app, tmp_path):
    db_path = str(tmp_path / "seed-once.db")
    first = make_app(DB_PATH=db_path)
    with first.app_context():
        get_db().execute(
            "UPDATE users SET password = 'mitigated' WHERE username = 'admin'"
        )
        get_db().commit()

    second = make_app(DB_PATH=db_path)
    with second.app_context():
        row = get_db().execute(
            "SELECT password FROM users WHERE username = 'admin'"
        ).fetchone()
        assert row["password"] == "mitigated"
        assert get_setting("seeded") == "1"


def test_seed_marker_short_circuits_second_run(app):
    # The marker guard is the mechanism: a second seed() must report "nothing
    # written", not merely rely on INSERT OR IGNORE to keep existing rows.
    from iot_device.seed_data import seed

    with app.app_context():
        assert seed(app) is False


def test_seed_force_keeps_existing_rows(app):
    # --force re-runs the inserts; INSERT OR IGNORE must keep runtime changes.
    from iot_device.seed_data import seed

    with app.app_context():
        get_db().execute(
            "UPDATE users SET password = 'mitigated' WHERE username = 'admin'"
        )
        get_db().commit()
        assert seed(app, force=True) is True
        row = get_db().execute(
            "SELECT password FROM users WHERE username = 'admin'"
        ).fetchone()
        assert row["password"] == "mitigated"


def test_seed_twice_keeps_counts(app):
    # Repeated seeding must not duplicate users/devices/settings (report §2.3).
    from iot_device.seed_data import seed

    def counts():
        with app.app_context():
            return (
                get_db().execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"],
                get_db().execute("SELECT COUNT(*) AS c FROM devices").fetchone()["c"],
                get_db().execute("SELECT COUNT(*) AS c FROM settings").fetchone()["c"],
            )

    before = counts()
    with app.app_context():
        assert seed(app) is False
    assert counts() == before
    assert before == (2, 4, 4)


def test_buzzer_startup_reconcile(make_app, tmp_path):
    db_path = str(tmp_path / "reconcile.db")
    first = make_app(DB_PATH=db_path)
    with first.app_context():
        get_db().execute("UPDATE devices SET state = 'on' WHERE name = 'buzzer-1'")
        get_db().commit()

    second = make_app(DB_PATH=db_path)
    with second.app_context():
        row = get_db().execute(
            "SELECT state FROM devices WHERE name = 'buzzer-1'"
        ).fetchone()
        assert row["state"] == "off"
        event = get_db().execute(
            "SELECT detail FROM events"
            " WHERE device = 'buzzer-1' AND detail = 'startup-reset'"
        ).fetchone()
        assert event is not None


# ---------------------------------------------------------------------------
# Audit trail (scenario 2)
# ---------------------------------------------------------------------------


def test_login_failure_is_audited(app, client):
    login(client, password="wrong")
    with app.app_context():
        row = get_db().execute(
            "SELECT username, action, source_ip FROM events"
            " WHERE action = 'login_fail' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row["username"] == "admin"
        assert row["source_ip"] == "127.0.0.1"


def test_control_is_audited(app, admin):
    admin.post("/api/control", json={"device": "led-1", "action": "on"})
    with app.app_context():
        row = get_db().execute(
            "SELECT username, device, detail FROM events"
            " WHERE action = 'control' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row["username"] == "admin"
        assert row["device"] == "led-1"
        assert row["detail"] == "on"


def test_diagnostics_is_audited(app, admin):
    admin.get("/api/diagnostics", query_string={"host": "127.0.0.1"})
    with app.app_context():
        row = get_db().execute(
            "SELECT detail FROM events"
            " WHERE action = 'diagnostics' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert "ping -c 2" in row["detail"]


# ---------------------------------------------------------------------------
# Buzzer race (C-09) and event retention (C-16)
# ---------------------------------------------------------------------------


def test_buzzer_stale_timer_cannot_override_new_on(app, admin):
    first = admin.post("/api/control", json={"device": "buzzer-1", "action": "on"})
    assert first.status_code == 200
    second = admin.post("/api/control", json={"device": "buzzer-1", "action": "on"})
    assert second.status_code == 200

    with app.app_context():
        rows = get_db().execute(
            "SELECT id FROM events"
            " WHERE device = 'buzzer-1' AND action = 'control' ORDER BY id"
        ).fetchall()
        first_event, second_event = rows[0]["id"], rows[-1]["id"]

    # Drop the real timer and drive the stale-timer path directly, so the test
    # is deterministic instead of sleeping through the 5 s auto-off window.
    devices._cancel_buzzer_timer("buzzer-1")
    with devices._timer_lock:
        generation = devices._generations["buzzer-1"]

    with app.app_context():
        devices._buzzer_auto_off(app, "buzzer-1", generation, first_event)
    assert device_state(app, "buzzer-1") == "on"

    with app.app_context():
        devices._buzzer_auto_off(app, "buzzer-1", generation, second_event)
    assert device_state(app, "buzzer-1") == "off"


def test_buzzer_stale_generation_is_ignored(app, admin):
    assert admin.post(
        "/api/control", json={"device": "buzzer-1", "action": "on"}
    ).status_code == 200
    with app.app_context():
        event_id = get_db().execute(
            "SELECT id FROM events"
            " WHERE device = 'buzzer-1' AND action = 'control'"
            " ORDER BY id DESC LIMIT 1"
        ).fetchone()["id"]

    devices._cancel_buzzer_timer("buzzer-1")
    with devices._timer_lock:
        generation = devices._generations["buzzer-1"]

    # A timer from an older generation must return before touching the DB.
    with app.app_context():
        devices._buzzer_auto_off(app, "buzzer-1", generation - 1, event_id)
    assert device_state(app, "buzzer-1") == "on"


def test_buzzer_schedule_registers_token_and_cancels_old_timer(app, admin):
    assert admin.post(
        "/api/control", json={"device": "buzzer-1", "action": "on"}
    ).status_code == 200
    with devices._timer_lock:
        old_timer = devices._timers["buzzer-1"]

    assert admin.post(
        "/api/control", json={"device": "buzzer-1", "action": "on"}
    ).status_code == 200
    with devices._timer_lock:
        new_timer = devices._timers["buzzer-1"]
        token = devices._timer_tokens["buzzer-1"]

    assert old_timer is not new_timer
    assert old_timer.finished.is_set()
    with app.app_context():
        latest = get_db().execute(
            "SELECT id FROM events"
            " WHERE device = 'buzzer-1' AND action = 'control'"
            " ORDER BY id DESC LIMIT 1"
        ).fetchone()["id"]
    assert token == latest


def test_buzzer_schedule_registers_token_without_control_path(app):
    # _schedule_buzzer_off() is also a public-ish helper; it must register its
    # own token instead of relying on control_device()'s pre-commit guard.
    with app.app_context():
        event_id = record_event("control", device="buzzer-1", detail="on")
        devices._timer_tokens.pop("buzzer-1", None)
        devices._schedule_buzzer_off(app, "buzzer-1", event_id)
        assert devices._timer_tokens["buzzer-1"] == event_id
    devices._cancel_buzzer_timer("buzzer-1")


def test_buzzer_on_registers_token_before_commit(app, admin, monkeypatch):
    # If scheduling is a no-op, only the pre-commit registration can protect
    # the event row the auto-off UPDATE will later match (C-09/C-16).
    monkeypatch.setattr(devices, "_schedule_buzzer_off", lambda *args, **kwargs: None)
    assert admin.post(
        "/api/control", json={"device": "buzzer-1", "action": "on"}
    ).status_code == 200
    with app.app_context():
        latest = get_db().execute(
            "SELECT id FROM events"
            " WHERE device = 'buzzer-1' AND action = 'control'"
            " ORDER BY id DESC LIMIT 1"
        ).fetchone()["id"]
    try:
        assert devices.protected_event_ids() == {latest}
    finally:
        devices._timer_tokens.pop("buzzer-1", None)


def test_buzzer_off_cancels_timer(app, admin):
    assert admin.post(
        "/api/control", json={"device": "buzzer-1", "action": "on"}
    ).status_code == 200
    with devices._timer_lock:
        timer = devices._timers["buzzer-1"]

    assert admin.post(
        "/api/control", json={"device": "buzzer-1", "action": "off"}
    ).status_code == 200
    assert timer.finished.is_set()
    with devices._timer_lock:
        assert "buzzer-1" not in devices._timers
        assert "buzzer-1" not in devices._timer_tokens


def test_prune_events_keeps_newest(app):
    with app.app_context():
        for index in range(10):
            record_event("control", device="led-1", detail=f"e{index}")
        deleted = prune_events(3)
        assert deleted == 7
        rows = get_db().execute("SELECT detail FROM events ORDER BY id").fetchall()
        assert [row["detail"] for row in rows] == ["e7", "e8", "e9"]


def test_event_cap_is_enforced_by_sampling(make_app):
    app = make_app(EVENTS_MAX_ROWS=5)
    with app.app_context():
        for index in range(20):
            record_event("control", device="led-1", detail=f"e{index}")
        count = get_db().execute("SELECT COUNT(*) AS c FROM events").fetchone()["c"]
        assert count <= 5
        newest = get_db().execute(
            "SELECT detail FROM events ORDER BY id DESC LIMIT 1"
        ).fetchone()["detail"]
        assert newest == "e19"


def test_prune_protects_pending_buzzer_token(app):
    with app.app_context():
        ids = [
            record_event("control", device="led-1", detail=f"e{index}")
            for index in range(10)
        ]
        devices._timer_tokens["buzzer-1"] = ids[0]
        try:
            prune_events(3, protected_ids=devices.protected_event_ids())
            remaining = [
                row["id"]
                for row in get_db()
                .execute("SELECT id FROM events ORDER BY id")
                .fetchall()
            ]
            assert ids[0] in remaining
            # Three newest rows plus the protected token row.
            assert len(remaining) == 4
        finally:
            devices._timer_tokens.pop("buzzer-1", None)
