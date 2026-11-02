"""Shared FastAPI dependencies."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, Query, status
from sqlalchemy.orm import Session as DBSession

from app.db.database import get_db
from app.models import User
from app.services.auth import resolve_session
from app.services.email_links import SETTINGS_PURPOSE, user_from_link_token

log = logging.getLogger(__name__)


def current_user(
    authorization: str | None = Header(default=None),
    db: DBSession = Depends(get_db),
) -> User:
    """Extract bearer token from `Authorization: Bearer <token>` and resolve user."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    user = resolve_session(db, token)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired session")
    return user


def current_user_or_settings_token(
    token: str | None = Query(default=None),
    authorization: str | None = Header(default=None),
    db: DBSession = Depends(get_db),
) -> User:
    """Session bearer token, or a `?token=` from an email footer link.

    Only the preference endpoints use this. Changing the notification email
    deliberately stays session-only, so a forwarded email can't redirect
    someone else's alerts.
    """
    if token:
        user = user_from_link_token(db, SETTINGS_PURPOSE, token)
        if user is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired link")
        return user
    return current_user(authorization, db)


# ---------------------------------------------------------------------------
# Admin access
# ---------------------------------------------------------------------------


def is_admin(db: DBSession, user: User) -> bool:
    """True if this user may reach /admin.

    Two sources, deliberately. `ADMIN_EMAILS` is the bootstrap — it works
    before the person has ever signed in, and it means losing DB access
    can't lock everyone out. An `admin_roles` row grants a further operator
    with one INSERT and no deploy. Checked in that order so the env var is
    always authoritative for the owner.
    """
    from app.core.settings import get_settings
    from app.models import AdminRole

    if (user.email or "").lower() in get_settings().admin_email_list:
        return True
    return db.get(AdminRole, user.id) is not None


def current_admin(
    user: User = Depends(current_user),
    db: DBSession = Depends(get_db),
) -> User:
    """A signed-in user who is also an admin. Humans only."""
    if not is_admin(db, user):
        # Logged so repeated attempts are visible in log_entries.
        log.warning("Non-admin user %d attempted an admin endpoint", user.id)
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin access required")
    return user


@dataclass(frozen=True)
class Caller:
    """Who is asking for diagnostics: a human admin, or the agent.

    Deliberately not a `User`. A service caller has no user, and collapsing
    the two would let service access be mistaken for a particular person's
    access — in the audit trail, and in any future permission check. The
    type keeps them apart.
    """

    user: User | None = None
    service: bool = False

    @property
    def scope(self) -> str:
        """Which PII scope this caller's data should be built at.

        This is why `Caller` exists: the same endpoint serves real email
        addresses to the dashboard and pseudonyms to the agent, decided by
        who authenticated rather than hardcoded per route.
        """
        from app.services.diagnostics import SCOPE_ADMIN, SCOPE_SERVICE

        return SCOPE_SERVICE if self.service else SCOPE_ADMIN

    @property
    def label(self) -> str:
        return "service" if self.service else f"user:{self.user.id if self.user else '?'}"


def admin_or_service(
    authorization: str | None = Header(default=None),
    db: DBSession = Depends(get_db),
) -> Caller:
    """A human admin (session + role) or the monitoring agent (token).

    Tries the session first so a human's own identity is used when both
    could apply. The service token only reaches the endpoints named in
    `admin_routes.SERVICE_SAFE_PATHS`.
    """
    from app.api.admin_routes import verify_service_token

    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
        user = resolve_session(db, token)
        if user is not None:
            if not is_admin(db, user):
                log.warning("Non-admin user %d attempted an admin endpoint", user.id)
                raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin access required")
            return Caller(user=user)
        # Not a session token — the only other thing it can be is the
        # service credential.
        verify_service_token(token)
        return Caller(service=True)
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Bearer token required")
