"""Ticker catalog + auto-complete search."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_, select
from sqlalchemy.orm import Session as DBSession

from app.db.database import get_db
from app.models import Ticker
from app.schemas.schemas import TickerOut

router = APIRouter(prefix="/tickers", tags=["tickers"])


@router.get("", response_model=list[TickerOut])
def list_tickers(
    seeded_only: bool = Query(default=True, description="Show only the curated seed list."),
    limit: int = Query(default=200, le=500),
    db: DBSession = Depends(get_db),
) -> list[TickerOut]:
    stmt = select(Ticker)
    if seeded_only:
        stmt = stmt.where(Ticker.is_seeded.is_(True))
    rows = db.execute(stmt.order_by(Ticker.symbol).limit(limit)).scalars().all()
    return [TickerOut.model_validate(r) for r in rows]


@router.get("/search", response_model=list[TickerOut])
def search_tickers(
    q: str = Query(min_length=1, max_length=40, description="Symbol or name fragment."),
    limit: int = Query(default=10, le=50),
    db: DBSession = Depends(get_db),
) -> list[TickerOut]:
    """Auto-complete: case-insensitive prefix-on-symbol OR substring-on-name.

    Symbol matches rank above name matches (cheap heuristic: order by symbol
    length so 'AA' surfaces 'AAPL' before 'AAOI...').
    """
    qq = q.strip().lower()
    like = f"%{qq}%"
    sym_like = f"{qq}%"
    stmt = (
        select(Ticker)
        .where(
            or_(
                Ticker.symbol.ilike(sym_like),
                Ticker.symbol.ilike(like),
                Ticker.name.ilike(like),
            )
        )
        .limit(limit)
    )
    rows = db.execute(stmt).scalars().all()
    # Re-rank: exact symbol match first, then symbol prefix, then name match.
    def rank(t: Ticker) -> tuple[int, int, str]:
        s = t.symbol.lower()
        if s == qq:
            return (0, 0, t.symbol)
        if s.startswith(qq):
            return (1, len(s), t.symbol)
        return (2, len(t.name), t.symbol)

    rows.sort(key=rank)
    return [TickerOut.model_validate(r) for r in rows[:limit]]
