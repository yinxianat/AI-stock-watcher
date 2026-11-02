"""Non-interactive authentication for the monitoring agent.

The agent can't sign in the way a person does — magic-link auth needs an
inbox and a browser. So it holds one long-lived secret (`ADMIN_API_TOKEN`)
and exchanges it for a short-lived token once per run:

    POST /admin/service-login  {"secret": "..."}  ->  {"token": "...", ...}
    GET  /admin/watch          Authorization: Bearer <short-lived token>

Why the exchange rather than sending the secret every time: a static bearer
travels on every request, so anything that captures a header once — a proxy
log, an error tracker, a misconfigured middleware — holds a credential valid
until someone notices and rotates it. Here the secret crosses the wire once
per run and what's used afterwards expires in minutes.

Issued tokens are stored as SHA-256 digests only, matching `sessions` and
`login_tokens`, so a database dump cannot be replayed.
"""

from __future__ import annotations

import logging
import secrets
from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session as DBSession

from app.core.security import hash_session_token
from app.core.settings import get_settings
from app.models import ServiceSession, utcnow

log = logging.getLogger(__name__)

# A secret shorter than this is refused outright, so nobody can quietly set
# ADMIN_API_TOKEN=test and still have the endpoint work.
MIN_SECRET_LENGTH = 32


class ServiceAuthError(Exception):
    """Base for refusals. Carries no detail that would help a guesser."""


class ServiceAuthUnavailable(ServiceAuthError):
    """No secret configured, or one too weak to accept."""


class ServiceAuthRejected(ServiceAuthError):
    """Wrong secret, or an expired/unknown token."""


def configured_secret() -> str:
    """The service secret, or raise if it's missing or too weak.

    Fails closed in both directions: an unset secret disables service
    access entirely rather than leaving it open, and a short one is treated
    as a misconfiguration rather than silently accepted.
    """
    secret = get_settings().admin_api_token.strip()
    if not secret:
        raise ServiceAuthUnavailable("ADMIN_API_TOKEN is not set")
    if len(secret) < MIN_SECRET_LENGTH:
        # Logged (without the value) so a weak secret is visible rather than
        # just producing confusing 503s.
        log.error(
            "ADMIN_API_TOKEN is too short to accept (%d chars, need %d)",
            len(secret), MIN_SECRET_LENGTH,
        )
        raise ServiceAuthUnavailable("ADMIN_API_TOKEN is too short")
    return secret


def login(db: DBSession, secret: str, client_net: str) -> tuple[str, int, bool]:
    """Exchange the secret for a short-lived token.

    Returns `(token, ttl_seconds, net_is_new)`. `net_is_new` is True when no
    previous login came from this network, which is the signal worth telling
    the admin about — routine logins from a known network stay silent, so
    the notification doesn't become noise that gets filtered away.
    """
    expected = configured_secret()
    if not secrets.compare_digest(secret, expected):
        raise ServiceAuthRejected("bad secret")

    _prune(db)
    seen_before = db.execute(
        select(ServiceSession.id).where(ServiceSession.client_net == client_net).limit(1)
    ).first() is not None

    ttl = max(1, get_settings().service_token_ttl_minutes)
    token = secrets.token_urlsafe(32)
    db.add(ServiceSession(
        token_hash=hash_session_token(token),
        client_net=client_net,
        expires_at=utcnow() + timedelta(minutes=ttl),
    ))
    db.commit()
    log.info("Service token issued to %s (ttl %dm)", client_net, ttl)
    return token, ttl * 60, not seen_before


def resolve(db: DBSession, token: str) -> ServiceSession:
    """The live session for this token, or raise.

    Expiry and revocation are checked in the query, so a stale row can
    never authenticate even if pruning hasn't run.
    """
    # Confirms a secret is configured at all: if service access has been
    # turned off, previously issued tokens must stop working too.
    configured_secret()
    row = db.execute(
        select(ServiceSession).where(
            ServiceSession.token_hash == hash_session_token(token),
            ServiceSession.revoked_at.is_(None),
            ServiceSession.expires_at > utcnow(),
        )
    ).scalar_one_or_none()
    if row is None:
        raise ServiceAuthRejected("unknown or expired token")
    return row


def revoke_all(db: DBSession) -> int:
    """Invalidate every outstanding service token. For rotating the secret."""
    rows = db.execute(
        select(ServiceSession).where(ServiceSession.revoked_at.is_(None))
    ).scalars().all()
    now = utcnow()
    for r in rows:
        r.revoked_at = now
    db.commit()
    return len(rows)


def _prune(db: DBSession) -> None:
    """Drop rows that expired a while ago.

    Done here rather than in a scheduled job: the table only grows when the
    agent logs in, so the login path is exactly when it needs tidying, and
    the window is kept long enough that `client_net` history survives for
    the new-network check above.
    """
    cutoff = utcnow() - timedelta(days=30)
    db.execute(delete(ServiceSession).where(ServiceSession.expires_at < cutoff))
    db.commit()
