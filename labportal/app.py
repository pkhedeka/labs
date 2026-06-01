#!/usr/bin/env python3
"""
Lab Portal — lightweight web app for managing OCP lab access requests.
"""
import fcntl
import glob
import hashlib
import json
import os
import pty
import re
import secrets
import shutil
import urllib.request
import urllib.error
import select
import signal
import struct
import subprocess
import sys
import termios
import threading
from datetime import datetime
from functools import wraps

from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, session, abort, jsonify, send_file
)
from flask_socketio import SocketIO, emit, disconnect

import config
from db import get_db, init_db
from mail import (send_password_reset_notification, send_reset_token_email)

app = Flask(__name__)
app.secret_key = config.SECRET_KEY


@app.context_processor
def inject_globals():
    return {
        "hostname": config.lab_hostname(),
        "maintenance_message": config.get_site("maintenance_message") or "",
    }


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

socketio = SocketIO(app, path="socket.io", cors_allowed_origins="*",
                    async_mode="threading")

# Auto-reap child processes (prevents zombie terminals)
signal.signal(signal.SIGCHLD, signal.SIG_IGN)

# Active terminal sessions: sid -> {fd, pid, last_activity}
terminal_sessions = {}
TERMINAL_TIMEOUT = 3600       # 1 hour inactivity timeout (seconds)
TERMINAL_WARN_BEFORE = 300    # warn 5 minutes before timeout


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
    return config.get_site("admin_password")


def set_admin_password(password):
    config.set_site("admin_password", hash_password(password))


def validate_email(email):
    """Validate email against the configured allowed domains."""
    email = email.strip().lower()
    if "@" not in email:
        return False, "Invalid email address."
    local, domain = email.rsplit("@", 1)
    if not local:
        return False, "Invalid email address."
    allowed = config.allowed_email_domains()
    if not allowed:
        return True, email  # no restriction configured
    if domain not in allowed:
        domains_str = ", ".join(f"@{d}" for d in sorted(allowed))
        return False, f"Only {domains_str} email addresses are accepted."
    return True, email


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("admin"):
            return redirect(url_for("user_login"))
        return f(*args, **kwargs)
    return decorated


def user_login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_email"):
            return redirect(url_for("user_login"))
        return f(*args, **kwargs)
    return decorated


def log_activity(event, details=None):
    """Record an event in the activity_log table."""
    client_ip = request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or request.remote_addr
    conn = get_db()
    conn.execute(
        "INSERT INTO activity_log (event, user_email, ip_address, details) VALUES (?, ?, ?, ?)",
        (event,
         session.get("user_email") or session.get("admin_user", ""),
         client_ip,
         details)
    )
    conn.commit()
    conn.close()


def setup_required(f):
    """Redirect to setup wizard if first-run hasn't been completed."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not config.is_setup_complete():
            return redirect(url_for("setup"))
        return f(*args, **kwargs)
    return decorated


def generate_password(length=12):
    alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def get_cluster_info(clusters):
    """Get deployment metadata (creator, description, install_type) per cluster from DB."""
    conn = get_db()
    rows = conn.execute(
        "SELECT cluster_name, started_by, description, install_type FROM deployments WHERE status IN ('deploying','completed')"
    ).fetchall()
    conn.close()
    info = {}
    for row in rows:
        info[row["cluster_name"]] = {
            "started_by": row["started_by"] or "",
            "description": row["description"] or "",
            "install_type": row["install_type"] or "upi",
        }
    return info


def get_cluster_reservations():
    """Get active (non-expired) cluster reservations."""
    conn = get_db()
    conn.execute("DELETE FROM cluster_reservations WHERE reserved_until < datetime('now')")
    conn.commit()
    rows = conn.execute(
        "SELECT cluster_name, reserved_by, purpose, reserved_until FROM cluster_reservations"
    ).fetchall()
    conn.close()
    return {
        row["cluster_name"]: {
            "reserved_by": row["reserved_by"],
            "purpose": row["purpose"],
            "reserved_until": row["reserved_until"],
        }
        for row in rows
    }


def _write_reservation_file():
    """Write active reservations to JSON for MOTD script consumption."""
    reservations = get_cluster_reservations()
    try:
        with open("/var/run/cluster-reservations.json", "w") as f:
            json.dump(reservations, f)
    except PermissionError:
        pass


def get_cluster_versions(clusters):
    """Get OCP version per cluster — DB first, then fall back to /kvm/clusters/ dirs."""
    import glob
    conn = get_db()
    rows = conn.execute(
        "SELECT cluster_name, ocp_version FROM deployments WHERE status IN ('deploying','completed')"
    ).fetchall()
    conn.close()
    versions = {row["cluster_name"]: row["ocp_version"] for row in rows}
    # Fill in missing versions by scanning disk (most recently modified first)
    for name in clusters:
        if name not in versions:
            matches = glob.glob(f"/kvm/clusters/{name}-*/auth/kubeconfig")
            if matches:
                # Pick the most recently modified directory
                latest = max(matches, key=os.path.getmtime)
                dir_name = latest.split("/")[3]       # e.g. "upi1-4.19.22"
                version = dir_name[len(name) + 1:]   # strip "<name>-"
                if version:
                    versions[name] = version
    return versions


def get_lab_status():
    """Query libvirt for VM list and system resources."""
    vms = []
    try:
        result = subprocess.run(
            ["virsh", "list", "--all"],
            capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.strip().split("\n")[2:]:
            parts = line.split()
            if len(parts) >= 3:
                vm_id = parts[0] if parts[0] != "-" else "-"
                name = parts[1]
                state = " ".join(parts[2:])
                vms.append({"id": vm_id, "name": name, "state": state})
            elif len(parts) == 2:
                vms.append({"id": "-", "name": parts[0], "state": parts[1]})
    except Exception:
        pass

    # Group VMs into clusters by name convention: vm-<cluster>-<role>
    clusters = {}
    for vm in vms:
        name = vm["name"]
        cluster = name  # fallback
        if name.startswith("vm-"):
            stripped = name[3:]
            for suffix in ("-bootstrap", "-master-0", "-master-1", "-master-2", "-worker-0", "-worker-1"):
                if stripped.endswith(suffix):
                    cluster = stripped[: -len(suffix)]
                    break
            else:
                cluster = stripped
        else:
            for suffix in ("-boot", "-m0", "-m1", "-m2", "-w0", "-w1"):
                if name.endswith(suffix):
                    cluster = name[: -len(suffix)]
                    break
        if cluster not in clusters:
            clusters[cluster] = []
        clusters[cluster].append(vm)

    # System resources
    resources = {}
    try:
        result = subprocess.run(
            ["free", "-g"], capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.split("\n"):
            if line.startswith("Mem:"):
                parts = line.split()
                resources["ram_total"] = parts[1]
                resources["ram_used"] = parts[2]
                resources["ram_free"] = parts[3]
    except Exception:
        pass

    try:
        result = subprocess.run(
            ["nproc"], capture_output=True, text=True, timeout=5
        )
        resources["cpus"] = result.stdout.strip()
    except Exception:
        pass

    # Count vCPUs allocated to running VMs
    cpus_used = 0
    for vm in vms:
        if vm["state"] != "running":
            continue
        try:
            result = subprocess.run(
                ["virsh", "vcpucount", vm["name"], "--current"],
                capture_output=True, text=True, timeout=5
            )
            cpus_used += int(result.stdout.strip())
        except Exception:
            pass
    resources["cpus_used"] = str(cpus_used)

    try:
        disk_path = "/kvm" if os.path.ismount("/kvm") else "/"
        result = subprocess.run(
            ["df", "-h", disk_path],
            capture_output=True, text=True, timeout=5
        )
        lines = result.stdout.strip().split("\n")
        if len(lines) > 1:
            parts = lines[1].split()
            resources["disk_total"] = parts[1]
            resources["disk_used"] = parts[2]
            resources["disk_avail"] = parts[3]
            resources["disk_pct"] = parts[4]
    except Exception:
        pass

    return vms, clusters, resources


def generate_infra_config():
    """Write /etc/ocp-lab.conf for shell scripts to source."""
    slots = config.cluster_slots()
    domain = config.base_domain()
    # Format: name:offset pairs space-separated
    slots_str = " ".join(f"{k}:{v}" for k, v in sorted(slots.items(), key=lambda x: x[1]))
    conf = f"""# Generated by OCP Lab Portal — do not edit manually
BASE_DOMAIN="{domain}"
CLUSTER_SLOTS="{slots_str}"
"""
    try:
        with open("/etc/ocp-lab.conf", "w") as f:
            f.write(conf)
    except PermissionError:
        print("[config] Warning: Could not write /etc/ocp-lab.conf (permission denied)")


# --- First-run Setup Wizard ---

@app.route("/setup", methods=["GET", "POST"])
def setup():
    if config.is_setup_complete():
        return redirect(url_for("index"))

    if request.method == "POST":
        # Collect form data
        admin_username = request.form.get("admin_user", "admin").strip()
        admin_password = request.form.get("admin_password", "").strip()
        admin_password2 = request.form.get("admin_password2", "").strip()
        admin_email_addr = request.form.get("admin_email", "").strip()

        email_domains = request.form.get("email_domains", "").strip()
        smtp = request.form.get("smtp_host", "localhost").strip()
        smtp_p = request.form.get("smtp_port", "25").strip()
        from_addr = request.form.get("from_email", "").strip()

        domain = request.form.get("base_domain", "example.com").strip()
        hostname = request.form.get("lab_hostname", "").strip()

        # Cluster slots — dynamic rows: slot_name_1, slot_offset_1, ...
        slots = {}
        i = 1
        while True:
            sname = request.form.get(f"slot_name_{i}", "").strip().lower()
            soffset = request.form.get(f"slot_offset_{i}", "").strip()
            if not sname and not soffset:
                break
            if sname and soffset:
                try:
                    slots[sname] = int(soffset)
                except ValueError:
                    pass
            i += 1

        # Validation
        errors = []
        if not admin_password or len(admin_password) < 8:
            errors.append("Admin password must be at least 8 characters.")
        if admin_password != admin_password2:
            errors.append("Passwords do not match.")
        if not domain:
            errors.append("Base domain is required.")
        if not slots:
            errors.append("At least one cluster slot is required.")

        # Check for duplicate offsets
        offsets = list(slots.values())
        if len(offsets) != len(set(offsets)):
            errors.append("Cluster slot IP offsets must be unique.")

        if errors:
            return render_template("setup.html", errors=errors,
                                   admin_user=admin_username,
                                   admin_email=admin_email_addr,
                                   email_domains=email_domains,
                                   smtp_host=smtp, smtp_port=smtp_p,
                                   from_email=from_addr,
                                   base_domain=domain,
                                   lab_hostname=hostname,
                                   slots=slots)

        # Save everything to DB
        config.set_site_bulk({
            "admin_user": admin_username,
            "admin_password": hash_password(admin_password),
            "admin_email": admin_email_addr,
            "allowed_email_domains": email_domains,
            "smtp_host": smtp,
            "smtp_port": smtp_p,
            "from_email": from_addr,
            "base_domain": domain,
            "lab_hostname": hostname or domain,
            "cluster_slots": json.dumps(slots),
            "setup_complete": "true",
        })

        # Generate config file for shell scripts
        config.reload_site_config()
        generate_infra_config()

        conn = get_db()
        conn.execute(
            "INSERT OR IGNORE INTO users (email, first_name, last_name, password_hash, is_active, is_admin) VALUES (?, ?, ?, ?, 1, 1)",
            (admin_email_addr, admin_username, "", hash_password(admin_password))
        )
        conn.commit()
        conn.close()

        flash("Setup complete! You can now log in as admin.", "success")
        return redirect(url_for("login"))

    # GET — show setup form with defaults
    return render_template("setup.html", errors=[],
                           admin_user="admin", admin_email="",
                           email_domains="", smtp_host="localhost",
                           smtp_port="25", from_email="",
                           base_domain="example.com", lab_hostname="",
                           slots={"cluster1": 110, "cluster2": 120, "cluster3": 130})


# --- Routes ---

@app.route("/api/status")
def api_status():
    """JSON endpoint for live dashboard updates."""
    vms, clusters, resources = get_lab_status()
    clusters_data = {}
    for name, cvms in clusters.items():
        clusters_data[name] = cvms
    cluster_versions = get_cluster_versions(clusters)
    cluster_info = get_cluster_info(clusters)
    cluster_reservations = get_cluster_reservations()
    conn = get_db()
    total_deployments = conn.execute(
        "SELECT COUNT(*) FROM activity_log WHERE event='cluster_deploy'"
    ).fetchone()[0]
    conn.close()
    return jsonify(vms=vms, clusters=clusters_data, resources=resources,
                   cluster_versions=cluster_versions, cluster_info=cluster_info,
                   cluster_reservations=cluster_reservations,
                   total_deployments=total_deployments)


@app.route("/")
@setup_required
def index():
    if session.get("user_email"):
        return redirect(url_for("user_dashboard"))
    vms, clusters, resources = get_lab_status()
    cluster_versions = get_cluster_versions(clusters)
    ssh_user = ""
    if session.get("user_email"):
        ssh_user = derive_linux_username(session["user_email"])
    return render_template("index.html",
                           vms=vms, clusters=clusters, resources=resources,
                           ssh_user=ssh_user, base_domain=config.base_domain(),
                           cluster_versions=cluster_versions)



@app.route("/admin/maintenance", methods=["POST"])
@login_required
def admin_set_maintenance():
    msg = request.form.get("message", "").strip()
    if msg:
        config.set_site("maintenance_message", msg)
    else:
        conn = get_db()
        conn.execute("DELETE FROM admin_config WHERE key='maintenance_message'")
        conn.commit()
        conn.close()
        config._site_cache.pop("maintenance_message", None)
    return redirect(url_for("admin_panel"))


@app.route("/login")
def login():
    return redirect(url_for("user_login"))


@app.route("/logout")
def logout():
    return redirect(url_for("user_logout"))


@app.route("/admin")
@login_required
def admin_panel():
    conn = get_db()
    users = conn.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()
    conn.close()
    return render_template("admin.html", users=users)


@app.route("/admin/activity")
@user_login_required
def admin_activity():
    if not session.get("admin"):
        abort(403)
    event_filter = request.args.get("event", "")
    page = max(1, request.args.get("page", 1, type=int))
    per_page = 50

    conn = get_db()
    event_types = [r[0] for r in conn.execute(
        "SELECT DISTINCT event FROM activity_log ORDER BY event"
    ).fetchall()]

    if event_filter:
        total = conn.execute(
            "SELECT COUNT(*) FROM activity_log WHERE event=?", (event_filter,)
        ).fetchone()[0]
        logs = conn.execute(
            "SELECT * FROM activity_log WHERE event=? ORDER BY timestamp DESC LIMIT ? OFFSET ?",
            (event_filter, per_page, (page - 1) * per_page)
        ).fetchall()
    else:
        total = conn.execute("SELECT COUNT(*) FROM activity_log").fetchone()[0]
        logs = conn.execute(
            "SELECT * FROM activity_log ORDER BY timestamp DESC LIMIT ? OFFSET ?",
            (per_page, (page - 1) * per_page)
        ).fetchall()
    conn.close()

    total_pages = max(1, (total + per_page - 1) // per_page)
    return render_template("activity_log.html", logs=logs, total=total,
                           event_types=event_types, event_filter=event_filter,
                           page=page, total_pages=total_pages)


@app.route("/admin/user/<int:user_id>/toggle", methods=["POST"])
@login_required
def admin_toggle_user(user_id):
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not user:
        conn.close()
        abort(404)
    new_status = 0 if user["is_active"] else 1
    conn.execute("UPDATE users SET is_active=? WHERE id=?", (new_status, user_id))
    conn.commit()
    conn.close()
    action = "activated" if new_status else "deactivated"
    flash(f"User {user['first_name']} {user['last_name']} ({user['email']}) {action}.", "success")
    return redirect(url_for("admin_panel", status="all"))


@app.route("/admin/user/<int:user_id>/reset-password", methods=["POST"])
@login_required
def admin_reset_password(user_id):
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not user:
        conn.close()
        abort(404)
    password = generate_password()
    conn.execute("UPDATE users SET password_hash=? WHERE id=?",
                 (hash_password(password), user_id))
    conn.commit()
    conn.close()
    full_name = f"{user['first_name']} {user['last_name']}"
    send_password_reset_notification(user["email"], full_name, password)
    flash(f"Password reset for {full_name}. New credentials emailed to {user['email']}.", "success")
    return redirect(url_for("admin_panel", status="all"))


def derive_linux_username(email):
    """Derive Linux username from email: part before @, lowercase."""
    local = email.split("@")[0].lower()
    # Sanitize: only allow alphanumeric, dots, hyphens, underscores
    clean = "".join(c for c in local if c.isalnum() or c in ".-_")
    return clean[:32]  # Linux username max 32 chars


def create_linux_user(username, first_name, last_name):
    """Create a Linux user account with password policies.

    - Force password change on first login (chage -d 0)
    - Password expires after 180 days (chage -M 180)
    - Account locks after 30 days of inactivity (chage -I 30)
    - User added to 'labusers' group (restricted from /kvm)
    """
    errors = []

    # Ensure labusers group exists
    try:
        subprocess.run(["getent", "group", "labusers"],
                       capture_output=True, timeout=5)
    except Exception:
        pass
    subprocess.run(["groupadd", "-f", "labusers"],
                   capture_output=True, timeout=5)

    # Check if user already exists
    result = subprocess.run(["id", username], capture_output=True, timeout=5)
    if result.returncode == 0:
        return True, f"Linux user '{username}' already exists"

    # Create user with comment (full name), home dir, labusers group
    full_name = f"{first_name} {last_name}"
    result = subprocess.run(
        ["useradd", "-m", "-c", full_name, "-G", "labusers,libvirt", "-s", "/bin/bash", username],
        capture_output=True, text=True, timeout=10
    )
    if result.returncode != 0:
        return False, f"useradd failed: {result.stderr.strip()}"

    # Generate temporary password
    temp_password = generate_password(16)
    proc = subprocess.run(
        ["chpasswd"],
        input=f"{username}:{temp_password}",
        capture_output=True, text=True, timeout=10
    )
    if proc.returncode != 0:
        errors.append(f"chpasswd failed: {proc.stderr.strip()}")

    # Password expires after 180 days
    subprocess.run(["chage", "-M", "180", username],
                   capture_output=True, timeout=5)
    # Account locks after 30 days of inactivity
    subprocess.run(["chage", "-I", "30", username],
                   capture_output=True, timeout=5)

    # Restrict access to /kvm — deny labusers group
    if os.path.isdir("/kvm"):
        subprocess.run(
            ["setfacl", "-m", f"u:{username}:r-x", "/kvm"],
            capture_output=True, timeout=5
        )
        # Recursively deny write/delete on /kvm subdirectories
        subprocess.run(
            ["setfacl", "-R", "-m", f"u:{username}:r-X", "/kvm"],
            capture_output=True, timeout=5
        )

    if errors:
        return True, f"User created with warnings: {'; '.join(errors)}"
    return True, temp_password




# --- User Auth ---

@app.route("/user/forgot-password", methods=["GET", "POST"])
@setup_required
def forgot_password():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        conn = get_db()
        user = conn.execute("SELECT * FROM users WHERE email=? AND is_active=1",
                            (email,)).fetchone()
        if user:
            token = secrets.token_urlsafe(32)
            expires = datetime.utcnow().isoformat(timespec="seconds")
            conn.execute(
                "INSERT OR REPLACE INTO password_resets (email, token, expires_at) "
                "VALUES (?, ?, datetime(?, '+1 hour'))",
                (email, token, expires)
            )
            conn.commit()
            send_reset_token_email(email,
                                   f"{user['first_name']} {user['last_name']}",
                                   token)
        conn.close()
        flash("If that email exists in our system, a reset link has been sent.", "success")
        return redirect(url_for("user_login"))
    return render_template("forgot_password.html")


@app.route("/user/reset-password/<token>", methods=["GET", "POST"])
@setup_required
def reset_password(token):
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM password_resets WHERE token=? AND expires_at > datetime('now')",
        (token,)
    ).fetchone()
    if not row:
        conn.close()
        flash("Invalid or expired reset link. Please request a new one.", "danger")
        return redirect(url_for("forgot_password"))

    if request.method == "POST":
        password = request.form.get("password", "")
        password2 = request.form.get("password2", "")
        if len(password) < 8:
            flash("Password must be at least 8 characters.", "danger")
            return render_template("reset_password.html", token=token)
        if password != password2:
            flash("Passwords do not match.", "danger")
            return render_template("reset_password.html", token=token)

        conn.execute("UPDATE users SET password_hash=? WHERE email=?",
                     (hash_password(password), row["email"]))
        conn.execute("DELETE FROM password_resets WHERE email=?", (row["email"],))
        conn.commit()
        conn.close()
        flash("Password updated. You can now log in.", "success")
        return redirect(url_for("user_login"))

    conn.close()
    return render_template("reset_password.html", token=token)


@app.route("/user/login", methods=["GET", "POST"])
@setup_required
def user_login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        conn = get_db()
        user = conn.execute(
            "SELECT * FROM users WHERE email=? AND is_active=1", (email,)
        ).fetchone()
        conn.close()

        if user and verify_password(password, user["password_hash"]):
            session["user_email"] = user["email"]
            session["user_name"] = f"{user['first_name']} {user['last_name']}"
            if user["is_admin"]:
                session["admin"] = True
            log_activity("login", f"{user['first_name']} {user['last_name']}")
            return redirect(url_for("user_dashboard"))
        else:
            flash("Invalid email or password.", "danger")

    return render_template("user_login.html")


@app.route("/user/logout")
def user_logout():
    log_activity("logout")
    session.pop("user_email", None)
    session.pop("user_name", None)
    session.pop("admin", None)
    return redirect(url_for("index"))


def _find_next_ipi_offset(clusters):
    """Find the next available IPI IP offset (blocks of 10 from 140-190)."""
    # Collect used offsets from DB
    conn = get_db()
    rows = conn.execute(
        "SELECT ip_offset FROM deployments WHERE install_type='ipi' AND status IN ('deploying','completed')"
    ).fetchall()
    conn.close()
    used_offsets = {row["ip_offset"] for row in rows if row["ip_offset"]}
    # Also check running VMs to catch manually deployed clusters
    for name in clusters:
        for vm in clusters[name]:
            if vm["state"] == "running":
                # Check if any VM IP falls in IPI range
                pass
    for offset in range(config.IPI_OFFSET_START, config.IPI_OFFSET_END + 1, config.IPI_OFFSET_STEP):
        if offset not in used_offsets:
            return offset
    return None


def _check_resources(install_type):
    """Check if enough CPU and RAM are available for the given install type."""
    _, _, resources = get_lab_status()
    cpus_total = int(resources.get("cpus", 0))
    cpus_used = int(resources.get("cpus_used", 0))
    ram_total = int(resources.get("ram_total", 0))
    ram_used = int(resources.get("ram_used", 0))

    itype = config.INSTALL_TYPES.get(install_type, {})
    cpus_free = cpus_total - cpus_used
    ram_free = ram_total - ram_used

    if cpus_free < itype.get("vcpus", 0):
        return False, f"Not enough CPUs: {cpus_free} free, need {itype['vcpus']}"
    if ram_free < itype.get("ram_gb", 0):
        return False, f"Not enough RAM: {ram_free}G free, need {itype['ram_gb']}G"
    return True, "OK"


@app.route("/user/dashboard")
@user_login_required
def user_dashboard():
    vms, clusters, resources = get_lab_status()
    slots = config.cluster_slots()
    available_slots = sorted(name for name in slots if name not in clusters)
    ssh_user = derive_linux_username(session.get("user_email", ""))
    domain = config.base_domain()
    cluster_versions = get_cluster_versions(clusters)
    cluster_info = get_cluster_info(clusters)
    cluster_reservations = get_cluster_reservations()
    conn = get_db()
    total_deployments = conn.execute(
        "SELECT COUNT(*) FROM activity_log WHERE event='cluster_deploy'"
    ).fetchone()[0]
    conn.close()
    return render_template("user_dashboard.html",
                           vms=vms, clusters=clusters, resources=resources,
                           cluster_slots=sorted(slots.keys()),
                           available_slots=available_slots,
                           install_types=config.INSTALL_TYPES,
                           ssh_user=ssh_user, base_domain=domain,
                           cluster_versions=cluster_versions,
                           cluster_info=cluster_info,
                           cluster_reservations=cluster_reservations,
                           total_deployments=total_deployments)


# --- Cluster Management ---

@app.route("/cluster/kubeconfig/<cluster_name>")
@user_login_required
def cluster_kubeconfig(cluster_name):
    """Serve kubeconfig file for download."""
    import glob
    kubeconfig_path = None
    # Try DB first for the exact path
    conn = get_db()
    dep = conn.execute(
        "SELECT cluster_name, ocp_version FROM deployments WHERE cluster_name=? AND status IN ('deploying','completed') LIMIT 1",
        (cluster_name,)
    ).fetchone()
    conn.close()
    if dep:
        kubeconfig_path = f"/kvm/clusters/{dep['cluster_name']}-{dep['ocp_version']}/auth/kubeconfig"
    if not kubeconfig_path or not os.path.isfile(kubeconfig_path):
        # Fall back to scanning /kvm/clusters/<cluster_name>-*/auth/kubeconfig
        matches = glob.glob(f"/kvm/clusters/{cluster_name}-*/auth/kubeconfig")
        kubeconfig_path = max(matches, key=os.path.getmtime) if matches else None
    if not kubeconfig_path or not os.path.isfile(kubeconfig_path):
        flash(f"Kubeconfig not found for cluster '{cluster_name}'. Deployment may still be in progress.", "warning")
        return redirect(url_for("user_dashboard"))
    return send_file(kubeconfig_path, as_attachment=True,
                     download_name=f"kubeconfig-{cluster_name}")


@app.route("/cluster/create", methods=["POST"])
@user_login_required
def cluster_create():
    cluster_name = request.form.get("cluster_name", "").strip()
    ocp_version = request.form.get("ocp_version", "").strip()
    install_type = request.form.get("install_type", "upi").strip()
    network_type = request.form.get("network_type", "OVNKubernetes").strip()
    description = request.form.get("description", "").strip()[:80]

    if not cluster_name or not ocp_version:
        flash("Cluster name and OCP version are required.", "danger")
        return redirect(url_for("user_dashboard"))

    if network_type not in ("OVNKubernetes", "OpenShiftSDN"):
        flash("Invalid network type.", "danger")
        return redirect(url_for("user_dashboard"))

    # OpenShiftSDN only supported on OCP 4.14 and below
    if network_type == "OpenShiftSDN":
        try:
            major, minor = ocp_version.split(".")[:2]
            if int(major) > 4 or (int(major) == 4 and int(minor) > 14):
                flash("OpenShiftSDN is only available on OCP 4.14 and below. Use OVNKubernetes for newer versions.", "danger")
                return redirect(url_for("user_dashboard"))
        except (ValueError, IndexError):
            pass

    if install_type not in config.INSTALL_TYPES:
        flash(f"Invalid install type '{install_type}'.", "danger")
        return redirect(url_for("user_dashboard"))

    itype = config.INSTALL_TYPES[install_type]
    vms, clusters, _ = get_lab_status()

    if install_type == "upi":
        # UPI: validate cluster_name is a configured slot
        slots = config.cluster_slots()
        if cluster_name not in slots:
            flash(f"Invalid cluster slot '{cluster_name}'. Choose from: {', '.join(sorted(slots))}.", "danger")
            return redirect(url_for("user_dashboard"))
        ip_offset = slots[cluster_name]
    else:
        # IPI (and future types): cluster_name is user-provided
        if not re.match(r'^[a-z0-9][a-z0-9\-]{0,14}$', cluster_name):
            flash("Cluster name must be lowercase alphanumeric (may include hyphens), 1-15 characters.", "danger")
            return redirect(url_for("user_dashboard"))
        ip_offset = _find_next_ipi_offset(clusters)
        if ip_offset is None:
            flash("No IPI IP offset slots available. Delete an existing IPI cluster first.", "danger")
            return redirect(url_for("user_dashboard"))

    # Check if cluster already exists (VMs running)
    if cluster_name in clusters:
        flash(f"Cluster '{cluster_name}' already exists.", "warning")
        return redirect(url_for("user_dashboard"))

    # Check if another cluster's bootstrap is still running
    for vm in vms:
        if "bootstrap" in vm["name"] and vm["state"] == "running":
            flash(f"Another deployment is in progress ({vm['name']} is still running). "
                  "Please wait for it to finish before deploying a new cluster.", "warning")
            return redirect(url_for("user_dashboard"))

    # Resource check
    ok, msg = _check_resources(install_type)
    if not ok:
        flash(f"Cannot deploy {itype['label']}: {msg}", "danger")
        return redirect(url_for("user_dashboard"))

    # Validate OCP version exists on the mirror
    mirror_url = f"https://mirror.openshift.com/pub/openshift-v4/clients/ocp/{ocp_version}/"
    try:
        req = urllib.request.Request(mirror_url, method="HEAD")
        resp = urllib.request.urlopen(req, timeout=10)
        if resp.status != 200:
            flash(f"OCP version {ocp_version} not found on the mirror.", "danger")
            return redirect(url_for("user_dashboard"))
    except urllib.error.HTTPError:
        flash(f"OCP version {ocp_version} is not available. Check https://mirror.openshift.com/pub/openshift-v4/clients/ocp/ for valid versions.", "danger")
        return redirect(url_for("user_dashboard"))
    except Exception:
        pass  # Network issue — let the deploy script handle it

    # Select deploy script for this install type
    deploy_script = itype["script"]

    # Start deployment in background, detached from portal process
    log_file = f"/tmp/deploy-{cluster_name}-{ocp_version}.log"
    try:
        env = os.environ.copy()
        env["BASE_DOMAIN"] = config.base_domain()
        proc = subprocess.Popen(
            [deploy_script, ocp_version, cluster_name, str(ip_offset), network_type],
            stdout=open(log_file, "w"),
            stderr=subprocess.STDOUT,
            cwd="/root",
            start_new_session=True,
            env=env
        )
        conn = get_db()
        conn.execute(
            "INSERT INTO deployments (cluster_name, ocp_version, status, started_by, pid, log_file, ip_offset, install_type, description) "
            "VALUES (?, ?, 'deploying', ?, ?, ?, ?, ?, ?)",
            (cluster_name, ocp_version, session.get("user_email"), proc.pid, log_file, ip_offset, install_type, description)
        )
        conn.commit()
        conn.close()
        log_activity("cluster_deploy", f"{cluster_name} {install_type.upper()} OCP {ocp_version}")
        flash(f"Cluster '{cluster_name}' ({itype['label']}) deployment started (OCP {ocp_version}). You will be notified via email upon successful installation.", "success")
    except Exception as e:
        flash(f"Failed to start deployment: {e}", "danger")

    return redirect(url_for("user_dashboard"))


@app.route("/cluster/delete", methods=["POST"])
@user_login_required
def cluster_delete():
    cluster_name = request.form.get("cluster_name", "").strip()
    if not cluster_name:
        flash("Cluster name is required.", "danger")
        return redirect(url_for("user_dashboard"))

    vms, clusters, _ = get_lab_status()
    if cluster_name not in clusters:
        flash(f"Cluster '{cluster_name}' not found.", "warning")
        return redirect(url_for("user_dashboard"))

    # Non-admin users can only delete clusters they created
    if not session.get("admin"):
        conn = get_db()
        dep = conn.execute(
            "SELECT started_by FROM deployments WHERE cluster_name=? AND status IN ('deploying','completed') LIMIT 1",
            (cluster_name,)
        ).fetchone()
        conn.close()
        if dep and dep["started_by"] != session.get("user_email"):
            flash("You can only delete clusters you created.", "danger")
            return redirect(url_for("user_dashboard"))

    # Look up install_type and ip_offset from DB
    conn = get_db()
    dep = conn.execute(
        "SELECT install_type, ip_offset FROM deployments WHERE cluster_name=? AND status IN ('deploying','completed') LIMIT 1",
        (cluster_name,)
    ).fetchone()
    conn.close()
    dep_install_type = dep["install_type"] if dep and dep["install_type"] else "upi"
    dep_ip_offset = dep["ip_offset"] if dep else None

    errors = []
    for vm in clusters[cluster_name]:
        try:
            subprocess.run(["virsh", "destroy", vm["name"]],
                           capture_output=True, timeout=10)
        except Exception:
            pass
        try:
            result = subprocess.run(
                ["virsh", "undefine", vm["name"], "--remove-all-storage"],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode != 0:
                errors.append(f"{vm['name']}: {result.stderr.strip()}")
        except Exception as e:
            errors.append(f"{vm['name']}: {e}")

    # IPI-specific cleanup: VBMC, DHCP reservations, DNS blocks
    if dep_install_type == "ipi" and dep_ip_offset is not None:
        vm_prefix = f"vm-{cluster_name}"
        mac_base = f"{dep_ip_offset:02x}"
        num_masters = 3
        num_workers = 2
        vbmc_port_base = 6200 + dep_ip_offset - 100

        # Stop/delete VBMC entries (masters + workers)
        for role, count, port_offset in [("master", num_masters, 0), ("worker", num_workers, num_masters)]:
            for i in range(count):
                vm_name = f"{vm_prefix}-{role}-{i}"
                try:
                    subprocess.run(["vbmc", "stop", vm_name],
                                   capture_output=True, timeout=10)
                except Exception:
                    pass
                try:
                    subprocess.run(["vbmc", "delete", vm_name],
                                   capture_output=True, timeout=10)
                except Exception:
                    pass

        # Remove DHCP reservations (masters: 0x11+i, workers: 0x21+i)
        for base_byte, count in [(0x11, num_masters), (0x21, num_workers)]:
            for i in range(count):
                bm_mac = f"52:54:00:{mac_base}:01:{base_byte + i:02x}"
                try:
                    subprocess.run(
                        ["virsh", "net-update", "default", "delete", "ip-dhcp-host",
                         f"<host mac='{bm_mac}'/>", "--live", "--config"],
                        capture_output=True, timeout=10
                    )
                except Exception:
                    pass

        # Remove DNS blocks from IPI include files
        fwd_zone = "/var/named/ipi-forward.include"
        rev_zone = "/var/named/ipi-reverse.include"
        for zone_file in (fwd_zone, rev_zone):
            if os.path.isfile(zone_file):
                try:
                    subprocess.run(
                        ["sed", "-i", f"/^; IPI-START {cluster_name}$/,/^; IPI-END {cluster_name}$/d", zone_file],
                        capture_output=True, timeout=10
                    )
                except Exception as e:
                    errors.append(f"DNS cleanup {zone_file}: {e}")

        # Clean up libvirt storage pools created by IPI installer
        try:
            result = subprocess.run(["virsh", "pool-list", "--all", "--name"],
                                    capture_output=True, text=True, timeout=10)
            for pool_name in result.stdout.strip().splitlines():
                pool_name = pool_name.strip()
                if not pool_name or pool_name in ("default", "images"):
                    continue
                if cluster_name in pool_name:
                    subprocess.run(["virsh", "pool-destroy", pool_name],
                                   capture_output=True, timeout=10)
                    subprocess.run(["virsh", "pool-undefine", pool_name],
                                   capture_output=True, timeout=10)
        except Exception as e:
            errors.append(f"pool cleanup: {e}")

        # Clean up bootstrap images from /kvm/libvirt-images (symlinked from /var/lib/libvirt/openshift-images)
        for img_dir in glob.glob(f"/kvm/libvirt-images/{cluster_name}-*"):
            if os.path.isdir(img_dir):
                try:
                    shutil.rmtree(img_dir)
                except Exception as e:
                    errors.append(f"bootstrap image cleanup: {e}")

        # Reload named to pick up zone changes
        try:
            subprocess.run(["systemctl", "reload", "named"],
                           capture_output=True, timeout=10)
        except Exception:
            pass

    # Delete deployment records and log files
    conn = get_db()
    rows = conn.execute(
        "SELECT log_file FROM deployments WHERE cluster_name=?", (cluster_name,)
    ).fetchall()
    for row in rows:
        if row["log_file"]:
            try:
                os.remove(row["log_file"])
            except OSError:
                pass
    conn.execute("DELETE FROM deployments WHERE cluster_name=?", (cluster_name,))
    conn.commit()
    conn.close()

    # Clean up cluster directories under /kvm/clusters/<name>-*
    # (master ISOs in /kvm/client_tools/ are preserved)
    for cluster_dir in glob.glob(f"/kvm/clusters/{cluster_name}-*"):
        if os.path.isdir(cluster_dir):
            try:
                shutil.rmtree(cluster_dir)
            except Exception as e:
                errors.append(f"cleanup {cluster_dir}: {e}")

    # Release any reservation on this cluster
    conn = get_db()
    conn.execute("DELETE FROM cluster_reservations WHERE cluster_name=?", (cluster_name,))
    conn.commit()
    conn.close()
    _write_reservation_file()

    # Refresh MOTD to reflect the change
    subprocess.Popen(["/root/labs/update-motd.sh"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    log_activity("cluster_delete", cluster_name)
    if errors:
        flash(f"Cluster '{cluster_name}' partially deleted. Errors: {'; '.join(errors)}", "warning")
    else:
        flash(f"Cluster '{cluster_name}' deleted successfully.", "success")

    return redirect(url_for("user_dashboard"))


@app.route("/cluster/reserve", methods=["POST"])
@user_login_required
def cluster_reserve():
    cluster_name = request.form.get("cluster_name", "").strip()
    purpose = request.form.get("purpose", "").strip()[:80]
    duration = request.form.get("duration", "4").strip()

    if not cluster_name:
        flash("Cluster name is required.", "danger")
        return redirect(url_for("user_dashboard"))

    try:
        hours = int(duration)
        if hours < 1 or hours > 168:
            raise ValueError
    except ValueError:
        flash("Invalid duration. Choose 1-168 hours.", "danger")
        return redirect(url_for("user_dashboard"))

    conn = get_db()
    conn.execute("DELETE FROM cluster_reservations WHERE reserved_until < datetime('now')")
    existing = conn.execute(
        "SELECT reserved_by FROM cluster_reservations WHERE cluster_name=?",
        (cluster_name,)
    ).fetchone()
    if existing:
        conn.close()
        flash(f"Cluster '{cluster_name}' is already reserved by {existing['reserved_by'].split('@')[0]}.", "warning")
        return redirect(url_for("user_dashboard"))

    import sqlite3 as _sqlite3
    try:
        conn.execute(
            "INSERT INTO cluster_reservations (cluster_name, reserved_by, purpose, reserved_until) "
            "VALUES (?, ?, ?, datetime('now', '+' || ? || ' hours'))",
            (cluster_name, session.get("user_email"), purpose, str(hours))
        )
        conn.commit()
    except _sqlite3.IntegrityError:
        conn.close()
        flash(f"Cluster '{cluster_name}' was just reserved by someone else.", "warning")
        return redirect(url_for("user_dashboard"))
    conn.close()

    log_activity("cluster_reserve", f"{cluster_name} for {hours}h: {purpose}")
    _write_reservation_file()
    flash(f"Cluster '{cluster_name}' reserved for {hours} hours.", "success")
    return redirect(url_for("user_dashboard"))


@app.route("/cluster/release", methods=["POST"])
@user_login_required
def cluster_release():
    cluster_name = request.form.get("cluster_name", "").strip()
    if not cluster_name:
        flash("Cluster name is required.", "danger")
        return redirect(url_for("user_dashboard"))

    conn = get_db()
    existing = conn.execute(
        "SELECT reserved_by FROM cluster_reservations WHERE cluster_name=?",
        (cluster_name,)
    ).fetchone()
    if not existing:
        conn.close()
        flash(f"Cluster '{cluster_name}' is not reserved.", "info")
        return redirect(url_for("user_dashboard"))

    if existing["reserved_by"] != session.get("user_email") and not session.get("admin"):
        conn.close()
        flash("Only the reserver or an admin can release this reservation.", "danger")
        return redirect(url_for("user_dashboard"))

    conn.execute("DELETE FROM cluster_reservations WHERE cluster_name=?", (cluster_name,))
    conn.commit()
    conn.close()

    log_activity("cluster_release", cluster_name)
    _write_reservation_file()
    flash(f"Reservation for '{cluster_name}' released.", "success")
    return redirect(url_for("user_dashboard"))


@app.route("/cluster/logs/<cluster_name>")
def cluster_logs(cluster_name):
    if not session.get("user_email") and not session.get("admin"):
        return redirect(url_for("user_login"))
    conn = get_db()
    dep = conn.execute(
        "SELECT * FROM deployments WHERE cluster_name=? ORDER BY started_at DESC LIMIT 1",
        (cluster_name,)
    ).fetchone()
    conn.close()

    if not dep or not dep["log_file"]:
        flash(f"No logs found for cluster '{cluster_name}'.", "warning")
        if session.get("user_email"):
            return redirect(url_for("user_dashboard"))
        return redirect(url_for("index"))

    try:
        with open(dep["log_file"], "r") as f:
            lines = f.readlines()
            tail = lines[-200:] if len(lines) > 200 else lines
        log_content = "".join(tail)
    except FileNotFoundError:
        log_content = "Log file not found."

    return render_template("cluster_logs.html", deployment=dep, log_content=log_content)


# --- Web Terminal ---

@app.route("/user/terminal")
@user_login_required
def user_terminal():
    cluster = request.args.get("cluster", "")
    return render_template("terminal.html", cluster=cluster)


def _set_terminal_size(fd, rows, cols):
    winsize = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)


def _read_pty_output(sid, fd):
    """Background thread: read from PTY fd and emit to WebSocket."""
    while True:
        try:
            ready, _, _ = select.select([fd], [], [], 0.1)
            if ready:
                data = os.read(fd, 4096)
                if data:
                    socketio.emit("pty_output",
                                  {"output": data.decode("utf-8", errors="replace")},
                                  namespace="/terminal", to=sid)
                else:
                    break
        except OSError:
            break
    socketio.emit("pty_output", {"output": "\r\n[Session ended]\r\n"},
                  namespace="/terminal", to=sid)


def _cleanup_terminal(sid):
    sess = terminal_sessions.pop(sid, None)
    if sess:
        try:
            os.kill(sess["pid"], signal.SIGHUP)
        except OSError:
            pass
        try:
            os.close(sess["fd"])
        except OSError:
            pass


def _find_kubeconfig(cluster_name):
    """Find kubeconfig path for a cluster (e.g. upi1 -> /kvm/clusters/upi1-*/auth/kubeconfig)."""
    import glob
    matches = glob.glob(f"/kvm/clusters/{cluster_name}-*/auth/kubeconfig")
    if matches:
        # Sort by mtime, newest first
        matches.sort(key=os.path.getmtime, reverse=True)
        return matches[0]
    return None


@socketio.on("connect", namespace="/terminal")
def terminal_connect(auth=None):
    user_email = session.get("user_email")
    if not user_email:
        disconnect()
        return

    # All terminal sessions run as the shared 'ocpterm' user
    linux_user = "ocpterm"
    cluster = (auth or {}).get("cluster", "") if auth else ""

    pid, fd = pty.fork()
    if pid == 0:
        # Child — become ocpterm with TERM set for curses (watch, top, vi)
        os.environ["TERM"] = "xterm-256color"
        os.execlp("su", "su", "-", linux_user, "-w", "TERM")
    else:
        import time as _time
        terminal_sessions[request.sid] = {
            "fd": fd, "pid": pid,
            "last_activity": _time.time(), "warned": False
        }
        _set_terminal_size(fd, 24, 80)
        socketio.start_background_task(_read_pty_output, request.sid, fd)

        # Auto-export KUBECONFIG if cluster was specified
        if cluster:
            kc_path = _find_kubeconfig(cluster)
            if kc_path:
                import time
                time.sleep(0.5)  # wait for shell to be ready
                cmd = f"export KUBECONFIG={kc_path}\n"
                try:
                    os.write(fd, cmd.encode())
                except OSError:
                    pass

        log_activity("terminal_open", f"{user_email} cluster={cluster}" if cluster else user_email)


@socketio.on("pty_input", namespace="/terminal")
def terminal_input(data):
    sess = terminal_sessions.get(request.sid)
    if sess:
        import time as _time
        sess["last_activity"] = _time.time()
        sess["warned"] = False
        try:
            os.write(sess["fd"], data["input"].encode("utf-8"))
        except OSError:
            pass


@socketio.on("resize", namespace="/terminal")
def terminal_resize(data):
    sess = terminal_sessions.get(request.sid)
    if sess:
        try:
            _set_terminal_size(sess["fd"], data["rows"], data["cols"])
        except OSError:
            pass


@socketio.on("disconnect", namespace="/terminal")
def terminal_disconnect():
    _cleanup_terminal(request.sid)


def _terminal_reaper():
    """Background thread: warn idle sessions and kill timed-out ones."""
    import time as _time
    while True:
        _time.sleep(60)  # check every minute
        now = _time.time()
        for sid, sess in list(terminal_sessions.items()):
            idle = now - sess["last_activity"]

            # Kill sessions idle beyond timeout
            if idle >= TERMINAL_TIMEOUT:
                socketio.emit("pty_output",
                              {"output": "\r\n\033[1;31m[Session timed out after 1 hour of inactivity]\033[0m\r\n"},
                              namespace="/terminal", to=sid)
                _cleanup_terminal(sid)
                socketio.emit("pty_output",
                              {"output": "\r\n[Disconnected]\r\n"},
                              namespace="/terminal", to=sid)
                continue

            # Warn 5 minutes before timeout
            if idle >= (TERMINAL_TIMEOUT - TERMINAL_WARN_BEFORE) and not sess.get("warned"):
                mins_left = max(1, int((TERMINAL_TIMEOUT - idle) / 60))
                socketio.emit("pty_output",
                              {"output": f"\r\n\033[1;33m[Warning: session will timeout in ~{mins_left} min due to inactivity]\033[0m\r\n"},
                              namespace="/terminal", to=sid)
                sess["warned"] = True


# Start the reaper thread
threading.Thread(target=_terminal_reaper, daemon=True).start()


# --- CLI ---

def cli_set_password():
    import getpass
    init_db()
    pw = getpass.getpass("Set admin password: ")
    pw2 = getpass.getpass("Confirm: ")
    if pw != pw2:
        print("Passwords don't match.")
        sys.exit(1)
    if len(pw) < 8:
        print("Password must be at least 8 characters.")
        sys.exit(1)
    set_admin_password(pw)
    print(f"Admin password set for user '{config.admin_user()}'.")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "set-password":
        cli_set_password()
    else:
        init_db()
        if not config.is_setup_complete():
            print("First-run setup not completed. Visit /labs/setup in the browser.")
        socketio.run(app, host="127.0.0.1", port=5000, debug=False,
                     allow_unsafe_werkzeug=True)
