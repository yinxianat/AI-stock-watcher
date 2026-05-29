"""Job 2 — Compute trend metrics for every (user, watched ticker).

Reads PriceSnapshot, writes TrendAnalysis. The TrendAnalysis table is
replaced each run, same as PriceSnapshot, since it's a derivative view.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app.db.database import get_session_factory
from app.models import PriceSnapshot, TrendAnalysis, WatchlistItem, utcnow
from app.services.trend import compute_trend

log = logging.getLogger(__name__)


def run_compute(db: DBSession | None = None) -> int:
    """Replace TrendAnalysis with freshly computed rows. Returns row count."""
    own = db is None
    if own:
        db = get_session_factory()()
    try:
        snaps_by_ticker = {
            s.ticker_id: s
            for s in db.execute(select(PriceSnapshot)).scalars().all()
        }
        # Drop previous trend rows.
        for old in db.execute(select(TrendAnalysis)).scalars().all():
            db.delete(old)
        db.flush()

        count = 0
        watch_rows = db.execute(select(WatchlistItem)).scalars().all()
        for w in watch_rows:
            snap = snaps_by_ticker.get(w.ticker_id)
            if snap is None:
                continue
            t = compute_trend(snap)
            db.add(
                TrendAnalysis(
                    user_id=w.user_id,
                    ticker_id=w.ticker_id,
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
            count += 1
        db.commit()
        log.info("Trend compute complete: %d (user, ticker) rows", count)
        return count
    finally:
        if own:
            db.close()
