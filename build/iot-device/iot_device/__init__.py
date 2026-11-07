"""Application factory for the IIoT Smart Controller baseline.

WARNING: this application is intentionally vulnerable and must only run inside
an isolated lab network (192.168.100.90:8080).  See rencana-aplikasi-iot.md.
"""

import logging
import time
from pathlib import Path

from flask import Flask, abort, jsonify, request

from .config import Config
from .db import (
    close_db,
    enforce_event_cap,
    get_db,
    init_schema,
    set_setting,
)
from .devices import protected_event_ids, reconcile_on_startup
from .routes import bp, init_diagnostics_limiter
from .seed_data import seed as seed_data

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


def create_app(config_object=Config, *, seed_on_start: bool = True) -> Flask:
    base = Path(__file__).resolve().parent.parent
    app = Flask(
        __name__,
        template_folder=str(base / "templates"),
        static_folder=str(base / "static"),
    )

    app.config.from_object(Config)
    if isinstance(config_object, dict):
        app.config.update(config_object)
    elif config_object is not Config:
        app.config.from_object(config_object)

    # A bad create_app() override must not turn every POST into a 500; the
    # env path still fails fast at import like PORT.
    raw_limit = app.config.get("MAX_CONTENT_LENGTH")
    if raw_limit is not None:
        try:
            app.config["MAX_CONTENT_LENGTH"] = int(raw_limit)
        except (TypeError, ValueError, OverflowError):
            app.logger.warning(
                "invalid MAX_CONTENT_LENGTH=%r; using 65536", raw_limit
            )
            app.config["MAX_CONTENT_LENGTH"] = 64 * 1024

    app.register_blueprint(bp)
    app.teardown_appcontext(close_db)
    # Per-app diagnostics concurrency cap (C-06); the limiter lives in
    # app.extensions so tests can build several apps in one process.
    init_diagnostics_limiter(app)
    # Retention pruning must not delete the control event a pending buzzer
    # timer still uses as its conditional-write token (C-16/C-09).
    app.extensions.setdefault("iot_device", {})["protected_event_ids"] = (
        protected_event_ids
    )

    with app.app_context():
        init_schema()
        if seed_on_start:
            # Seeding is one-shot: the marker in settings keeps runtime
            # changes (e.g. a mitigated admin password) intact across
            # restarts (C-02).  It only fills rows that are missing.
            seed_data(app)
            # Buzzer state is timer-driven and cannot survive a restart
            # (C-17).  The CLI seeder skips this: no service start happened.
            reconcile_on_startup()
        # uptime_s reflects service start, not the original install date.
        set_setting("boot_ts", str(int(time.time())))
        # The sampling counter is in memory; trim an oversized table now so
        # a restart cannot reset the counter and let it grow unbounded.
        enforce_event_cap()

    @app.before_request
    def reject_oversized_body():
        # A chunked body has no Content-Length to check preemptively, and
        # Werkzeug's limited stream truncates it at the cap instead of
        # raising 413.  The baseline does not need streaming uploads, so
        # require a Content-Length and reject chunked bodies outright.
        if "chunked" in request.headers.get("Transfer-Encoding", "").lower():
            abort(411)
        # Enforce the global body cap even for views that never read the
        # body (e.g. /login short-circuits when a session already exists).
        # Flask/Werkzeug only raise 413 when the stream is read.
        limit = app.config.get("MAX_CONTENT_LENGTH")
        if limit is not None:
            try:
                limit = int(limit)
            except (TypeError, ValueError, OverflowError):
                limit = None
        if (
            limit is not None
            and request.content_length is not None
            and request.content_length > limit
        ):
            abort(413)

    @app.errorhandler(404)
    def not_found(exc):
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "not found"}), 404
        return "Not Found", 404

    @app.errorhandler(405)
    def method_not_allowed(exc):
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "method not allowed"}), 405
        return "Method Not Allowed", 405

    @app.errorhandler(411)
    def length_required(exc):
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "length required"}), 411
        return "Length Required", 411

    @app.errorhandler(413)
    def request_too_large(exc):
        # MAX_CONTENT_LENGTH (C-06) raises this before a view can read the
        # body.  Keep the same JSON/HTML split as 404/405.
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "payload too large"}), 413
        return "Payload Too Large", 413

    @app.errorhandler(500)
    def server_error(exc):  # pragma: no cover - defensive
        app.logger.exception("unhandled error: %s", exc)
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "internal server error"}), 500
        return "Internal Server Error", 500

    return app
