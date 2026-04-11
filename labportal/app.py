#!/usr/bin/env python3
"""
Lab Portal — lightweight web app for managing OCP lab access requests.
"""
import hashlib
import os
import secrets
import sys
from datetime import datetime
from functools import wraps

from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, session, abort
)

import config
from db import get_db, init_db
from mail import send_admin_notification, send_user_approved, send_user_denied

app = Flask(__name__)
app.secret_key = config.SECRET_KEY


class PrefixMiddleware:
    """Make Flask aware it's served under /labs via reverse proxy."""
    def __init__(self, wsgi_app, prefix="/labs"):
        self.wsgi_app = wsgi_app
        self.prefix = prefix

    def __call__(self, environ, start_response):
        environ["SCRIPT_NAME"] = self.prefix
        path = environ.get("PATH_INFO", "")
        if path.startswith(self.prefix):
            environ["PATH_INFO"] = path[len(self.prefix):]
        return self.wsgi_app(environ, start_response)


app.wsgi_app = PrefixMiddleware(app.wsgi_app, prefix="/labs")


# --- Helpers ---

def hash_password(password, salt=None):
    if salt is None:
        salt = secrets.token_hex(16)
    hashed = hashlib.sha256(f"{salt}:{password}".encode()).hexdigest()
    return f"{salt}:{hashed}"


def verify_password(password, stored):
    salt, expected = stored.split(":", 1)
    return hash_password(password, salt) == stored


def get_admin_password_hash():
    conn = get_db()
    row = conn.execute(
        "SELECT value FROM admin_config WHERE key='admin_password'"
    ).fetchone()
    conn.close()
    return row["value"] if row else None


def set_admin_password(password):
    conn = get_db()
    hashed = hash_password(password)
    conn.execute(
        "INSERT OR REPLACE INTO admin_config (key, value) VALUES ('admin_password', ?)",
        (hashed,)
    )
    conn.commit()
    conn.close()


def validate_email(email):
    """Check that email is @redhat.com and not spoofable."""
    email = email.strip().lower()
    if "@" not in email:
        return False, "Invalid email address."
    local, domain = email.rsplit("@", 1)
    if not local:
        return False, "Invalid email address."
    if domain not in config.ALLOWED_DOMAINS:
        return False, f"Only @redhat.com email addresses are accepted."
    # Block subdomains like user@something.redhat.com
    if domain != "redhat.com":
        return False, "Only @redhat.com email addresses are accepted."
    return True, email


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("admin"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


# --- Routes ---

@app.route("/")
def index():
    conn = get_db()
    stats = {
        "total": conn.execute("SELECT COUNT(*) FROM access_requests").fetchone()[0],
        "pending": conn.execute(
            "SELECT COUNT(*) FROM access_requests WHERE status='pending'"
        ).fetchone()[0],
    }
    conn.close()
    return render_template("index.html", stats=stats, hostname=config.LAB_HOSTNAME)


@app.route("/request", methods=["GET", "POST"])
def request_access():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip()
        reason = request.form.get("reason", "").strip()

        errors = []
        if not name or len(name) < 2:
            errors.append("Name is required (at least 2 characters).")
        if not reason or len(reason) < 10:
            errors.append("Please provide a reason (at least 10 characters).")

        valid, result = validate_email(email)
        if not valid:
            errors.append(result)
        else:
            email = result

        if errors:
            return render_template("request_form.html", errors=errors,
                                   name=name, email=email, reason=reason)

        # Check for duplicate pending requests
        conn = get_db()
        existing = conn.execute(
            "SELECT id FROM access_requests WHERE email=? AND status='pending'",
            (email,)
        ).fetchone()
        if existing:
            conn.close()
            flash("You already have a pending request. Please wait for it to be reviewed.", "warning")
            return redirect(url_for("request_access"))

        conn.execute(
            "INSERT INTO access_requests (name, email, reason) VALUES (?, ?, ?)",
            (name, email, reason)
        )
        conn.commit()
        conn.close()

        # Notify admin
        send_admin_notification(name, email, reason)
        flash("Your request has been submitted. You'll receive an email once it's reviewed.", "success")
        return redirect(url_for("index"))

    return render_template("request_form.html", errors=[], name="", email="", reason="")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")

        stored_hash = get_admin_password_hash()
        if not stored_hash:
            flash("Admin password not set. Run: python3 app.py set-password", "danger")
            return render_template("login.html")

        if username == config.ADMIN_USER and verify_password(password, stored_hash):
            session["admin"] = True
            return redirect(url_for("admin_panel"))
        else:
            flash("Invalid credentials.", "danger")

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.pop("admin", None)
    return redirect(url_for("index"))


@app.route("/admin")
@login_required
def admin_panel():
    status_filter = request.args.get("status", "pending")
    conn = get_db()
    if status_filter == "all":
        rows = conn.execute(
            "SELECT * FROM access_requests ORDER BY created_at DESC"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM access_requests WHERE status=? ORDER BY created_at DESC",
            (status_filter,)
        ).fetchall()
    conn.close()
    return render_template("admin.html", requests=rows, status_filter=status_filter)


@app.route("/admin/action/<int:req_id>", methods=["POST"])
@login_required
def admin_action(req_id):
    action = request.form.get("action")
    note = request.form.get("note", "").strip()

    if action not in ("approve", "deny"):
        abort(400)

    conn = get_db()
    row = conn.execute("SELECT * FROM access_requests WHERE id=?", (req_id,)).fetchone()
    if not row:
        conn.close()
        abort(404)

    new_status = "approved" if action == "approve" else "denied"
    conn.execute(
        "UPDATE access_requests SET status=?, resolved_at=?, admin_note=? WHERE id=?",
        (new_status, datetime.utcnow().isoformat(), note, req_id)
    )
    conn.commit()
    conn.close()

    if action == "approve":
        send_user_approved(row["email"], row["name"], note)
        flash(f"Approved request from {row['name']}. Notification sent.", "success")
    else:
        send_user_denied(row["email"], row["name"], note)
        flash(f"Denied request from {row['name']}. Notification sent.", "info")

    return redirect(url_for("admin_panel"))


# --- CLI ---

def cli_set_password():
    import getpass
    init_db()
    pw = getpass.getpass("Set admin password: ")
    pw2 = getpass.getpass("Confirm: ")
    if pw != pw2:
        print("Passwords don't match.")
        sys.exit(1)
    set_admin_password(pw)
    print(f"Admin password set for user '{config.ADMIN_USER}'.")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "set-password":
        cli_set_password()
    else:
        init_db()
        if not get_admin_password_hash():
            print("WARNING: No admin password set. Run: python3 app.py set-password")
        app.run(host="127.0.0.1", port=5000, debug=False)
