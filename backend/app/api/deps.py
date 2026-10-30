"""Shared FastAPI dependencies."""

from __future__ import annotations

from fastapi import Depends, Header, HTTPException, Query, status
from sqlalchemy.orm import Session as DBSession

from app.db.database import get_db
from app.models import User
from app.services.auth import resolve_session
from app.services.email_links import SETTINGS_PURPOSE, user_from_link_token


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
