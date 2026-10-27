"""Authentication routes — magic-link request, verify, logout."""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session as DBSession

from app.api.deps import current_user
from app.db.database import get_db
from app.models import User
from app.schemas.schemas import (
    EmailPreferences,
    LoginRequest,
    MagicLinkVerify,
    SessionResponse,
    UpdateNotifyEmailRequest,
    UserOut,
)
from app.services.auth import (
    TooManySignInEmails,
    issue_session,
    request_magic_link,
    revoke_session,
    verify_magic_link,
)
from app.services.confirm_email import (
    EmailInUse,
    SameNotifyEmail,
    TooManyConfirmationEmails,
    confirm_notify_email,
    request_notify_email_change,
)
from app.services.email_prefs import (
    is_unsubscribed,
    set_alerts_enabled,
    user_id_from_unsubscribe_token,
)
from app.services.signin_backfill import kickoff_signin_backfill

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/request-link", status_code=204, response_class=Response)
def request_link(payload: LoginRequest, db: DBSession = Depends(get_db)):
    """Email the user a magic link.

    Always returns 204 — even if the email is unknown — so we don't leak
    which addresses are registered. Account creation is implicit on verify.
    """
    try:
        request_magic_link(db, payload.email)
    except TooManySignInEmails as e:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "A sign-in link was sent recently. Please check your inbox or try again shortly.",
            headers={"Retry-After": str(e.retry_after_seconds)},
        )
    return Response(status_code=204)


@router.post("/verify", response_model=SessionResponse)
def verify(
    payload: MagicLinkVerify,
    background_tasks: BackgroundTasks,
    db: DBSession = Depends(get_db),
) -> SessionResponse:
    user = verify_magic_link(db, payload.token)
    if user is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired token")
    plaintext, row = issue_session(db, user)

    # Sign-in backfill: ensure we have recent daily_closes for every ticker
    # this user has notification rules on, so the day-over-day PRICE_CHANGE
    # baseline works from their first session. No-op when nothing is missing
    # or when the test app_env is set.
    background_tasks.add_task(kickoff_signin_backfill, user.id)

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
    try:
        request_notify_email_change(db, user, payload.new_email)
    except SameNotifyEmail:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "That's already your notification email.",
        )
    except EmailInUse:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "That email is already used by another account. Please choose a different one.",
        )
    except TooManyConfirmationEmails as e:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "A confirmation email was sent recently. Please check your inbox or try again shortly.",
            headers={"Retry-After": str(e.retry_after_seconds)},
        )
    return Response(status_code=204)


@router.post("/notify-email/confirm", response_model=UserOut)
def confirm_change(
    payload: MagicLinkVerify, db: DBSession = Depends(get_db)
) -> UserOut:
    try:
        user = confirm_notify_email(db, payload.token)
    except EmailInUse:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "That email is now used by another account, so it can't be confirmed.",
        )
    if user is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired token")
    return UserOut.model_validate(user)


@router.get("/email-preferences", response_model=EmailPreferences)
def get_email_preferences(
    user: User = Depends(current_user), db: DBSession = Depends(get_db)
) -> EmailPreferences:
    return EmailPreferences(alerts_enabled=not is_unsubscribed(db, user.id))


@router.put("/email-preferences", response_model=EmailPreferences)
def update_email_preferences(
    payload: EmailPreferences,
    user: User = Depends(current_user),
    db: DBSession = Depends(get_db),
) -> EmailPreferences:
    set_alerts_enabled(db, user, payload.alerts_enabled)
    return EmailPreferences(alerts_enabled=payload.alerts_enabled)


@router.post("/unsubscribe", status_code=204, response_class=Response)
def unsubscribe(payload: MagicLinkVerify, db: DBSession = Depends(get_db)):
    """Unsubscribe link from an alert email. No sign-in needed: the signed
    token identifies the user. Safe to call more than once."""
    user_id = user_id_from_unsubscribe_token(payload.token)
    user = db.get(User, user_id) if user_id is not None else None
    if user is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired unsubscribe link")
    set_alerts_enabled(db, user, False)
    return Response(status_code=204)
