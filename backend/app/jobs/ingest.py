"""Job 1 — Pull price data from yfinance and update durable history.

For each watched ticker we:

1. Fetch a year of daily closes from yfinance.
2. Upsert every (date, close) row into `daily_closes` (the source of truth).
3. Rebuild the derived `PriceSnapshot` row from the just-written history,
   so `previous_price` is yesterday's actual close and
   `week_low`/`month_low`/... are the prior-window extremes EXCLUDING today
   (enabling strict "new low" / "new high" detection in compute).

`daily_closes` is durable (retention = PRICE_HISTORY_LIFETIME). Only
`price_snapshots` is replaced on each run.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import date as date_
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app.db.database import get_session_factory
from app.models import PriceSnapshot, Ticker, utcnow
from app.services.alerts import alert_event_upstream_api_down
from app.services.price_history import (
    derive_snapshot_for_ticker,
    upsert_daily_closes,
)

log = logging.getLogger(__name__)

# Fetcher returns a list of (date, close) rows in date-ascending order, or
# None on failure. Provider routed via `STOCK_DATA_PROVIDER` setting so tests
# can swap it out without ever touching the network.
PriceFetcher = Callable[[str], list[tuple[date_, float]] | None]


def default_fetcher(symbol: str) -> list[tuple[date_, float]] | None:  # pragma: no cover (network)
    """Fetch 1y of daily (date, close) pairs via the configured provider."""
    from app.services.stock_data import get_daily_history

    return get_daily_history(symbol)


# ---------------------------------------------------------------------------
# Per-ticker ingest (reusable by on-demand path)
# ---------------------------------------------------------------------------


def ingest_one_ticker(
    db: DBSession,
    ticker: Ticker,
    fetcher: PriceFetcher = default_fetcher,
) -> bool:
    """Fetch, upsert daily_closes, and refresh the PriceSnapshot row for one
    ticker. Returns True iff we got usable data.

    Caller must commit the session. We only commit our own work if the
    derived snapshot couldn't be built (i.e. nothing changed in `price_snapshots`).
    """
    rows = fetcher(ticker.symbol)
    if not rows:
        log.info("Skipping %s — no data", ticker.symbol)
        return False

    upsert_daily_closes(db, ticker.id, rows)

    derived = derive_snapshot_for_ticker(db, ticker.id)
    if derived is None:
        # Shouldn't happen — we just inserted at least one row.
        log.warning("Ingest %s: history empty after upsert?", ticker.symbol)
        return False

    # Replace this ticker's snapshot row only — no global wipe.
    existing = db.execute(
        select(PriceSnapshot).where(PriceSnapshot.ticker_id == ticker.id)
    ).scalar_one_or_none()
    if existing is not None:
        db.delete(existing)
        db.flush()

    db.add(
        PriceSnapshot(
            ticker_id=ticker.id,
            price=derived.price,
            previous_price=derived.previous_price,
            week_low=derived.week_low,
            week_high=derived.week_high,
            month_low=derived.month_low,
            month_high=derived.month_high,
            quarter_low=derived.quarter_low,
            quarter_high=derived.quarter_high,
            year_low=derived.year_low,
            year_high=derived.year_high,
            captured_at=utcnow(),
        )
    )
    db.flush()
    return True


# ---------------------------------------------------------------------------
# Batch entrypoint
# ---------------------------------------------------------------------------


def _watched_ticker_ids(db: DBSession) -> set[int]:
    from app.models import WatchlistItem

    return {
        tid
        for (tid,) in db.execute(
            select(WatchlistItem.ticker_id).distinct()
        ).all()
    }


def run_ingest(
    db: DBSession | None = None,
    fetcher: PriceFetcher = default_fetcher,
    only_watched: bool = True,
) -> int:
    """Refresh price history + snapshots for watched tickers.

    Returns the count of tickers we successfully ingested.
    """
    own_session = db is None
    if own_session:
        db = get_session_factory()()
    try:
        if only_watched:
            watched_ids = _watched_ticker_ids(db)
            if not watched_ids:
                tickers: Iterable[Ticker] = []
            else:
                tickers = (
                    db.execute(select(Ticker).where(Ticker.id.in_(watched_ids)))
                    .scalars()
                    .all()
                )
        else:
            tickers = db.execute(select(Ticker)).scalars().all()

        count = 0
        attempted = 0
        failed_symbols: list[str] = []
        for t in tickers:
            attempted += 1
            try:
                if ingest_one_ticker(db, t, fetcher=fetcher):
                    count += 1
                else:
                    failed_symbols.append(t.symbol)
            except Exception:
                failed_symbols.append(t.symbol)
                log.exception("Ingest failed for %s", t.symbol)

        db.commit()
        log.info(
            "Ingest complete: %d/%d tickers (failed: %d)",
            count, attempted, len(failed_symbols),
        )

        # Upstream-down detection (same thresholds as before).
        if attempted > 0 and count == 0:
            log.critical(
                "Ingest fetched 0 of %d tickers — upstream price API appears down",
                attempted,
            )
            alert_event_upstream_api_down(attempted, count, failed_symbols)
        elif attempted >= 4 and count / attempted < 0.5:
            log.error(
                "Ingest success rate %d/%d (<50%%) — partial upstream failure",
                count, attempted,
            )

        return count
    finally:
        if own_session:
            db.close()
