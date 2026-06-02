"""Direct coverage for `app.services.price_history` — the layer that owns
the DailyClose table.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import select

from app.models import DailyClose, Ticker, TickerType
from app.services.price_history import (
    derive_snapshot_for_ticker,
    prune_daily_closes,
    upsert_daily_closes,
)


def _mk_ticker(db_session, symbol: str = "AAPL") -> Ticker:
    t = Ticker(symbol=symbol, name=symbol, type=TickerType.STOCK, is_seeded=True)
    db_session.add(t)
    db_session.flush()
    return t


def test_upsert_inserts_new_rows(db_session):
    t = _mk_ticker(db_session)
    today = date.today()
    rows = [(today - timedelta(days=i), 100.0 + i) for i in range(5)]
    written = upsert_daily_closes(db_session, t.id, rows)
    assert written == 5
    assert db_session.execute(
        select(DailyClose).where(DailyClose.ticker_id == t.id)
    ).scalars().all().__len__() == 5


def test_upsert_updates_changed_close_skips_unchanged(db_session):
    t = _mk_ticker(db_session)
    today = date.today()
    upsert_daily_closes(db_session, t.id, [(today, 100.0), (today - timedelta(days=1), 99.0)])

    # Same dates, one revised, one unchanged.
    written = upsert_daily_closes(
        db_session, t.id,
        [(today, 101.0), (today - timedelta(days=1), 99.0)],
    )
    assert written == 1
    rows = {r.date: r.close for r in db_session.execute(
        select(DailyClose).where(DailyClose.ticker_id == t.id)
    ).scalars()}
    assert rows[today] == 101.0
    assert rows[today - timedelta(days=1)] == 99.0


def test_upsert_skips_non_finite_values(db_session):
    t = _mk_ticker(db_session)
    written = upsert_daily_closes(
        db_session, t.id,
        [(date.today(), float("nan")), (date.today() - timedelta(days=1), 50.0)],
    )
    assert written == 1


def test_derive_returns_none_when_no_history(db_session):
    t = _mk_ticker(db_session)
    assert derive_snapshot_for_ticker(db_session, t.id) is None


def test_derive_prior_window_excludes_today(db_session):
    """`snap.week_low` is the min of the *prior* window — today's close is
    compared AGAINST it, never INCLUDED in it."""
    t = _mk_ticker(db_session)
    today = date.today()
    # Prior 7 days: yesterday=101, ..., 7 days ago=107. Today: 95 (new low).
    rows = [(today - timedelta(days=i), 100.0 + i) for i in range(1, 8)]
    rows.append((today, 95.0))
    upsert_daily_closes(db_session, t.id, rows)
    db_session.commit()

    s = derive_snapshot_for_ticker(db_session, t.id)
    assert s is not None
    assert s.price == 95.0
    assert s.previous_price == 101.0  # yesterday's close
    # week window = prior 7 days → min 101, max 107, today excluded.
    assert s.week_low == 101.0
    assert s.week_high == 107.0
    assert s.price < s.week_low  # caller can flag is_week_low


def test_derive_handles_single_day_of_history(db_session):
    t = _mk_ticker(db_session)
    today = date.today()
    upsert_daily_closes(db_session, t.id, [(today, 100.0)])
    db_session.commit()

    s = derive_snapshot_for_ticker(db_session, t.id)
    assert s is not None
    assert s.price == 100.0
    assert s.previous_price is None
    # Prior windows are empty → min/max are None → strict comparison can't fire.
    assert s.week_low is None
    assert s.week_high is None
    assert s.year_low is None


def test_prune_deletes_rows_older_than_lifetime(db_session):
    t = _mk_ticker(db_session)
    # Backdate by setting dates explicitly.
    old_date = (datetime.utcnow() - timedelta(days=400)).date()
    recent_date = (datetime.utcnow() - timedelta(days=10)).date()
    db_session.add_all([
        DailyClose(ticker_id=t.id, date=old_date, close=10.0),
        DailyClose(ticker_id=t.id, date=recent_date, close=20.0),
    ])
    db_session.commit()

    deleted = prune_daily_closes(db_session, lifetime=timedelta(days=365))
    assert deleted == 1

    remaining = db_session.execute(
        select(DailyClose).where(DailyClose.ticker_id == t.id)
    ).scalars().all()
    assert len(remaining) == 1
    assert remaining[0].date == recent_date
