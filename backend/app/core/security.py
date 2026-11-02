"""Token signing utilities.

We use itsdangerous's TimestampSigner for stateless, expiring tokens. The
issued tokens carry a purpose tag so a magic-link token can never be replayed
as an email-confirmation token (and vice-versa).
"""

from __future__ import annotations

import hmac
import re
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


def mask_email(address: str | None) -> str:
    """An address safe to write to a log: `alice@gmail.com` → `a***@gmail.com`.

    Logs are read by the admin, stored in `info_logs` / `log_entries`, mailed
    out in alerts and (for a few endpoints) served to the monitoring agent.
    None of those need the real address, so it never goes in. Enough of the
    address survives to tell two users apart while debugging.
    """
    if not address:
        return "(none)"
    address = address.strip()
    local, sep, domain = address.partition("@")
    if not sep or not local:
        return "***"  # not an address shape; reveal nothing
    return f"{local[0]}***@{domain}" if domain else "***"


_EMAIL_IN_TEXT = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def redact_emails(text: str | None) -> str:
    """Mask every address inside free text before it is logged.

    Masking only the recipient isn't enough: an alert subject like
    "New user signed up: alice@gmail.com" would still write the address to
    `info_logs`. Admin *emails* keep the real address — the admin needs it —
    but the log copy never does. Pattern-based, so a subject added later is
    covered without anyone remembering to mask it.
    """
    if not text:
        return ""
    return _EMAIL_IN_TEXT.sub(lambda m: mask_email(m.group(0)), text)
