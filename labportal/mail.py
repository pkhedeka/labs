import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import config


def send_password_reset_notification(user_email, user_name, new_password):
    """Notify user that admin has reset their password."""
    hostname = config.lab_hostname()
    subject = f"[Lab Portal] Your password has been reset"
    body = f"""Hi {user_name},

Your password for the OCP Lab Portal has been reset by an administrator.

New credentials:
  Email:    {user_email}
  Password: {new_password}

Login at: http://{hostname}/labs/user/login

Please change your password after logging in.

Thanks,
Lab Admin
"""
    _send(user_email, subject, body)


def send_reset_token_email(user_email, user_name, token):
    """Send a password reset link to the user."""
    hostname = config.lab_hostname()
    subject = f"[Lab Portal] Password reset request"
    body = f"""Hi {user_name},

A password reset was requested for your account on the OCP Lab Portal.

Click the link below to set a new password (valid for 1 hour):

http://{hostname}/labs/user/reset-password/{token}

If you did not request this, you can safely ignore this email.

Thanks,
Lab Admin
"""
    _send(user_email, subject, body)


def _send(to_addr, subject, body):
    """Send an email via SMTP. Fails silently with a log if SMTP is unavailable."""
    from_addr = config.from_email()
    if not to_addr or not from_addr:
        print(f"[mail] Skipping — from={from_addr!r}, to={to_addr!r}")
        return
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
