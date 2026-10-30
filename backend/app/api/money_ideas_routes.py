"""Money Ideas history API: returns every idea this user has received."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import current_user
from app.db.database import get_session_factory
from app.models import MoneyIdeaSend, User
from app.services import money_ideas
from app.services.articles.preferences import get_age_groups

from sqlalchemy import select

router = APIRouter(prefix="/money-ideas", tags=["money-ideas"])


@router.get("")
def list_money_ideas(user: User = Depends(current_user)):
    db = get_session_factory()()
    try:
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
    finally:
        db.close()
