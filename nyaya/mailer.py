"""Optional outgoing e-mail (reminders) through the organisation's own SMTP server.

Supabase Auth sends sign-up and password e-mails itself; this is only for application e-mail. It is off until
SMTP_HOST and SMTP_FROM are set — the app then says so instead of pretending an e-mail was sent.
"""
from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage

from . import config


class MailError(Exception):
    pass


def configured() -> bool:
    return bool(config.SMTP_HOST and config.SMTP_FROM)


def send(to: str, subject: str, body: str) -> None:
    if not configured():
        raise MailError("E-mail is not configured on this server.")
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = config.SMTP_FROM, to, subject
    msg.set_content(body)
    ctx = ssl.create_default_context()
    try:
        if config.SMTP_SSL:
            with smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, timeout=20, context=ctx) as s:
                _login_and_send(s, msg)
        else:
            with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=20) as s:
                if config.SMTP_STARTTLS:
                    s.starttls(context=ctx)
                _login_and_send(s, msg)
    except (OSError, smtplib.SMTPException) as exc:
        raise MailError(f"The mail server refused or could not be reached ({type(exc).__name__}).") from exc


def _login_and_send(s: smtplib.SMTP, msg: EmailMessage) -> None:
    if config.SMTP_USER:
        s.login(config.SMTP_USER, config.SMTP_PASSWORD)
    s.send_message(msg)
