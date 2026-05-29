"""Shared FastAPI dependencies."""

from __future__ import annotations

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session as DBSession

from app.db.database import get_db
from app.models import User
from app.services.auth import resolve_session


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
