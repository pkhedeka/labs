import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from config import SMTP_HOST, SMTP_PORT, FROM_EMAIL, ADMIN_EMAIL, LAB_HOSTNAME


def send_admin_notification(request_name, request_email, reason):
    """Notify admin of a new access request."""
    subject = f"[Lab Portal] New access request from {request_name}"
    body = f"""New lab access request received:

Name:   {request_name}
Email:  {request_email}
Reason: {reason}

Review and approve/deny at:
http://{LAB_HOSTNAME}:8080/labs/admin
"""
    _send(ADMIN_EMAIL, subject, body)


def send_user_approved(user_email, user_name, admin_note="", password=None):
    """Notify user their request was approved."""
    subject = f"[Lab Portal] Your access to {LAB_HOSTNAME} has been approved"
    note_line = f"\nNote from admin: {admin_note}\n" if admin_note else ""
    creds = ""
    if password:
        creds = f"""
Your login credentials:
  Email:    {user_email}
  Password: {password}

Login at: http://{LAB_HOSTNAME}:8080/labs/user/login
Please change your password after first login.
"""
    body = f"""Hi {user_name},

Your request for lab access to {LAB_HOSTNAME} has been approved.
{note_line}{creds}
Lab portal: http://{LAB_HOSTNAME}:8080/labs

Thanks,
Lab Admin
"""
    _send(user_email, subject, body)


def send_user_denied(user_email, user_name, admin_note=""):
    """Notify user their request was denied."""
    subject = f"[Lab Portal] Your access request for {LAB_HOSTNAME}"
    note_line = f"\nReason: {admin_note}\n" if admin_note else ""
    body = f"""Hi {user_name},

Your request for lab access to {LAB_HOSTNAME} has been denied.
{note_line}
If you believe this is an error, please reach out to the lab admin
at {ADMIN_EMAIL}.

Thanks,
Lab Admin
"""
    _send(user_email, subject, body)


def _send(to_addr, subject, body):
    """Send an email via SMTP. Fails silently with a log if SMTP is unavailable."""
    msg = MIMEMultipart()
    msg["From"] = FROM_EMAIL
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as server:
            server.sendmail(FROM_EMAIL, [to_addr], msg.as_string())
    except Exception as e:
        print(f"[mail] Failed to send to {to_addr}: {e}")
