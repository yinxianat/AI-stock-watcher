"""Durable price-history helpers — the read/write layer over `DailyClose`.

The Option-B redesign promoted `DailyClose` to the source of truth for all
price history. `PriceSnapshot` and `TrendAnalysis` are derived views; this
module is the only place that should `INSERT`/`UPDATE` `DailyClose` and the
canonical way to read window extremes from it.

Idempotency: `upsert_daily_closes` uses (ticker_id, date) as the key and
overwrites the close on conflict, so yfinance returning a corporate-action
adjustment for a past day will correctly update the stored value.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date as date_, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DBSession

from app.models import DailyClose, utcnow

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------


def upsert_daily_closes(
    db: DBSession,
    ticker_id: int,
    rows: Iterable[tuple[date_, float]],
) -> int:
    """Insert/update (ticker_id, date, close) rows. Returns #rows written.

    Uses a load-then-compare strategy rather than a dialect-specific
    ON CONFLICT clause, because the code path is shared between SQLite (dev,
    tests) and Postgres (prod). For ~252 rows/ticker the overhead is trivial.
    """
    rows = list(rows)
    if not rows:
        return 0

    incoming: dict[date_, float] = {}
    for d, c in rows:
        # Defensive: skip non-finite values that yfinance occasionally returns.
        if c is None or not _is_finite(c):
            continue
        incoming[d] = float(c)
    if not incoming:
        return 0

    dates = list(incoming.keys())
    existing = {
        row.date: row
        for row in db.execute(
            select(DailyClose).where(
                DailyClose.ticker_id == ticker_id, DailyClose.date.in_(dates)
            )
        ).scalars()
    }

    now = utcnow()
    written = 0
    for d, c in incoming.items():
        row = existing.get(d)
        if row is None:
            db.add(DailyClose(ticker_id=ticker_id, date=d, close=c, captured_at=now))
            written += 1
        elif row.close != c:
            row.close = c
            row.captured_at = now
            written += 1
    db.flush()
    return written


def _is_finite(x: float) -> bool:
    try:
        return x == x and x not in (float("inf"), float("-inf"))
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# Derived "current view" for a ticker
# ---------------------------------------------------------------------------


@dataclass
class SnapshotFields:
    """Result of `derive_snapshot_for_ticker` — the fields that go onto a
    `PriceSnapshot` row, computed from the DailyClose history."""

    price: float
    previous_price: float | None
    week_low: float | None
    week_high: float | None
    month_low: float | None
    month_high: float | None
    quarter_low: float | None
    quarter_high: float | None
    year_low: float | None
    year_high: float | None
    as_of: date_


# Window sizes in calendar days. "Excluding today" semantics — see derive().
_WINDOWS = {
    "week": 7,
    "month": 30,
    "quarter": 91,
    "year": 365,
}


def derive_snapshot_for_ticker(
    db: DBSession, ticker_id: int
) -> SnapshotFields | None:
    """Build the current-view snapshot fields from the durable history.

    Returns None if there's no DailyClose row for the ticker. `previous_price`
    is the close from the prior trading day in our history (may be None if
    this is the first day of data). The min/max fields cover the prior
    window EXCLUDING the most recent day, so a strict comparison
    `today < snap.week_low` means "today set a new weekly low".
    """
    latest = db.execute(
        select(DailyClose)
        .where(DailyClose.ticker_id == ticker_id)
        .order_by(DailyClose.date.desc())
        .limit(1)
    ).scalar_one_or_none()
    if latest is None:
        return None

    today_d = latest.date

    prev = db.execute(
        select(DailyClose.close)
        .where(DailyClose.ticker_id == ticker_id, DailyClose.date < today_d)
        .order_by(DailyClose.date.desc())
        .limit(1)
    ).scalar_one_or_none()

    extremes: dict[str, tuple[float | None, float | None]] = {}
    for name, days in _WINDOWS.items():
        cutoff = today_d - timedelta(days=days)
        row = db.execute(
            select(func.min(DailyClose.close), func.max(DailyClose.close)).where(
                DailyClose.ticker_id == ticker_id,
                DailyClose.date >= cutoff,
                DailyClose.date < today_d,
            )
        ).one()
        extremes[name] = (row[0], row[1])

    return SnapshotFields(
        price=latest.close,
        previous_price=prev,
        week_low=extremes["week"][0],
        week_high=extremes["week"][1],
        month_low=extremes["month"][0],
        month_high=extremes["month"][1],
        quarter_low=extremes["quarter"][0],
        quarter_high=extremes["quarter"][1],
        year_low=extremes["year"][0],
        year_high=extremes["year"][1],
        as_of=today_d,
    )


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


def prune_daily_closes(db: DBSession, lifetime: timedelta) -> int:
    """Delete DailyClose rows older than `lifetime`. Returns rows deleted.

    Cutoff is computed from `datetime.utcnow().date()`. Idempotent.
    """
    from sqlalchemy import delete

    cutoff = (datetime.utcnow() - lifetime).date()
    result = db.execute(delete(DailyClose).where(DailyClose.date < cutoff))
    db.commit()
    deleted = result.rowcount or 0
    log.info("DailyClose retention: deleted %d rows older than %s", deleted, cutoff)
    return deleted
