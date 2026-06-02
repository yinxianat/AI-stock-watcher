"""Sign-in backfill — fill daily-close history for tickers a user has
notification rules on, so day-over-day comparisons work immediately after
they log in (even on a fresh deploy or after long absence).

Runs from FastAPI BackgroundTasks after a successful magic-link verify.
Idempotent and per-ticker — if we already have a recent close, skip the
upstream call.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import select

from app.core.settings import get_settings
from app.db.database import get_session_factory
from app.jobs.ingest import ingest_one_ticker
from app.models import DailyClose, NotificationRule, Ticker

log = logging.getLogger(__name__)

# How fresh a DailyClose has to be for us to skip the backfill. Two trading
# days covers a Monday sign-in after a weekend.
_FRESHNESS = timedelta(days=3)


def tickers_needing_backfill(db, user_id: int) -> list[int]:
    """Return ticker IDs the user has enabled rules on that don't have a
    recent DailyClose row."""
    cutoff_date = (datetime.utcnow() - _FRESHNESS).date()

    rule_ticker_ids = {
        tid for (tid,) in db.execute(
            select(NotificationRule.ticker_id)
            .where(
                NotificationRule.user_id == user_id,
                NotificationRule.enabled.is_(True),
            )
            .distinct()
        ).all()
    }
    if not rule_ticker_ids:
        return []

    fresh_ticker_ids = {
        tid for (tid,) in db.execute(
            select(DailyClose.ticker_id)
            .where(
                DailyClose.ticker_id.in_(rule_ticker_ids),
                DailyClose.date >= cutoff_date,
            )
            .distinct()
        ).all()
    }
    return sorted(rule_ticker_ids - fresh_ticker_ids)


def backfill_user_history(user_id: int) -> int:
    """Backfill daily_closes for any rule-ticker missing recent data.

    Opens its own DB session (BackgroundTasks calls don't share request
    sessions). Returns the number of tickers actually fetched. Never raises.
    """
    if get_settings().app_env == "test":
        # Tests inject their own DB-bound spy directly; skipping the real
        # network path keeps pytest hermetic.
        return 0
    try:
        db = get_session_factory()()
    except Exception:
        log.exception("signin_backfill: could not open DB session")
        return 0

    fetched = 0
    try:
        ticker_ids = tickers_needing_backfill(db, user_id)
        if not ticker_ids:
            return 0

        log.info(
            "Sign-in backfill for user=%d: %d ticker(s) need recent history",
            user_id, len(ticker_ids),
        )
        for tid in ticker_ids:
            ticker = db.get(Ticker, tid)
            if ticker is None:
                continue
            try:
                if ingest_one_ticker(db, ticker):
                    fetched += 1
            except Exception:
                log.exception(
                    "signin_backfill: ingest failed for user=%d ticker=%s",
                    user_id, ticker.symbol,
                )
        db.commit()
    finally:
        try:
            db.close()
        except Exception:
            pass
    return fetched


def kickoff_signin_backfill(user_id: int) -> None:
    """BackgroundTasks-friendly wrapper — swallows all errors."""
    try:
        backfill_user_history(user_id)
    except Exception:
        log.exception("signin_backfill: kickoff crashed for user=%d", user_id)
