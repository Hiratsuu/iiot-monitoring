"""Application factory for the IIoT Smart Controller baseline.

WARNING: this application is intentionally vulnerable and must only run inside
an isolated lab network (192.168.100.90:8080).  See rencana-aplikasi-iot.md.
"""

import logging
from pathlib import Path

from flask import Flask, jsonify, request

from .config import Config
from .db import close_db, init_schema, get_db
from .routes import bp

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


def create_app(config_object=Config) -> Flask:
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

    app.register_blueprint(bp)
    app.teardown_appcontext(close_db)

    with app.app_context():
        init_schema()
        row = get_db().execute("SELECT COUNT(*) AS n FROM users").fetchone()
        if row["n"] == 0:
            from seed import seed

            seed(app)

    @app.errorhandler(404)
    def not_found(exc):
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "not found"}), 404
        return "Not Found", 404

    @app.errorhandler(500)
    def server_error(exc):  # pragma: no cover - defensive
        app.logger.exception("unhandled error: %s", exc)
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "internal server error"}), 500
        return "Internal Server Error", 500

    return app
