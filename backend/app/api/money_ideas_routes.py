"""Money Ideas history API: returns every idea this user has received.

Two ways in. The frontend calls it with a session bearer token. Recap
emails link to it with a signed `?token=`, so a parent clicking from their
inbox doesn't hit the login page first (same approach as unsubscribe).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app.api.deps import current_user
from app.core.security import issue_token, verify_token
from app.core.settings import get_settings
from app.db.database import get_db
from app.models import MoneyIdeaSend, User
from app.services import money_ideas
from app.services.articles.preferences import get_age_groups

router = APIRouter(prefix="/money-ideas", tags=["money-ideas"])

MONEY_IDEAS_PURPOSE = "money_ideas"
MONEY_IDEAS_TTL_SECONDS = 90 * 24 * 3600  # emails sit in inboxes


def money_ideas_url(user_id: int) -> str:
    """History link for recap emails. The token identifies the user and is
    signed, not stored, so the page opens without signing in."""
    token = issue_token(MONEY_IDEAS_PURPOSE, {"uid": user_id})
    return f"{get_settings().frontend_base_url}/money-ideas?token={token}"


def user_from_token(db: DBSession, token: str) -> User | None:
    payload = verify_token(MONEY_IDEAS_PURPOSE, token, MONEY_IDEAS_TTL_SECONDS)
    uid = payload.get("uid") if payload else None
    return db.get(User, uid) if isinstance(uid, int) else None


@router.get("")
def list_money_ideas(
    token: str | None = Query(None),
    authorization: str | None = Header(default=None),
    db: DBSession = Depends(get_db),
):
    if token:
        user = user_from_token(db, token)
        if user is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired link")
    else:
        user = current_user(authorization, db)

    ages = get_age_groups(db, user.id)
    sends = db.execute(
        select(MoneyIdeaSend)
        .where(MoneyIdeaSend.user_id == user.id)
        .order_by(MoneyIdeaSend.trading_date.desc())
    ).scalars().all()
    items = []
    for s in sends:
        idea = money_ideas.get(s.idea_key)
        if idea is None:
            continue
        groups = [g for g in ages if g in idea.versions] or ["6-9"]
        items.append({
            "date": s.trading_date.isoformat(),
            "key": idea.key,
            "title": idea.title,
            "position": money_ideas.position(idea),
            "total": len(money_ideas.CURRICULUM),
            "versions": [
                {"age_group": g, "idea": idea.versions[g][0],
                 "question": idea.versions[g][1]}
                for g in groups
            ],
        })
    return {"ideas": items}
