"""Purchase-price comparison endpoints — what the user paid, and the gain.

Session auth only, deliberately: the preference endpoints accept a signed
token from an email footer so a parent clicking from their inbox is not sent
to a login screen, but *entering a purchase price* is not something to do
from a link in an email. Reading one is not either.

The gain/loss is computed here rather than in the browser so that the page
and the recap email quote the same number from the same function.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app.api.deps import current_user
from app.db.database import get_db
from app.models import TrendAnalysis, User, WatchlistItem
from app.schemas.schemas import GainLossOut, HoldingIn, HoldingOut
from app.services import holdings

router = APIRouter(prefix="/holdings", tags=["holdings"])


def _require_enabled(db: DBSession, user: User) -> None:
    """A write while the feature is off is a client bug, not a user choice.

    Reads stay open so the page can show prices the moment the switch flips,
    without a second round trip.
    """
    if not holdings.tracking_enabled(db, user.id):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Purchase-price comparison is off. Turn it on in Settings first.",
        )


def _require_watched(db: DBSession, user: User, ticker_id: int) -> None:
    """You can only price something you are watching.

    Without this a caller could enumerate ticker ids and store prices against
    stocks that never appear anywhere — rows nothing would ever clean up,
    since the cleanup happens when a watchlist row is removed.
    """
    watched = db.execute(
        select(WatchlistItem.id).where(
            WatchlistItem.user_id == user.id, WatchlistItem.ticker_id == ticker_id
        )
    ).first()
    if watched is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That stock is not on your watchlist")


def _prices(db: DBSession, user_id: int) -> dict[int, float]:
    """Latest price per ticker for this user, from the last compute run."""
    rows = db.execute(
        select(TrendAnalysis.ticker_id, TrendAnalysis.price)
        .where(TrendAnalysis.user_id == user_id)
    ).all()
    return {ticker_id: price for ticker_id, price in rows}


def _out(holding, current_price: float | None) -> HoldingOut:
    gl = holdings.compute(holding.purchase_price, holding.shares, current_price)
    return HoldingOut(
        ticker_id=holding.ticker_id,
        purchase_price=holding.purchase_price,
        shares=holding.shares,
        updated_at=holding.updated_at,
        gain_loss=None if gl is None else GainLossOut(
            current_price=gl.current_price,
            per_share=gl.per_share,
            pct=gl.pct,
            cost_basis=gl.cost_basis,
            market_value=gl.market_value,
            total=gl.total,
            is_gain=gl.is_gain,
            summary=holdings.summary(gl),
        ),
    )


@router.get("", response_model=list[HoldingOut])
def list_holdings(
    user: User = Depends(current_user), db: DBSession = Depends(get_db)
) -> list[HoldingOut]:
    """Every price this user has entered, with the gain where a price is known.

    Returns an empty list — not a 409 — while the feature is off, so turning
    it on needs no special-casing in the client.
    """
    rows = holdings.for_user(db, user.id)
    prices = _prices(db, user.id)
    return [_out(h, prices.get(tid)) for tid, h in sorted(rows.items())]


@router.put("/{ticker_id}", response_model=HoldingOut)
def set_holding(
    ticker_id: int,
    payload: HoldingIn,
    user: User = Depends(current_user),
    db: DBSession = Depends(get_db),
) -> HoldingOut:
    """Set or change what the user paid. Same call either way — the user is
    allowed to edit this whenever they like, including after a stock split or
    a second purchase at a different price."""
    _require_enabled(db, user)
    _require_watched(db, user, ticker_id)
    row = holdings.upsert(
        db, user.id, ticker_id, payload.purchase_price, payload.shares
    )
    return _out(row, _prices(db, user.id).get(ticker_id))


@router.delete("/{ticker_id}", status_code=204, response_class=Response)
def delete_holding(
    ticker_id: int, user: User = Depends(current_user), db: DBSession = Depends(get_db)
):
    """Forget the price for one stock, keeping the stock on the watchlist.

    204 whether or not there was a price: the user asked for there to be
    none, and there is none.
    """
    _require_enabled(db, user)
    holdings.clear(db, user.id, ticker_id)
    return Response(status_code=204)
