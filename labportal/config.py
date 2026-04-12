import os
import secrets

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

SECRET_KEY = os.environ.get("LABPORTAL_SECRET_KEY", secrets.token_hex(32))

# Admin credentials — override via environment
ADMIN_USER = os.environ.get("LABPORTAL_ADMIN_USER", "admin")
ADMIN_PASSWORD_HASH = None  # Set on first run via CLI

# Database
DB_PATH = os.environ.get("LABPORTAL_DB", os.path.join(BASE_DIR, "labportal.db"))

# Email
SMTP_HOST = os.environ.get("LABPORTAL_SMTP_HOST", "smtp.corp.redhat.com")
SMTP_PORT = int(os.environ.get("LABPORTAL_SMTP_PORT", "25"))
ADMIN_EMAIL = os.environ.get("LABPORTAL_ADMIN_EMAIL", "admin@redhat.com")
FROM_EMAIL = os.environ.get("LABPORTAL_FROM_EMAIL", "labportal@lab.example.com")

# Allowed email domains for access requests
ALLOWED_DOMAINS = {"redhat.com"}

# Blocked patterns (subdomains that aren't real @redhat.com)
BLOCKED_PATTERNS = []

# Lab system info
LAB_HOSTNAME = os.environ.get("LABPORTAL_HOSTNAME", "lab.example.com")
DEPLOY_SCRIPT = os.environ.get("LABPORTAL_DEPLOY_SCRIPT", "/root/ocp-upi-deploy.sh")

# Predefined cluster slots: name -> IP offset
# Each slot uses IPs .offset through .offset+5 on 192.168.122.0/24
CLUSTER_SLOTS = {
    "upi1": 110,
    "upi2": 120,
    "upi3": 130,
}
