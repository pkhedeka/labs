import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import config


def send_admin_notification(request_name, request_email, reason):
    """Notify admin of a new access request."""
    hostname = config.lab_hostname()
    subject = f"[Lab Portal] New access request from {request_name}"
    body = f"""New lab access request received:

Name:   {request_name}
Email:  {request_email}
Reason: {reason}

Review and approve/deny at:
http://{hostname}/labs/admin
"""
    _send(config.admin_email(), subject, body)


def send_user_approved(user_email, user_name, admin_note="", password=None,
                       linux_username=None, linux_password=None):
    """Notify user their request was approved."""
    hostname = config.lab_hostname()
    subject = f"[Lab Portal] Your access to {hostname} has been approved"
    note_line = f"\nNote from admin: {admin_note}\n" if admin_note else ""
    creds = ""
    if password:
        creds = f"""
Portal login credentials:
  Email:    {user_email}
  Password: {password}

Login at: http://{hostname}/labs/user/login
"""
    ssh_creds = ""
    if linux_username:
        ssh_creds = f"""
SSH access to the lab system:
  ssh {linux_username}@{hostname}
"""
        if linux_password:
            ssh_creds += f"""  Temporary password: {linux_password}
  You will be asked to change your password on first login.
  Password expires every 180 days.
  Account locks after 30 days of inactivity.
"""
    body = f"""Hi {user_name},

Your request for lab access to {hostname} has been approved.
{note_line}{creds}{ssh_creds}
Lab portal: http://{hostname}/labs

Thanks,
Lab Admin
"""
    _send(user_email, subject, body)


def send_user_denied(user_email, user_name, admin_note=""):
    """Notify user their request was denied."""
    hostname = config.lab_hostname()
    subject = f"[Lab Portal] Your access request for {hostname}"
    note_line = f"\nReason: {admin_note}\n" if admin_note else ""
    body = f"""Hi {user_name},

Your request for lab access to {hostname} has been denied.
{note_line}
If you believe this is an error, please reach out to the lab admin
at {config.admin_email()}.

Thanks,
Lab Admin
"""
    _send(user_email, subject, body)


def _send(to_addr, subject, body):
    """Send an email via SMTP. Fails silently with a log if SMTP is unavailable."""
    if not to_addr:
        print("[mail] No recipient address configured, skipping.")
        return

    from_addr = config.from_email()
    msg = MIMEMultipart()
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP(config.smtp_host(), config.smtp_port(), timeout=10) as server:
            server.sendmail(from_addr, [to_addr], msg.as_string())
    except Exception as e:
        print(f"[mail] Failed to send to {to_addr}: {e}")
