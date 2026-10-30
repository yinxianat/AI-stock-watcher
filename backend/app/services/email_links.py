"""Signed deep links for emails.

Recap and alert emails point at pages that normally need a session. Rather
than dropping a parent on a login form, each link carries a token that
identifies the user.

Two properties matter. The token is scoped to a purpose, so a Settings link
can't be replayed as a feedback vote (see `app.core.security._serializer`,
which salts the signature with the purpose). And it never mints a session:
it authorises exactly the endpoints that opt into it, so a forwarded email
can't be used to sign in or change the notification email.
"""

from __future__ import annotations

from sqlalchemy.orm import Session as DBSession

from app.core.security import issue_token, verify_token
from app.core.settings import get_settings
from app.models import User

# Emails sit in inboxes, so these outlive a session by a wide margin.
EMAIL_LINK_TTL_SECONDS = 90 * 24 * 3600

SETTINGS_PURPOSE = "settings"
MONEY_IDEAS_PURPOSE = "money_ideas"


def link_url(purpose: str, user_id: int, path: str) -> str:
    token = issue_token(purpose, {"uid": user_id})
    return f"{get_settings().frontend_base_url}{path}?token={token}"


def user_from_link_token(db: DBSession, purpose: str, token: str) -> User | None:
    """The user a link token names, or None if it's invalid, expired, or
    signed for a different purpose."""
    payload = verify_token(purpose, token, EMAIL_LINK_TTL_SECONDS)
    uid = payload.get("uid") if payload else None
    return db.get(User, uid) if isinstance(uid, int) else None


def settings_url(user_id: int) -> str:
    """"Manage your watchlist, rules and story ages" link in email footers."""
    return link_url(SETTINGS_PURPOSE, user_id, "/settings")


def money_ideas_url(user_id: int) -> str:
    """"Every money idea you've had so far" link in recap emails."""
    return link_url(MONEY_IDEAS_PURPOSE, user_id, "/money-ideas")
