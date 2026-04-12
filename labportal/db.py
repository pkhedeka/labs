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
        CREATE TABLE IF NOT EXISTS access_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            first_name TEXT NOT NULL,
            last_name TEXT NOT NULL,
            email TEXT NOT NULL,
            reason TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            resolved_at TIMESTAMP,
            admin_note TEXT
        );

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
    """)

    # Migrations for existing databases
    try:
        conn.execute("SELECT first_name FROM access_requests LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE access_requests ADD COLUMN first_name TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE access_requests ADD COLUMN last_name TEXT NOT NULL DEFAULT ''")
        # Migrate: split existing 'name' into first/last
        rows = conn.execute("SELECT id, name FROM access_requests").fetchall()
        for row in rows:
            parts = row["name"].split(None, 1)
            first = parts[0] if parts else row["name"]
            last = parts[1] if len(parts) > 1 else ""
            conn.execute("UPDATE access_requests SET first_name=?, last_name=? WHERE id=?",
                         (first, last, row["id"]))
        conn.commit()

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

    conn.commit()
    conn.close()
