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

    host_configured = bool(settings.smtp_host and settings.smtp_host != "localhost")
    creds_configured = bool(settings.smtp_username)

    log.debug(
        "SMTP config check: SMTP_HOST=%r (configured=%s) SMTP_USERNAME=%s",
        settings.smtp_host,
        host_configured,
        "set" if creds_configured else "not set",
    )

    if not host_configured or not creds_configured:
        # Dev/unconfigured fallback — log clearly and skip the real send so
        # callers can see exactly why the email was dropped.
        reasons: list[str] = []
        if not host_configured:
            reasons.append(
                f"SMTP_HOST is not set or is still the default 'localhost' (got {settings.smtp_host!r})"
            )
        if not creds_configured:
            reasons.append("SMTP_USERNAME is not set")
        log.warning(
            "Email NOT sent to=%s subject=%r — SMTP is not fully configured: %s. Body:\n%s",
            to,
            subject,
            "; ".join(reasons),
            body_text,
        )
        return

    log.info("Sending email to=%s subject=%r via %s:%d", to, subject, settings.smtp_host, settings.smtp_port)

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as smtp:
        if settings.smtp_use_tls:
            smtp.starttls()
        if settings.smtp_username:
            smtp.login(settings.smtp_username, settings.smtp_password)
        smtp.send_message(msg)

    log.info("Email sent successfully to=%s subject=%r", to, subject)
