"""Which age groups' stories each parent gets."""

from __future__ import annotations

from sqlalchemy.orm import Session as DBSession

from app.models import StoryPreference, User
from app.services.articles.schemas import AGE_GROUPS, DEFAULT_AGE_GROUPS


def _clean(groups) -> list[str]:
    """Known groups only, youngest first, no repeats."""
    wanted = set(groups)
    return [g for g in AGE_GROUPS if g in wanted]


def get_age_groups(db: DBSession, user_id: int) -> list[str]:
    row = db.get(StoryPreference, user_id)
    groups = _clean(row.age_groups.split(",")) if row else []
    return groups or list(DEFAULT_AGE_GROUPS)


def set_age_groups(db: DBSession, user: User, groups: list[str]) -> list[str]:
    """Save the parent's choice. Commits. Returns what was saved."""
    clean = _clean(groups) or list(DEFAULT_AGE_GROUPS)
    row = db.get(StoryPreference, user.id)
    if row is None:
        db.add(StoryPreference(user_id=user.id, age_groups=",".join(clean)))
    else:
        row.age_groups = ",".join(clean)
    db.commit()
    return clean
