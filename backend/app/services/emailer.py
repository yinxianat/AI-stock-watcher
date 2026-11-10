"""SMTP email sender.

Kept intentionally small. In tests we monkeypatch `send_email` to capture
the outgoing messages instead of opening a real SMTP connection.

`send_email` RAISES `EmailSendError` when the mail server refuses or drops
the message. It used to log and swallow, which silently defeated every
caller that wrapped the send in `try`/`except` to record the failure: the
recap wrote a `RecapSend` row, the notify job kept its `send_failures`
counter at zero, and a story follow-up was marked "sent" and never retried,
all for emails nobody received. Callers that genuinely cannot act on a
failure (the sign-in link, the confirm link, admin alerts) use
`send_email_quiet`, so that choice is visible at the call site instead of
being imposed here.
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from app.core.security import mask_email, redact_emails
from app.core.settings import get_settings

log = logging.getLogger(__name__)


class EmailSendError(RuntimeError):
    """The mail server refused or dropped the message.

    Carries the SMTP error text, which is what the delivery-outcome detail
    and the admin alert are read for. It deliberately does NOT say whether
    the message was delivered: a disconnect during DATA means the server had
    the whole message and only its acknowledgement was lost, so a retry can
    duplicate the email. That trade-off belongs to each caller, not here.
    """


def send_email(
    to: str,
    subject: str,
    body_text: str,
    body_html: str | None = None,
    headers: dict[str, str] | None = None,
) -> None:
    """Send a plain-text (and optionally HTML) email via configured SMTP.

    Logs and then re-raises SMTP/network errors as `EmailSendError`, so a
    caller that records or retries a failed send can see it. Returns
    normally — without sending — when no SMTP server is configured, which is
    the development default and not a failure.

    Raises:
        EmailSendError: the mail server refused or dropped the message.
    """
    settings = get_settings()

    msg = EmailMessage()
    msg["From"] = settings.smtp_from
    msg["To"] = to
    msg["Subject"] = subject
    for name, value in (headers or {}).items():
        msg[name] = value
    msg.set_content(body_text)
    if body_html:
        msg.add_alternative(body_html, subtype="html")

    # Masked: this line runs on every send, so the raw address would make
    # info_logs the largest store of user emails in the system.
    log.info("Sending email to=%s subject=%r via %s:%s",
             mask_email(to), redact_emails(subject), settings.smtp_host,
             settings.smtp_port)

    if not settings.smtp_host or settings.smtp_host == "localhost" and not settings.smtp_username:
        # Dev fallback: no real server configured — just log and return.
        # The body carries signed magic-link / settings tokens, so it is
        # only ever written out in development.
        if settings.app_env == "development":
            log.warning("SMTP not configured; email NOT sent. Body:\n%s", body_text)
        else:
            log.warning("SMTP not configured; email to %s NOT sent", mask_email(to))
        return

    try:
        with smtplib.SMTP(
            settings.smtp_host, settings.smtp_port, timeout=settings.smtp_timeout
        ) as smtp:
            if settings.smtp_use_tls:
                smtp.starttls()
            if settings.smtp_username:
                smtp.login(settings.smtp_username, settings.smtp_password)
            smtp.send_message(msg)
    except (smtplib.SMTPException, OSError) as e:
        # Logged here, where the host and the masked recipient are known, so
        # callers add context rather than repeating it. ERROR also emails the
        # admin via EmailAlertHandler.
        log.exception(
            "Failed to send email to=%s subject=%r via %s:%s",
            mask_email(to),
            redact_emails(subject),
            settings.smtp_host,
            settings.smtp_port,
        )
        raise EmailSendError(f"{type(e).__name__}: {e}") from e


def send_email_quiet(
    to: str,
    subject: str,
    body_text: str,
    body_html: str | None = None,
    headers: dict[str, str] | None = None,
) -> bool:
    """`send_email` for callers that cannot act on a failure.

    Returns True if the message was handed to the mail server. Use it where
    there is nothing to record and nobody to tell — a sign-in link the user
    can simply request again, or a FastAPI background task, where a raise
    would surface after the response had already gone out. Anything that
    writes a row claiming the email was sent must call `send_email` and
    handle the error.
    """
    try:
        send_email(to, subject, body_text, body_html=body_html, headers=headers)
        return True
    except EmailSendError:
        return False  # already logged, with the recipient masked
