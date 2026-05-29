"""Notification rule CRUD."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app.api.deps import current_user
from app.db.database import get_db
from app.models import NotificationRule, Ticker, User
from app.schemas.schemas import RuleIn, RuleOut

router = APIRouter(prefix="/rules", tags=["rules"])


@router.get("", response_model=list[RuleOut])
def list_rules(
    user: User = Depends(current_user), db: DBSession = Depends(get_db)
) -> list[RuleOut]:
    rows = db.execute(
        select(NotificationRule).where(NotificationRule.user_id == user.id)
    ).scalars().all()
    return [RuleOut.model_validate(r) for r in rows]


@router.post("", response_model=RuleOut, status_code=status.HTTP_201_CREATED)
def upsert_rule(
    payload: RuleIn,
    user: User = Depends(current_user),
    db: DBSession = Depends(get_db),
) -> RuleOut:
    if db.get(Ticker, payload.ticker_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ticker not found")

    existing = db.execute(
        select(NotificationRule).where(
            NotificationRule.user_id == user.id,
            NotificationRule.ticker_id == payload.ticker_id,
            NotificationRule.event_type == payload.event_type,
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.pct_low = payload.pct_low
        existing.pct_high = payload.pct_high
        existing.enabled = payload.enabled
        db.commit()
        db.refresh(existing)
        return RuleOut.model_validate(existing)

    row = NotificationRule(
        user_id=user.id,
        ticker_id=payload.ticker_id,
        event_type=payload.event_type,
        pct_low=payload.pct_low,
        pct_high=payload.pct_high,
        enabled=payload.enabled,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return RuleOut.model_validate(row)


@router.delete("/{rule_id}", status_code=204, response_class=Response)
def delete_rule(
    rule_id: int, user: User = Depends(current_user), db: DBSession = Depends(get_db)
):
    row = db.get(NotificationRule, rule_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Rule not found")
    db.delete(row)
    db.commit()
    return Response(status_code=204)
