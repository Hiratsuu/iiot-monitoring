"""Authentication helpers.

The baseline compares passwords in plaintext and offers no throttling or
lockout (V2/V3).  Do not reuse this code outside an isolated lab.
"""

from functools import wraps

from flask import jsonify, redirect, request, session, url_for

from .db import get_db


def verify_login(username: str, password: str):
    """Return the user row on success, else None.

    V2: passwords are stored and compared as plaintext.
    """
    row = (
        get_db()
        .execute("SELECT * FROM users WHERE username = ?", (username,))
        .fetchone()
    )
    if row is not None and row["password"] == password:
        return row
    return None


def login_user(row) -> None:
    session.permanent = True
    session["user"] = row["username"]
    session["role"] = row["role"]


def logout_user() -> None:
    session.clear()


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user" not in session:
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "error": "authentication required"}), 401
            return redirect(url_for("routes.login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user" not in session:
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "error": "authentication required"}), 401
            return redirect(url_for("routes.login", next=request.path))
        if session.get("role") != "admin":
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "error": "forbidden"}), 403
            return "Forbidden", 403
        return view(*args, **kwargs)

    return wrapped
