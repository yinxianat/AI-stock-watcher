"""Alert-email subscription status.

Users can turn stock-alert emails off (Settings page, or the unsubscribe
link in every alert). That covers alerts and the daily watchlist recap —
sign-in links and email-confirmation messages are transactional and always
sent. The recap also has its own switch in Settings.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app.core.security import issue_token, verify_token
from app.core.settings import get_settings
from app.models import EmailOptOut, RecapOptOut, User

UNSUBSCRIBE_PURPOSE = "unsubscribe"


def is_unsubscribed(db: DBSession, user_id: int) -> bool:
    return db.get(EmailOptOut, user_id) is not None


def opted_out_user_ids(db: DBSession) -> set[int]:
    """All users who turned alerts off — load once per job run."""
    return set(db.execute(select(EmailOptOut.user_id)).scalars().all())


def set_alerts_enabled(db: DBSession, user: User, enabled: bool) -> None:
    """Turn alert emails on or off. Idempotent; commits."""
    row = db.get(EmailOptOut, user.id)
    if enabled and row is not None:
        db.delete(row)
    elif not enabled and row is None:
        db.add(EmailOptOut(user_id=user.id))
    db.commit()


def recap_enabled(db: DBSession, user_id: int) -> bool:
    """The daily watchlist recap's own switch (on unless turned off)."""
    return db.get(RecapOptOut, user_id) is None


def recap_opted_out_user_ids(db: DBSession) -> set[int]:
    return set(db.execute(select(RecapOptOut.user_id)).scalars().all())


def set_recap_enabled(db: DBSession, user: User, enabled: bool) -> None:
    """Turn the daily recap on or off. Idempotent; commits."""
    row = db.get(RecapOptOut, user.id)
    if enabled and row is not None:
        db.delete(row)
    elif not enabled and row is None:
        db.add(RecapOptOut(user_id=user.id))
    db.commit()


def unsubscribe_url(user: User) -> str:
    """Link for alert emails: opens a frontend page that confirms the opt-out.

    The token is signed (not stored) and identifies the user, so it works
    without signing in.
    """
    token = issue_token(UNSUBSCRIBE_PURPOSE, {"uid": user.id})
    return f"{get_settings().frontend_base_url}/unsubscribe?token={token}"


def user_id_from_unsubscribe_token(token: str) -> int | None:
    max_age = get_settings().unsubscribe_link_ttl_days * 24 * 3600
    payload = verify_token(UNSUBSCRIBE_PURPOSE, token, max_age)
    uid = payload.get("uid") if payload else None
    return uid if isinstance(uid, int) else None


def alert_email_footer(user: User) -> str:
    """Footer appended to every stock-alert email."""
    from app.services.email_links import settings_url

    return (
        f"Manage your watchlist and rules: {settings_url(user.id)}\n"
        f"Stop all alert emails: {unsubscribe_url(user)}"
    )


def alert_email_headers(user: User) -> dict[str, str]:
    """List-Unsubscribe lets Gmail/Apple Mail show their built-in
    "Unsubscribe" button next to the sender."""
    return {"List-Unsubscribe": f"<{unsubscribe_url(user)}>"}
