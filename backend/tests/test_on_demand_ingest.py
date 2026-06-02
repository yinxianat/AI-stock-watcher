"""Tests for `app.jobs.on_demand` — single-ticker upsert on watchlist add."""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select

from app.db.seed import seed
from app.jobs.on_demand import ingest_ticker_for_user
from app.models import (
    DailyClose,
    PriceSnapshot,
    Ticker,
    TickerType,
    TrendAnalysis,
    User,
    WatchlistItem,
)


def _history(today: date, prior: float, today_close: float):
    """30 prior flat days + today — enough to fill week/month/quarter windows."""
    rows = [(today - timedelta(days=i), prior) for i in range(30, 0, -1)]
    rows.append((today, today_close))
    return rows


def _make_user_watching(db_session, email: str, symbol: str) -> tuple[User, Ticker]:
    user = User(email=email, notify_email=email)
    db_session.add(user)
    ticker = Ticker(symbol=symbol, name=symbol, type=TickerType.STOCK, is_seeded=True)
    db_session.add(ticker)
    db_session.flush()
    db_session.add(WatchlistItem(user_id=user.id, ticker_id=ticker.id))
    db_session.commit()
    return user, ticker


def test_ingest_writes_history_snapshot_and_trend(db_session):
    """The on-demand helper backfills daily_closes, builds a PriceSnapshot
    with previous_price = prior trading day's close, and writes a trend row."""
    user, ticker = _make_user_watching(db_session, "bob@example.com", "AAPL")
    today = date.today()

    def fetch(_symbol):
        return _history(today, 100.0, 90.0)  # strictly below prior month min

    ok = ingest_ticker_for_user(ticker.id, user.id, db=db_session, fetcher=fetch)
    assert ok is True

    # daily_closes: 30 prior + today = 31 rows.
    closes = db_session.execute(
        select(DailyClose).where(DailyClose.ticker_id == ticker.id)
    ).scalars().all()
    assert len(closes) == 31

    snap = db_session.execute(
        select(PriceSnapshot).where(PriceSnapshot.ticker_id == ticker.id)
    ).scalar_one()
    assert snap.price == 90.0
    assert snap.previous_price == 100.0  # yesterday's close
    assert snap.month_low == 100.0  # prior window min (excludes today)

    trend = db_session.execute(
        select(TrendAnalysis).where(
            TrendAnalysis.user_id == user.id,
            TrendAnalysis.ticker_id == ticker.id,
        )
    ).scalar_one()
    assert trend.is_week_low is True  # 90 < 100 prior min
    assert trend.is_month_low is True
    assert round(trend.pct_change, 2) == -10.0


def test_ingest_is_idempotent_and_upserts_revised_closes(db_session):
    """Re-running with revised values for the same dates updates in place."""
    user, ticker = _make_user_watching(db_session, "carol@example.com", "MSFT")
    today = date.today()

    def fetch_v1(_symbol):
        return _history(today, 100.0, 100.0)

    def fetch_v2(_symbol):
        # Same dates, revised closes (simulates a corporate-action adjustment).
        return _history(today, 99.0, 99.0)

    assert ingest_ticker_for_user(ticker.id, user.id, db=db_session, fetcher=fetch_v1)
    assert ingest_ticker_for_user(ticker.id, user.id, db=db_session, fetcher=fetch_v2)

    # Same 31 rows — upsert, not append.
    closes = db_session.execute(
        select(DailyClose).where(DailyClose.ticker_id == ticker.id)
    ).scalars().all()
    assert len(closes) == 31
    assert all(c.close == 99.0 for c in closes)

    snaps = db_session.execute(
        select(PriceSnapshot).where(PriceSnapshot.ticker_id == ticker.id)
    ).scalars().all()
    assert len(snaps) == 1
    assert snaps[0].price == 99.0
    assert snaps[0].previous_price == 99.0


def test_ingest_refuses_unwatched_ticker(db_session):
    """Users can't trigger ingest for tickers they don't watch."""
    user = User(email="dave@example.com", notify_email="dave@example.com")
    ticker = Ticker(symbol="GOOGL", name="GOOGL", type=TickerType.STOCK, is_seeded=True)
    db_session.add_all([user, ticker])
    db_session.commit()

    ok = ingest_ticker_for_user(
        ticker.id, user.id, db=db_session,
        fetcher=lambda _: _history(date.today(), 100.0, 100.0),
    )
    assert ok is False
    # No history written.
    assert db_session.execute(
        select(DailyClose).where(DailyClose.ticker_id == ticker.id)
    ).first() is None


def test_add_watch_schedules_background_ingest(signed_in, monkeypatch):
    """POST /watchlist enqueues the on-demand ingest with the right args."""
    from app.api import watchlist_routes

    calls: list[tuple[int, int]] = []

    def _spy(ticker_id: int, user_id: int) -> None:
        calls.append((ticker_id, user_id))

    monkeypatch.setattr(watchlist_routes, "kickoff_ticker_ingest", _spy)

    seed()
    c = signed_in["client"]
    r = c.post("/watchlist", json={"symbol": "AAPL"}, headers=signed_in["auth"])
    assert r.status_code == 201
    body = r.json()
    assert len(calls) == 1
    assert calls[0][0] == body["ticker"]["id"]
    assert calls[0][1] == signed_in["user"]["id"]

    # Idempotent add (already watching) should NOT re-enqueue ingest.
    r2 = c.post("/watchlist", json={"symbol": "AAPL"}, headers=signed_in["auth"])
    assert r2.status_code in (200, 201)
    assert len(calls) == 1
