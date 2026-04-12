#!/usr/bin/env python3
"""
Lab Portal — lightweight web app for managing OCP lab access requests.
"""
import hashlib
import json
import os
import secrets
import subprocess
import sys
from datetime import datetime
from functools import wraps

from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, session, abort, jsonify
)

import config
from db import get_db, init_db
from mail import send_admin_notification, send_user_approved, send_user_denied

app = Flask(__name__)
app.secret_key = config.SECRET_KEY


@app.context_processor
def inject_globals():
    """Make hostname available in all templates."""
    return {"hostname": config.lab_hostname()}


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
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


def user_login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_email"):
            return redirect(url_for("user_login"))
        return f(*args, **kwargs)
    return decorated


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
        if not admin_password or len(admin_password) < 6:
            errors.append("Admin password must be at least 6 characters.")
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
    return jsonify(vms=vms, clusters=clusters_data, resources=resources)


@app.route("/")
@setup_required
def index():
    conn = get_db()
    stats = {
        "total": conn.execute("SELECT COUNT(*) FROM access_requests").fetchone()[0],
        "pending": conn.execute(
            "SELECT COUNT(*) FROM access_requests WHERE status='pending'"
        ).fetchone()[0],
    }
    conn.close()
    vms, clusters, resources = get_lab_status()
    return render_template("index.html", stats=stats,
                           vms=vms, clusters=clusters, resources=resources)


@app.route("/request", methods=["GET", "POST"])
@setup_required
def request_access():
    if request.method == "POST":
        first_name = request.form.get("first_name", "").strip()
        last_name = request.form.get("last_name", "").strip()
        email = request.form.get("email", "").strip()
        reason = request.form.get("reason", "").strip()

        errors = []
        if not first_name or len(first_name) < 2:
            errors.append("First name is required (at least 2 characters).")
        if not last_name:
            errors.append("Last name is required.")
        if not reason or len(reason) < 10:
            errors.append("Please provide a comment (at least 10 characters).")

        valid, result = validate_email(email)
        if not valid:
            errors.append(result)
        else:
            email = result

        if errors:
            return render_template("request_form.html", errors=errors,
                                   first_name=first_name, last_name=last_name,
                                   email=email, reason=reason)

        # Spam protection — one request per email per 24 hours
        conn = get_db()
        recent = conn.execute(
            "SELECT id FROM access_requests WHERE email=? AND created_at > datetime('now', '-24 hours')",
            (email,)
        ).fetchone()
        if recent:
            conn.close()
            flash("You can only submit one request per 24 hours. Please try again later.", "warning")
            return redirect(url_for("request_access"))

        # Check for duplicate pending requests
        existing = conn.execute(
            "SELECT id FROM access_requests WHERE email=? AND status='pending'",
            (email,)
        ).fetchone()
        if existing:
            conn.close()
            flash("You already have a pending request. Please wait for it to be reviewed.", "warning")
            return redirect(url_for("request_access"))

        conn.execute(
            "INSERT INTO access_requests (first_name, last_name, email, reason) VALUES (?, ?, ?, ?)",
            (first_name, last_name, email, reason)
        )
        conn.commit()
        conn.close()

        full_name = f"{first_name} {last_name}"
        send_admin_notification(full_name, email, reason)
        flash("Your request has been submitted. You'll receive an email once it's reviewed.", "success")
        return redirect(url_for("index"))

    return render_template("request_form.html", errors=[],
                           first_name="", last_name="", email="", reason="")


@app.route("/login", methods=["GET", "POST"])
@setup_required
def login():
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")

        stored_hash = get_admin_password_hash()
        if not stored_hash:
            flash("Admin password not set. Complete the setup wizard first.", "danger")
            return render_template("login.html")

        if username == config.admin_user() and verify_password(password, stored_hash):
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
    users = conn.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()
    conn.close()
    return render_template("admin.html", requests=rows, status_filter=status_filter, users=users)


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
        ["useradd", "-m", "-c", full_name, "-G", "labusers", "-s", "/bin/bash", username],
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

    # Force password change on first login
    subprocess.run(["chage", "-d", "0", username],
                   capture_output=True, timeout=5)
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

    full_name = f"{row['first_name']} {row['last_name']}"

    if action == "approve":
        password = generate_password()
        pw_hash = hash_password(password)
        linux_user = derive_linux_username(row["email"])

        conn2 = get_db()
        existing_user = conn2.execute(
            "SELECT id FROM users WHERE email=?", (row["email"],)
        ).fetchone()
        if existing_user:
            conn2.execute(
                "UPDATE users SET password_hash=?, is_active=1 WHERE email=?",
                (pw_hash, row["email"])
            )
        else:
            conn2.execute(
                "INSERT INTO users (email, first_name, last_name, linux_username, password_hash) "
                "VALUES (?, ?, ?, ?, ?)",
                (row["email"], row["first_name"], row["last_name"], linux_user, pw_hash)
            )
        conn2.commit()
        conn2.close()

        # Create Linux system account
        success, linux_result = create_linux_user(linux_user, row["first_name"], row["last_name"])
        if success and linux_result != f"Linux user '{linux_user}' already exists":
            # linux_result is the temp password
            temp_pw = linux_result
            send_user_approved(row["email"], full_name, note,
                               password=password, linux_username=linux_user,
                               linux_password=temp_pw)
            flash(f"Approved {full_name}. Portal + Linux account ({linux_user}) created.", "success")
        elif success:
            send_user_approved(row["email"], full_name, note, password=password,
                               linux_username=linux_user)
            flash(f"Approved {full_name}. Portal account created. Linux user '{linux_user}' already existed.", "success")
        else:
            send_user_approved(row["email"], full_name, note, password=password)
            flash(f"Approved {full_name}. Portal account created but Linux user creation failed: {linux_result}", "warning")
    else:
        send_user_denied(row["email"], full_name, note)
        flash(f"Denied request from {full_name}. Notification sent.", "info")

    return redirect(url_for("admin_panel"))


# --- User Auth ---

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
            return redirect(url_for("user_dashboard"))
        else:
            flash("Invalid email or password.", "danger")

    return render_template("user_login.html")


@app.route("/user/logout")
def user_logout():
    session.pop("user_email", None)
    session.pop("user_name", None)
    session.pop("admin", None)
    return redirect(url_for("index"))


@app.route("/user/dashboard")
@user_login_required
def user_dashboard():
    vms, clusters, resources = get_lab_status()
    slots = config.cluster_slots()
    available_slots = sorted(name for name in slots if name not in clusters)
    return render_template("user_dashboard.html",
                           vms=vms, clusters=clusters, resources=resources,
                           cluster_slots=sorted(slots.keys()),
                           available_slots=available_slots)


# --- Cluster Management ---

@app.route("/cluster/create", methods=["POST"])
@user_login_required
def cluster_create():
    cluster_name = request.form.get("cluster_name", "").strip()
    ocp_version = request.form.get("ocp_version", "").strip()

    if not cluster_name or not ocp_version:
        flash("Cluster name and OCP version are required.", "danger")
        return redirect(url_for("user_dashboard"))

    slots = config.cluster_slots()
    if cluster_name not in slots:
        flash(f"Invalid cluster slot '{cluster_name}'. Choose from: {', '.join(sorted(slots))}.", "danger")
        return redirect(url_for("user_dashboard"))

    ip_offset = slots[cluster_name]

    # Check if cluster already exists (VMs running)
    vms, clusters, _ = get_lab_status()
    if cluster_name in clusters:
        flash(f"Cluster '{cluster_name}' already exists.", "warning")
        return redirect(url_for("user_dashboard"))

    # Start deployment in background, detached from portal process
    log_file = f"/tmp/deploy-{cluster_name}-{ocp_version}.log"
    try:
        env = os.environ.copy()
        env["BASE_DOMAIN"] = config.base_domain()
        proc = subprocess.Popen(
            [config.DEPLOY_SCRIPT, ocp_version, cluster_name, str(ip_offset)],
            stdout=open(log_file, "w"),
            stderr=subprocess.STDOUT,
            cwd="/root",
            start_new_session=True,
            env=env
        )
        conn = get_db()
        conn.execute(
            "INSERT INTO deployments (cluster_name, ocp_version, status, started_by, pid, log_file, ip_offset) "
            "VALUES (?, ?, 'deploying', ?, ?, ?, ?)",
            (cluster_name, ocp_version, session.get("user_email"), proc.pid, log_file, ip_offset)
        )
        conn.commit()
        conn.close()
        flash(f"Cluster '{cluster_name}' deployment started (OCP {ocp_version}). You will be notified via email upon successful installation.", "success")
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

    if errors:
        flash(f"Cluster '{cluster_name}' partially deleted. Errors: {'; '.join(errors)}", "warning")
    else:
        flash(f"Cluster '{cluster_name}' deleted successfully.", "success")

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
    print(f"Admin password set for user '{config.admin_user()}'.")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "set-password":
        cli_set_password()
    else:
        init_db()
        if not config.is_setup_complete():
            print("First-run setup not completed. Visit /labs/setup in the browser.")
        app.run(host="127.0.0.1", port=5000, debug=False)
