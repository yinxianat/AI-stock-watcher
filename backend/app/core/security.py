"""Token signing utilities.

We use itsdangerous's TimestampSigner for stateless, expiring tokens. The
issued tokens carry a purpose tag so a magic-link token can never be replayed
as an email-confirmation token (and vice-versa).
"""

from __future__ import annotations

import hmac
from hashlib import sha256

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .settings import get_settings


def _serializer(purpose: str) -> URLSafeTimedSerializer:
    """Build a serializer namespaced to the given purpose.

    The `salt` argument scopes the HMAC — tokens minted for purpose A cannot
    be verified under purpose B even with the same SECRET_KEY.
    """
    return URLSafeTimedSerializer(
        secret_key=get_settings().secret_key,
        salt=f"ai-stock-watcher::{purpose}",
    )


def issue_token(purpose: str, payload: dict) -> str:
    """Sign and return a URL-safe token carrying `payload`."""
    return _serializer(purpose).dumps(payload)


def verify_token(purpose: str, token: str, max_age_seconds: int) -> dict | None:
    """Return decoded payload or None if token is invalid/expired/wrong purpose."""
    try:
        return _serializer(purpose).loads(token, max_age=max_age_seconds)
    except SignatureExpired:
        return None
    except BadSignature:
        return None


def hash_session_token(token: str) -> str:
    """One-way hash for storing session tokens at rest.

    We never store the plaintext session token in the DB — only its SHA-256
    digest. This way a DB leak can't be used to impersonate users.
    """
    return sha256(token.encode("utf-8")).hexdigest()


def constant_time_eq(a: str, b: str) -> bool:
    """Timing-safe string comparison."""
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))
