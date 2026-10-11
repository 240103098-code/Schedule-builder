"""Shared SMTP delivery. Missing configuration never counts as a sent email."""
from email.message import EmailMessage
import hashlib
import os
import smtplib
import ssl


def configured():
    return bool(os.getenv("SMTP_HOST") and (os.getenv("SMTP_FROM") or os.getenv("SMTP_USERNAME")))


def send_email(recipient, subject, body, key=None):
    if not configured():
        raise RuntimeError("Configure SMTP_HOST and SMTP_FROM before sending email")
    message = EmailMessage()
    message["From"] = os.getenv("SMTP_FROM") or os.getenv("SMTP_USERNAME")
    message["To"], message["Subject"] = recipient, subject
    if key:
        message["Message-ID"] = "<" + hashlib.sha256(key.encode()).hexdigest() + "@schedule-builder>"
    message.set_content(body)
    use_ssl = os.getenv("SMTP_USE_SSL", "false").lower() == "true"
    use_tls = os.getenv("SMTP_USE_TLS", "true").lower() == "true"
    if not use_ssl and not use_tls and os.getenv("APP_ENV") == "production":
        raise RuntimeError("SMTP encryption is required in production")
    transport = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
    kwargs = {"timeout":10}
    if use_ssl:
        kwargs["context"] = ssl.create_default_context()
    with transport(os.getenv("SMTP_HOST"), int(os.getenv("SMTP_PORT", "587")), **kwargs) as client:
        if use_tls and not use_ssl:
            client.starttls(context=ssl.create_default_context())
        if os.getenv("SMTP_USERNAME"):
            client.login(os.getenv("SMTP_USERNAME"), os.getenv("SMTP_PASSWORD", ""))
        refused = client.send_message(message)
        if refused:
            raise RuntimeError("SMTP server rejected the recipient")
    return True
