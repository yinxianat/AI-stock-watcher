"""On-demand ingest for a single (user, ticker).

Triggered when a user adds a ticker to their watchlist so they don't have
to wait for the next 09:35 / 12:30 / 16:05 ET batch tick. Reuses the same
`ingest_one_ticker` helper as the batch path — there's only one place that
fetches from yfinance, upserts `daily_closes`, and refreshes the snapshot.

We also compute/refresh the per-user `TrendAnalysis` row so the user sees
their trend data immediately. Notification dispatch is intentionally NOT
triggered here — the batch notify job will pick it up at the next tick.
Sending alerts in response to a sync user action would be surprising
("I just added AAPL and got emailed immediately").
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app.core.settings import get_settings
from app.db.database import get_session_factory
from app.jobs.ingest import PriceFetcher, default_fetcher, ingest_one_ticker
from app.models import (
    PriceSnapshot,
    Ticker,
    TrendAnalysis,
    WatchlistItem,
    utcnow,
)
from app.services.trend import compute_trend

log = logging.getLogger(__name__)


def ingest_ticker_for_user(
    ticker_id: int,
    user_id: int,
    db: DBSession | None = None,
    fetcher: PriceFetcher = default_fetcher,
) -> bool:
    """Fetch+store history+snapshot and per-user trend for one (user, ticker).

    Returns True on success. Safe to call repeatedly — `daily_closes` and
    `price_snapshots` are upserted; the trend row is replaced.
    """
    own = db is None
    if own:
        db = get_session_factory()()
    try:
        ticker = db.get(Ticker, ticker_id)
        if ticker is None:
            log.warning("on_demand: ticker_id=%d not found", ticker_id)
            return False

        watching = db.execute(
            select(WatchlistItem).where(
                WatchlistItem.user_id == user_id,
                WatchlistItem.ticker_id == ticker_id,
            )
        ).scalar_one_or_none()
        if watching is None:
            log.info(
                "on_demand: user=%d does not watch ticker=%d, skipping",
                user_id, ticker_id,
            )
            return False

        if not ingest_one_ticker(db, ticker, fetcher=fetcher):
            db.rollback()
            return False

        snap = db.execute(
            select(PriceSnapshot).where(PriceSnapshot.ticker_id == ticker_id)
        ).scalar_one()

        # Replace this user's trend row for this ticker, leave others alone.
        existing_trend = db.execute(
            select(TrendAnalysis).where(
                TrendAnalysis.user_id == user_id,
                TrendAnalysis.ticker_id == ticker_id,
            )
        ).scalar_one_or_none()
        if existing_trend is not None:
            db.delete(existing_trend)
            db.flush()

        t = compute_trend(snap)
        db.add(
            TrendAnalysis(
                user_id=user_id,
                ticker_id=ticker_id,
                price=snap.price,
                pct_change=t.pct_change,
                is_week_low=t.is_week_low,
                is_week_high=t.is_week_high,
                is_month_low=t.is_month_low,
                is_month_high=t.is_month_high,
                is_quarter_low=t.is_quarter_low,
                is_quarter_high=t.is_quarter_high,
                is_year_low=t.is_year_low,
                is_year_high=t.is_year_high,
                computed_at=utcnow(),
            )
        )
        db.commit()
        log.info(
            "on_demand: ingested %s for user=%d (price=%.2f, prev=%s)",
            ticker.symbol, user_id, snap.price,
            f"{snap.previous_price:.2f}" if snap.previous_price is not None else "n/a",
        )
        return True
    except Exception:
        log.exception(
            "on_demand: ingest failed for user=%d ticker_id=%d",
            user_id, ticker_id,
        )
        try:
            db.rollback()
        except Exception:
            pass
        return False
    finally:
        if own:
            db.close()


def kickoff_ticker_ingest(ticker_id: int, user_id: int) -> None:
    """BackgroundTasks-friendly wrapper.

    Opens its own DB session, swallows all errors (background tasks must
    never crash the request), and is a no-op in the test environment so
    pytest never hits the real yfinance endpoint.
    """
    if get_settings().app_env == "test":
        return
    try:
        ingest_ticker_for_user(ticker_id, user_id)
    except Exception:
        log.exception(
            "on_demand: kickoff crashed for user=%d ticker_id=%d",
            user_id, ticker_id,
        )
