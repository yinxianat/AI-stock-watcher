"""Watchlist endpoints — what tickers a user is tracking."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app.api.deps import current_user
from app.db.database import get_db
from app.models import Ticker, TickerType, User, WatchlistItem
from app.schemas.schemas import AddWatchRequest, WatchlistItemOut

router = APIRouter(prefix="/watchlist", tags=["watchlist"])


@router.get("", response_model=list[WatchlistItemOut])
def list_watchlist(
    user: User = Depends(current_user), db: DBSession = Depends(get_db)
) -> list[WatchlistItemOut]:
    stmt = (
        select(WatchlistItem)
        .where(WatchlistItem.user_id == user.id)
        .order_by(WatchlistItem.created_at)
    )
    rows = db.execute(stmt).scalars().all()
    return [WatchlistItemOut.model_validate(r) for r in rows]


@router.post("", response_model=WatchlistItemOut, status_code=status.HTTP_201_CREATED)
def add_watch(
    payload: AddWatchRequest,
    user: User = Depends(current_user),
    db: DBSession = Depends(get_db),
) -> WatchlistItemOut:
    if payload.ticker_id is not None:
        ticker = db.get(Ticker, payload.ticker_id)
        if ticker is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Ticker not found")
    else:
        symbol = (payload.symbol or "").strip().upper()
        if not symbol:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Symbol required")
        ticker = db.execute(
            select(Ticker).where(Ticker.symbol == symbol)
        ).scalar_one_or_none()
        if ticker is None:
            # User typed a symbol we don't have in catalog yet — create a stub.
            ticker = Ticker(
                symbol=symbol, name=symbol, type=TickerType.STOCK, is_seeded=False
            )
            db.add(ticker)
            db.flush()

    existing = db.execute(
        select(WatchlistItem).where(
            WatchlistItem.user_id == user.id, WatchlistItem.ticker_id == ticker.id
        )
    ).scalar_one_or_none()
    if existing is not None:
        return WatchlistItemOut.model_validate(existing)

    item = WatchlistItem(user_id=user.id, ticker_id=ticker.id)
    db.add(item)
    db.commit()
    db.refresh(item)
    return WatchlistItemOut.model_validate(item)


@router.delete("/{item_id}", status_code=204, response_class=Response)
def remove_watch(
    item_id: int, user: User = Depends(current_user), db: DBSession = Depends(get_db)
):
    item = db.get(WatchlistItem, item_id)
    if item is None or item.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Watch item not found")
    db.delete(item)
    db.commit()
    return Response(status_code=204)
