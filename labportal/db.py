import sqlite3
import os
from config import DB_PATH


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = get_db()
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

        CREATE TABLE IF NOT EXISTS password_resets (
            email TEXT PRIMARY KEY,
            token TEXT NOT NULL,
            expires_at TIMESTAMP NOT NULL
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

    conn.commit()
    conn.close()
