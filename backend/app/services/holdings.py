"""Purchase-price comparison: what a user paid, and what that is worth now.

Off by default. A user turns it on in Settings (`HoldingsOptIn`), then enters
what they paid per stock or fund (`Holding`). Shares are optional — the
percentage and the per-share difference need only a price, and a family that
just wants "am I up or down" should not have to disclose the size of their
position to find out.

The arithmetic lives here, in one pure function, because two places render
it: the watchlist page and the daily recap email. A second copy in
JavaScript would be a second answer to "what am I up?", and the one in the
email would be the one the user believes.

Privacy. A purchase price is financial data about a real person, so it is
handled like `notify_email`, not like a watchlist row:

* nothing in this module logs a price, a share count or a total, and no
  caller should either — `log.info("holding %s", ...)` would put a family's
  position into `info_logs`, which the admin dashboard renders;
* it is never passed to the AI story pipeline, which sends what it is given
  to a third-party model;
* Data Lens hides `purchase_price` and `shares` unless
  DATA_LENS_SHOW_HOLDINGS is explicitly turned on.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.orm import Session as DBSession

from app.models import Holding, HoldingsOptIn, User

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# The switch
# ---------------------------------------------------------------------------


def tracking_enabled(db: DBSession, user_id: int) -> bool:
    """True once the user has asked for the comparison. False is the default."""
    return db.get(HoldingsOptIn, user_id) is not None


def tracking_user_ids(db: DBSession) -> set[int]:
    """Everyone with the feature on, in one query — the recap runs over every
    user and must not ask per user."""
    return set(db.execute(select(HoldingsOptIn.user_id)).scalars().all())


def set_tracking_enabled(db: DBSession, user: User, enabled: bool) -> None:
    """Turn the comparison on or off. Idempotent either way.

    Turning it off deliberately keeps the `Holding` rows: a user who changes
    their mind should not have to type every price again, and the prices are
    not shown anywhere while the switch is off.
    """
    row = db.get(HoldingsOptIn, user.id)
    if enabled and row is None:
        db.add(HoldingsOptIn(user_id=user.id))
    elif not enabled and row is not None:
        db.delete(row)
    db.commit()


# ---------------------------------------------------------------------------
# The prices
# ---------------------------------------------------------------------------


def for_user(db: DBSession, user_id: int) -> dict[int, Holding]:
    """Every price this user has entered, keyed by ticker id."""
    rows = db.execute(
        select(Holding).where(Holding.user_id == user_id)
    ).scalars().all()
    return {h.ticker_id: h for h in rows}


def get(db: DBSession, user_id: int, ticker_id: int) -> Holding | None:
    return db.execute(
        select(Holding).where(
            Holding.user_id == user_id, Holding.ticker_id == ticker_id
        )
    ).scalar_one_or_none()


def upsert(
    db: DBSession, user_id: int, ticker_id: int,
    purchase_price: float, shares: float | None,
) -> Holding:
    """Set or change what the user paid. Editable at any time, by design.

    `shares=None` clears a share count that was there before, so the user can
    go back to percentages only without deleting the price.
    """
    row = get(db, user_id, ticker_id)
    if row is None:
        row = Holding(
            user_id=user_id, ticker_id=ticker_id,
            purchase_price=purchase_price, shares=shares,
        )
        db.add(row)
    else:
        row.purchase_price = purchase_price
        row.shares = shares
    db.commit()
    db.refresh(row)
    return row


def clear(db: DBSession, user_id: int, ticker_id: int) -> bool:
    """Forget what the user paid for one ticker. True if there was a row."""
    res = db.execute(
        delete(Holding).where(
            Holding.user_id == user_id, Holding.ticker_id == ticker_id
        )
    )
    db.commit()
    return res.rowcount > 0


def delete_for_ticker(db: DBSession, user_id: int, ticker_id: int) -> None:
    """Drop the price when the stock leaves the watchlist. Does not commit —
    the caller is mid-transaction removing the watchlist row and its rules,
    and a price that outlived its stock would reappear on re-adding it."""
    db.execute(
        delete(Holding).where(
            Holding.user_id == user_id, Holding.ticker_id == ticker_id
        )
    )


# ---------------------------------------------------------------------------
# The arithmetic — one copy, used by the watchlist page and the recap email
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GainLoss:
    """What a position is up or down.

    The per-share fields are always present; the totals are None without a
    share count, and every renderer has to handle that rather than
    substituting a zero. A `$0.00` next to a real position reads as "you have
    made nothing", which is a different and wrong statement.
    """

    purchase_price: float
    current_price: float
    shares: float | None
    per_share: float          # current - purchase, per share
    pct: float                # vs. what they paid
    cost_basis: float | None  # purchase_price * shares
    market_value: float | None
    total: float | None       # market_value - cost_basis

    @property
    def is_gain(self) -> bool:
        """Break-even counts as a gain for colouring, not as a loss: showing
        an unchanged position in red reads as bad news that hasn't happened."""
        return self.per_share >= 0


def compute(
    purchase_price: float, shares: float | None, current_price: float | None
) -> GainLoss | None:
    """What this holding is up or down, or None if that cannot be said.

    None — rather than zeros — when there is no current price (a stock added
    today, or a batch that has not run yet) or when the purchase price is not
    usable. "We don't know yet" and "you are even" must not render the same.
    """
    if current_price is None or purchase_price is None:
        return None
    if purchase_price <= 0:
        # Validated against at the API boundary; defended here because this
        # function is also called on rows written by earlier versions.
        return None
    per_share = current_price - purchase_price
    cost_basis = market_value = total = None
    if shares is not None and shares > 0:
        cost_basis = purchase_price * shares
        market_value = current_price * shares
        total = market_value - cost_basis
    return GainLoss(
        purchase_price=purchase_price,
        current_price=current_price,
        shares=shares,
        per_share=per_share,
        pct=per_share / purchase_price * 100.0,
        cost_basis=cost_basis,
        market_value=market_value,
        total=total,
    )


# ---------------------------------------------------------------------------
# Wording, shared by the email and (via the API) the page
# ---------------------------------------------------------------------------


def summary(gl: GainLoss) -> str:
    """One short phrase: "+$100.80 (+8.57%)", or "+$8.40/share (+8.57%)"
    when no share count was given. Plain text, safe in an email or a cell."""
    sign = "+" if gl.per_share >= 0 else "-"
    pct = f"{sign}{abs(gl.pct):.2f}%"
    if gl.total is None:
        return f"{sign}${abs(gl.per_share):,.2f}/share ({pct})"
    return f"{sign}${abs(gl.total):,.2f} ({pct})"
