"""Notify-email change confirmation flow.

Whenever a user changes their notification email (the address alerts go TO,
which can differ from their login email), we mint a one-time confirmation
token, send it to the NEW address, and keep `notify_email_confirmed=False`
until the user clicks through.
"""

from __future__ import annotations

import secrets
from datetime import timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session as DBSession

from app.core.security import hash_session_token, issue_token, verify_token
from app.core.settings import get_settings
from app.models import ConfirmationToken, User, utcnow
from app.services.emailer import send_email
from app.services.send_limits import TooManyEmails, enforce_send_limit, window_start

CONFIRM_PURPOSE = "confirm-notify-email"

# Re-exported so the route can catch it without importing send_limits.
TooManyConfirmationEmails = TooManyEmails


class SameNotifyEmail(Exception):
    """The new address is already the user's confirmed notification email."""


class EmailInUse(Exception):
    """Another account already uses this address."""


def _used_by_another_account(db: DBSession, user_id: int, email: str) -> bool:
    """True if a different user signs in with `email` or gets alerts at it.

    Only *confirmed* notification addresses count, so nobody can block an
    address just by typing it in and never confirming.
    """
    hit = db.execute(
        select(User.id).where(
            User.id != user_id,
            or_(
                User.email == email,
                (User.notify_email == email) & User.notify_email_confirmed.is_(True),
            ),
        )
    ).first()
    return hit is not None


def _check_send_limit(db: DBSession, user_id: int) -> None:
    sent = db.execute(
        select(ConfirmationToken.created_at)
        .where(
            ConfirmationToken.user_id == user_id,
            ConfirmationToken.created_at >= window_start(),
        )
        .order_by(ConfirmationToken.created_at.desc())
    ).scalars().all()
    enforce_send_limit(sent)


def request_notify_email_change(db: DBSession, user: User, new_email: str) -> str:
    """Start a notification-address change by emailing a confirmation link.

    Raises SameNotifyEmail, EmailInUse, or TooManyConfirmationEmails. Requesting
    the address that is already pending confirmation re-sends the link.
    """
    settings = get_settings()
    new_email = new_email.strip().lower()
    if new_email == user.notify_email.lower() and user.notify_email_confirmed:
        raise SameNotifyEmail()
    if _used_by_another_account(db, user.id, new_email):
        raise EmailInUse()
    _check_send_limit(db, user.id)
    payload = {"user_id": user.id, "new_email": new_email,
               "nonce": secrets.token_urlsafe(16)}
    token = issue_token(CONFIRM_PURPOSE, payload)

    user.notify_email = new_email
    user.notify_email_confirmed = False

    db.add(
        ConfirmationToken(
            user_id=user.id,
            new_email=new_email,
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
    """Returns None for a bad/expired token. Raises EmailInUse if another
    account claimed the address after the link was sent."""
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
    if _used_by_another_account(db, user.id, row.new_email):
        raise EmailInUse()
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
