"""Authentication routes — magic-link request, verify, logout."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session as DBSession

from app.api.deps import current_user
from app.db.database import get_db
from app.models import User
from app.schemas.schemas import (
    LoginRequest,
    MagicLinkVerify,
    SessionResponse,
    UpdateNotifyEmailRequest,
    UserOut,
)
from app.services.auth import (
    issue_session,
    request_magic_link,
    revoke_session,
    verify_magic_link,
)
from app.services.confirm_email import confirm_notify_email, request_notify_email_change

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/request-link", status_code=204, response_class=Response)
def request_link(payload: LoginRequest, db: DBSession = Depends(get_db)):
    """Email the user a magic link.

    Always returns 204 — even if the email is unknown — so we don't leak
    which addresses are registered. Account creation is implicit on verify.
    """
    request_magic_link(db, payload.email)
    return Response(status_code=204)


@router.post("/verify", response_model=SessionResponse)
def verify(payload: MagicLinkVerify, db: DBSession = Depends(get_db)) -> SessionResponse:
    user = verify_magic_link(db, payload.token)
    if user is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired token")
    plaintext, row = issue_session(db, user)
    return SessionResponse(
        session_token=plaintext, expires_at=row.expires_at, user=UserOut.model_validate(user)
    )


@router.post("/logout", status_code=204, response_class=Response)
def logout(
    payload: MagicLinkVerify,  # reuses {token} schema
    db: DBSession = Depends(get_db),
):
    revoke_session(db, payload.token)
    return Response(status_code=204)


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(current_user)) -> UserOut:
    return UserOut.model_validate(user)


@router.post("/notify-email", status_code=204, response_class=Response)
def change_notify_email(
    payload: UpdateNotifyEmailRequest,
    user: User = Depends(current_user),
    db: DBSession = Depends(get_db),
):
    request_notify_email_change(db, user, payload.new_email)
    return Response(status_code=204)


@router.post("/notify-email/confirm", response_model=UserOut)
def confirm_change(
    payload: MagicLinkVerify, db: DBSession = Depends(get_db)
) -> UserOut:
    user = confirm_notify_email(db, payload.token)
    if user is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired token")
    return UserOut.model_validate(user)
