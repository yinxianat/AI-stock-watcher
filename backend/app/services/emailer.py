"""SMTP email sender.

Kept intentionally small. In tests we monkeypatch `send_email` to capture
the outgoing messages instead of opening a real SMTP connection.
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from app.core.settings import get_settings

log = logging.getLogger(__name__)


def send_email(to: str, subject: str, body_text: str, body_html: str | None = None) -> None:
    """Send a plain-text (and optionally HTML) email via configured SMTP.

    Raises smtplib errors on failure — callers decide whether to surface or
    swallow them. We never raise back to the user-facing HTTP layer because
    that would leak SMTP details.
    """
    settings = get_settings()

    msg = EmailMessage()
    msg["From"] = settings.smtp_from
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body_text)
    if body_html:
        msg.add_alternative(body_html, subtype="html")

    log.info("Sending email to=%s subject=%r", to, subject)

    if not settings.smtp_host or settings.smtp_host == "localhost" and not settings.smtp_username:
        # Dev fallback: no real server configured — just log and return.
        log.warning("SMTP not configured; email NOT actually sent. Body:\n%s", body_text)
        return

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as smtp:
        if settings.smtp_use_tls:
            smtp.starttls()
        if settings.smtp_username:
            smtp.login(settings.smtp_username, settings.smtp_password)
        smtp.send_message(msg)
