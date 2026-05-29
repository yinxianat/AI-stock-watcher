"""Notify-email change confirmation flow.

Whenever a user changes their notification email (the address alerts go TO,
which can differ from their login email), we mint a one-time confirmation
token, send it to the NEW address, and keep `notify_email_confirmed=False`
until the user clicks through.
"""

from __future__ import annotations

import secrets
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app.core.security import hash_session_token, issue_token, verify_token
from app.core.settings import get_settings
from app.models import ConfirmationToken, User, utcnow
from app.services.emailer import send_email

CONFIRM_PURPOSE = "confirm-notify-email"


def request_notify_email_change(db: DBSession, user: User, new_email: str) -> str:
    settings = get_settings()
    payload = {"user_id": user.id, "new_email": new_email.lower(),
               "nonce": secrets.token_urlsafe(16)}
    token = issue_token(CONFIRM_PURPOSE, payload)

    user.notify_email = new_email.lower()
    user.notify_email_confirmed = False

    db.add(
        ConfirmationToken(
            user_id=user.id,
            new_email=new_email.lower(),
            token_hash=hash_session_token(token),
            expires_at=utcnow() + timedelta(minutes=settings.email_confirm_ttl_minutes),
        )
    )
    db.commit()

    link = f"{settings.frontend_base_url}/auth/confirm-email?token={token}"
    body = (
        "Please confirm this is the address where you'd like AI Stock Watcher "
        f"to send your notifications. The link expires in "
        f"{settings.email_confirm_ttl_minutes} minutes.\n\n{link}"
    )
    try:
        send_email(new_email, "Confirm your notification email", body)
    except Exception:  # pragma: no cover
        pass

    return token


def confirm_notify_email(db: DBSession, token: str) -> User | None:
    settings = get_settings()
    payload = verify_token(CONFIRM_PURPOSE, token, settings.email_confirm_ttl_minutes * 60)
    if not payload:
        return None

    token_hash = hash_session_token(token)
    row = db.execute(
        select(ConfirmationToken).where(ConfirmationToken.token_hash == token_hash)
    ).scalar_one_or_none()
    if row is None or row.consumed_at is not None or row.expires_at < utcnow():
        return None

    user = db.get(User, row.user_id)
    if user is None:
        return None
    user.notify_email = row.new_email
    user.notify_email_confirmed = True
    row.consumed_at = utcnow()
    db.commit()

    # Welcome / next-steps email so user knows when alerts will fire.
    try:
        send_email(
            user.notify_email,
            "Notifications activated",
            (
                "Your notification address is confirmed. AI Stock Watcher checks "
                "the market three times each US trading day "
                f"({settings.batch_job_times_et} ET) and will email you whenever "
                "any of your rules trigger. You can edit your watchlist or rules "
                f"any time at {settings.frontend_base_url}."
            ),
        )
    except Exception:  # pragma: no cover
        pass
    return user
