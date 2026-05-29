"""Read-only access to the latest batch outputs."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app.api.deps import current_user
from app.db.database import get_db
from app.models import PriceSnapshot, TrendAnalysis, User
from app.schemas.schemas import PriceSnapshotOut, TrendOut

router = APIRouter(prefix="/trends", tags=["trends"])


@router.get("", response_model=list[TrendOut])
def my_trends(
    user: User = Depends(current_user), db: DBSession = Depends(get_db)
) -> list[TrendOut]:
    rows = db.execute(
        select(TrendAnalysis).where(TrendAnalysis.user_id == user.id)
    ).scalars().all()
    return [TrendOut.model_validate(r) for r in rows]


@router.get("/snapshots", response_model=list[PriceSnapshotOut])
def list_snapshots(db: DBSession = Depends(get_db)) -> list[PriceSnapshotOut]:
    rows = db.execute(select(PriceSnapshot)).scalars().all()
    return [PriceSnapshotOut.model_validate(r) for r in rows]
