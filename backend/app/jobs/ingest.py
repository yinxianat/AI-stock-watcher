"""Job 1 — Ingest current price + period highs/lows from yfinance.

We pull a 1y daily history per ticker, take the most recent close as
"current price", and compute week/month/quarter/year extremes. The previous
PriceSnapshot's `price` becomes the new row's `previous_price`, which lets
the trend job compute pct_change without keeping deeper history.

The whole `price_snapshots` table is REPLACED each run — that's by design;
durable history is not the point of this app.
"""

from __future__ import annotations

import logging
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app.db.database import get_session_factory
from app.models import PriceSnapshot, Ticker, utcnow
from app.services.alerts import alert_event_upstream_api_down

log = logging.getLogger(__name__)

# yfinance is imported lazily inside the fetcher so test code can monkey-patch
# `default_fetcher` without ever touching the network.
PriceFetcher = Callable[[str], dict | None]


def default_fetcher(symbol: str) -> dict | None:  # pragma: no cover (network)
    """Fetch 1y of daily closes for `symbol`, return derived metrics or None."""
    import yfinance as yf

    try:
        hist = yf.Ticker(symbol).history(period="1y", interval="1d", auto_adjust=False)
    except Exception as e:
        log.warning("yfinance fetch failed for %s: %s", symbol, e)
        return None
    if hist is None or hist.empty:
        return None
    closes = hist["Close"].dropna()
    if closes.empty:
        return None
    return _derive_metrics(closes)


def _derive_metrics(closes) -> dict:
    """Given a pandas series of daily closes, derive snapshot fields."""
    import pandas as pd

    latest_idx = closes.index[-1]
    price = float(closes.iloc[-1])

    def slice_window(days: int):
        cutoff = latest_idx - pd.Timedelta(days=days)
        return closes[closes.index >= cutoff]

    return {
        "price": price,
        "week_low": float(slice_window(7).min()),
        "week_high": float(slice_window(7).max()),
        "month_low": float(slice_window(30).min()),
        "month_high": float(slice_window(30).max()),
        "quarter_low": float(slice_window(91).min()),
        "quarter_high": float(slice_window(91).max()),
        "year_low": float(closes.min()),
        "year_high": float(closes.max()),
    }


def run_ingest(
    db: DBSession | None = None,
    fetcher: PriceFetcher = default_fetcher,
    only_watched: bool = True,
) -> int:
    """Refresh the PriceSnapshot table. Returns # of tickers ingested.

    `only_watched=True` (the default) means we skip tickers nobody is
    watching — keeps batch runs cheap on prod. The seed catalog has ~50
    tickers but actual fetches will usually be a small subset.
    """
    own_session = db is None
    if own_session:
        db = get_session_factory()()
    try:
        # Snapshot previous prices for diffing.
        prev = {
            row.ticker_id: row.price
            for row in db.execute(select(PriceSnapshot)).scalars().all()
        }

        if only_watched:
            from app.models import WatchlistItem

            watched_ids = {
                tid
                for (tid,) in db.execute(
                    select(WatchlistItem.ticker_id).distinct()
                ).all()
            }
            if watched_ids:
                tickers = (
                    db.execute(select(Ticker).where(Ticker.id.in_(watched_ids)))
                    .scalars()
                    .all()
                )
            else:
                tickers = []
        else:
            tickers = db.execute(select(Ticker)).scalars().all()

        # Replace strategy: wipe table, re-insert. Cheap at this scale.
        for old in db.execute(select(PriceSnapshot)).scalars().all():
            db.delete(old)
        db.flush()

        count = 0
        attempted = 0
        failed_symbols: list[str] = []
        for t in tickers:
            attempted += 1
            data = fetcher(t.symbol)
            if data is None:
                failed_symbols.append(t.symbol)
                log.info("Skipping %s — no data", t.symbol)
                continue
            db.add(
                PriceSnapshot(
                    ticker_id=t.id,
                    price=data["price"],
                    previous_price=prev.get(t.id),
                    week_low=data["week_low"],
                    week_high=data["week_high"],
                    month_low=data["month_low"],
                    month_high=data["month_high"],
                    quarter_low=data["quarter_low"],
                    quarter_high=data["quarter_high"],
                    year_low=data["year_low"],
                    year_high=data["year_high"],
                    captured_at=utcnow(),
                )
            )
            count += 1
        db.commit()
        log.info(
            "Ingest complete: %d/%d tickers (failed: %d)",
            count, attempted, len(failed_symbols),
        )

        # Upstream-down detection: if we tried tickers and got NOTHING, the
        # data source is broken — alert. Half-failure (>50%) is ERROR but
        # not full outage.
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
