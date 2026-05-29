"""Magic-link authentication flow.

1. User submits email.
2. We mint a single-use, time-limited token (signed) and store ONLY its
   hash. We email the plaintext link to the user.
3. User clicks link -> frontend POSTs the token to /auth/verify.
4. We look up the hash, check expiry+consumed, mark consumed, and issue
   a long-lived session token (also stored as hash).
"""

from __future__ import annotations

import secrets
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app.core.security import hash_session_token, issue_token, verify_token
from app.core.settings import get_settings
from app.models import LoginToken, Session, User, utcnow
from app.services.emailer import send_email

LOGIN_PURPOSE = "login"
CONFIRM_PURPOSE = "confirm-email"


def _now():
    return utcnow()


def request_magic_link(db: DBSession, email: str) -> str:
    """Create+persist a magic-link token, email it, return the plaintext token.

    Returning the token is convenient for tests; the API endpoint discards it.
    """
    settings = get_settings()
    ttl_min = settings.magic_link_ttl_minutes
    payload = {"email": email.lower(), "nonce": secrets.token_urlsafe(16)}
    token = issue_token(LOGIN_PURPOSE, payload)

    db.add(
        LoginToken(
            email=email.lower(),
            token_hash=hash_session_token(token),
            expires_at=_now() + timedelta(minutes=ttl_min),
        )
    )
    db.commit()

    link = f"{settings.frontend_base_url}/auth/callback?token={token}"
    body = (
        f"Hi,\n\nClick the link below to sign in to AI Stock Watcher. "
        f"It expires in {ttl_min} minutes.\n\n{link}\n\n"
        "If you didn't request this, you can ignore this email."
    )
    try:
        send_email(email, "Your AI Stock Watcher sign-in link", body)
    except Exception:  # pragma: no cover — don't leak SMTP errors
        # We swallow because the token is already minted; user can retry.
        pass

    return token


def verify_magic_link(db: DBSession, token: str) -> User | None:
    """Validate token, mark consumed, upsert the user. Return the User or None."""
    settings = get_settings()
    payload = verify_token(LOGIN_PURPOSE, token, settings.magic_link_ttl_minutes * 60)
    if not payload:
        return None

    token_hash = hash_session_token(token)
    row = db.execute(select(LoginToken).where(LoginToken.token_hash == token_hash)).scalar_one_or_none()
    if row is None or row.consumed_at is not None or row.expires_at < _now():
        return None

    row.consumed_at = _now()

    email = payload["email"]
    user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if user is None:
        user = User(email=email, notify_email=email, notify_email_confirmed=True)
        db.add(user)
        db.flush()

    user.last_login_at = _now()
    db.commit()
    return user


def issue_session(db: DBSession, user: User) -> tuple[str, Session]:
    """Mint a session token, store its hash, return (plaintext_token, row)."""
    settings = get_settings()
    plaintext = secrets.token_urlsafe(48)
    row = Session(
        user_id=user.id,
        token_hash=hash_session_token(plaintext),
        expires_at=_now() + timedelta(days=settings.session_ttl_days),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return plaintext, row


def resolve_session(db: DBSession, plaintext_token: str) -> User | None:
    """Return the User attached to a valid session token, or None."""
    token_hash = hash_session_token(plaintext_token)
    row = db.execute(
        select(Session).where(Session.token_hash == token_hash)
    ).scalar_one_or_none()
    if row is None or row.revoked_at is not None or row.expires_at < _now():
        return None
    return db.get(User, row.user_id)


def revoke_session(db: DBSession, plaintext_token: str) -> None:
    token_hash = hash_session_token(plaintext_token)
    row = db.execute(
        select(Session).where(Session.token_hash == token_hash)
    ).scalar_one_or_none()
    if row and row.revoked_at is None:
        row.revoked_at = _now()
        db.commit()
