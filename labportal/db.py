import sqlite3
import os
from contextlib import contextmanager
from config import DB_PATH


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def get_db_ctx():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def init_db():
    conn = get_db()
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS admin_config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            first_name TEXT NOT NULL,
            last_name TEXT NOT NULL,
            linux_username TEXT UNIQUE,
            password_hash TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1,
            is_admin INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS password_reset_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL,
            requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            status TEXT NOT NULL DEFAULT 'pending'
        );

        CREATE TABLE IF NOT EXISTS activity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            event TEXT NOT NULL,
            user_email TEXT,
            ip_address TEXT,
            details TEXT
        );

        CREATE TABLE IF NOT EXISTS deployments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cluster_name TEXT NOT NULL,
            ocp_version TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'deploying',
            started_by TEXT,
            started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            finished_at TIMESTAMP,
            pid INTEGER,
            log_file TEXT,
            ip_offset INTEGER
        );

        CREATE TABLE IF NOT EXISTS cluster_reservations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cluster_name TEXT NOT NULL UNIQUE,
            reserved_by TEXT NOT NULL,
            purpose TEXT NOT NULL DEFAULT '',
            reserved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            reserved_until TIMESTAMP NOT NULL
        );

        CREATE TABLE IF NOT EXISTS cluster_extension_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cluster_name TEXT NOT NULL,
            requested_by TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            status TEXT NOT NULL DEFAULT 'pending'
        );

        CREATE TABLE IF NOT EXISTS lab_machines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            hostname TEXT NOT NULL,
            ssh_user TEXT NOT NULL DEFAULT 'root',
            ssh_port INTEGER NOT NULL DEFAULT 22,
            role TEXT NOT NULL DEFAULT 'peer',
            status TEXT NOT NULL DEFAULT 'pending',
            status_detail TEXT DEFAULT '',
            specs_json TEXT DEFAULT '{}',
            added_by TEXT NOT NULL,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    # Migrations for existing databases
    try:
        conn.execute("SELECT first_name FROM users LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE users ADD COLUMN first_name TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE users ADD COLUMN last_name TEXT NOT NULL DEFAULT ''")

    try:
        conn.execute("SELECT linux_username FROM users LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE users ADD COLUMN linux_username TEXT")
        rows = conn.execute("SELECT id, name FROM users").fetchall()
        for row in rows:
            parts = row["name"].split(None, 1)
            first = parts[0] if parts else row["name"]
            last = parts[1] if len(parts) > 1 else ""
            conn.execute("UPDATE users SET first_name=?, last_name=? WHERE id=?",
                         (first, last, row["id"]))
        conn.commit()

    try:
        conn.execute("SELECT ip_offset FROM deployments LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE deployments ADD COLUMN ip_offset INTEGER")

    try:
        conn.execute("SELECT install_type FROM deployments LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE deployments ADD COLUMN install_type TEXT DEFAULT 'upi'")

    try:
        conn.execute("SELECT description FROM deployments LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE deployments ADD COLUMN description TEXT DEFAULT ''")

    try:
        conn.execute("SELECT must_change_password FROM users LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0")

    try:
        conn.execute("SELECT machine_id FROM deployments LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE deployments ADD COLUMN machine_id INTEGER")

    try:
        conn.execute("SELECT role FROM lab_machines LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE lab_machines ADD COLUMN role TEXT NOT NULL DEFAULT 'peer'")

    boss = conn.execute("SELECT id FROM lab_machines WHERE role='boss'").fetchone()
    if not boss:
        import subprocess as _sp
        specs = {"kvm": True, "libvirt": True}
        try:
            specs["cpus"] = int(_sp.run(["nproc"], capture_output=True, text=True).stdout.strip())
            for line in _sp.run(["free", "-g"], capture_output=True, text=True).stdout.splitlines():
                if line.startswith("Mem:"):
                    specs["ram_gb"] = int(line.split()[1])
            df = _sp.run(["df", "-BG", "--output=avail", "/kvm"], capture_output=True, text=True)
            specs["storage_gb"] = int(df.stdout.strip().splitlines()[-1].strip().rstrip("G"))
        except Exception:
            pass
        import json as _json
        import socket as _sock
        conn.execute(
            "INSERT OR IGNORE INTO lab_machines (name, hostname, role, status, specs_json, added_by) "
            "VALUES (?, ?, 'boss', 'ready', ?, 'system')",
            (_sock.gethostname().split(".")[0], "localhost", _json.dumps(specs))
        )

    conn.commit()
    conn.close()
