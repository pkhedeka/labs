import json
import os
import secrets

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Flask secret — only this and DB_PATH remain as file-level constants.
# Everything else is stored in the database after first-run setup.
SECRET_KEY = os.environ.get("LABPORTAL_SECRET_KEY", secrets.token_hex(32))

# Database path — must be known before DB exists
DB_PATH = os.environ.get("LABPORTAL_DB", os.path.join(BASE_DIR, "labportal.db"))

# Deploy script paths — host-level config, not portal-level
DEPLOY_SCRIPT = os.environ.get("LABPORTAL_DEPLOY_SCRIPT", "/root/labs/ocp-upi-deploy.sh")

# Install types — resource costs and deploy scripts per installation method
INSTALL_TYPES = {
    "upi": {
        "label": "UPI (User Provisioned)",
        "script": os.environ.get("LABPORTAL_UPI_SCRIPT", "/root/labs/ocp-upi-deploy.sh"),
        "vcpus": 16,    # 3×4 masters + 2×2 workers
        "ram_gb": 80,    # 5×16G
        "requires_slot": True,
    },
    "ipi": {
        "label": "IPI (Installer Provisioned)",
        "script": os.environ.get("LABPORTAL_IPI_SCRIPT", "/root/labs/ocp-ipi-deploy.sh"),
        "vcpus": 32,    # 3×8 masters + 2×4 workers
        "ram_gb": 128,   # 3×32G + 2×16G
        "requires_slot": False,
    },
}

# IPI dynamic IP offset range (blocks of 10)
IPI_OFFSET_START = 140
IPI_OFFSET_END = 190
IPI_OFFSET_STEP = 10


# ---------------------------------------------------------------------------
# Site configuration — DB-backed with in-memory cache
# ---------------------------------------------------------------------------
# All site settings live in the admin_config table (key/value).
# On first run the setup wizard populates them.  After that they're read
# from DB once and cached in memory for the lifetime of the process.

_site_cache = {}
_site_loaded = False


def _db_conn():
    """Lazy import to avoid circular dependency with db.py."""
    from db import get_db
    return get_db()


def load_site_config():
    """Load all admin_config rows into the in-memory cache."""
    global _site_cache, _site_loaded
    try:
        conn = _db_conn()
        rows = conn.execute("SELECT key, value FROM admin_config").fetchall()
        conn.close()
        _site_cache = {row["key"]: row["value"] for row in rows}
    except Exception:
        _site_cache = {}
    _site_loaded = True


def get_site(key, default=None):
    """Read a site config value (cached)."""
    if not _site_loaded:
        load_site_config()
    return _site_cache.get(key, default)


def set_site(key, value):
    """Write a site config value to DB and update cache."""
    conn = _db_conn()
    conn.execute(
        "INSERT OR REPLACE INTO admin_config (key, value) VALUES (?, ?)",
        (key, value)
    )
    conn.commit()
    conn.close()
    _site_cache[key] = value


def set_site_bulk(data: dict):
    """Write multiple site config values at once."""
    conn = _db_conn()
    for k, v in data.items():
        conn.execute(
            "INSERT OR REPLACE INTO admin_config (key, value) VALUES (?, ?)",
            (k, v)
        )
    conn.commit()
    conn.close()
    _site_cache.update(data)


def reload_site_config():
    """Force-reload config from DB (e.g. after admin edits settings)."""
    global _site_loaded
    _site_loaded = False
    load_site_config()


def is_setup_complete():
    """Check whether the first-run wizard has been completed."""
    return get_site("setup_complete") == "true"


# --- Convenience accessors for commonly used settings ---

def admin_user():
    return get_site("admin_user", "admin")

def base_domain():
    return get_site("base_domain", "example.com")

def allowed_email_domains():
    raw = get_site("allowed_email_domains", "")
    if not raw:
        return set()
    return {d.strip().lower() for d in raw.split(",") if d.strip()}

def cluster_slots():
    raw = get_site("cluster_slots", "{}")
    try:
        slots = json.loads(raw)
        return {k: int(v) for k, v in slots.items()}
    except (json.JSONDecodeError, ValueError):
        return {}

def admin_email():
    return get_site("admin_email", "")

def lab_hostname():
    return get_site("lab_hostname", "lab.local")

def storage_dir():
    return get_site("storage_dir", "/kvm")
