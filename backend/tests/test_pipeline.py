"""Exercise the ingest -> compute -> notify pipeline end-to-end with a
mocked price fetcher.

Post-redesign, the fetcher returns a list of (date, close) pairs covering
the relevant window. The ingest job upserts those into `daily_closes`,
re-derives the `PriceSnapshot`, and compute/notify do their thing.
"""

from datetime import date, timedelta

from sqlalchemy import select

from app.db.database import get_session_factory
from app.db.seed import seed
from app.jobs.compute import run_compute
from app.jobs.ingest import run_ingest
from app.jobs.notify import run_notify
from app.models import (
    NotificationEventType,
    NotificationRule,
    Ticker,
    User,
    WatchlistItem,
)


def _history_with_jump(today: date, prior_close: float, today_close: float):
    """Return (date, close) rows: a flat prior week then a single jump today."""
    rows = []
    # 30 days of flat prior closes — gives us prior week/month/quarter mins/maxes.
    for i in range(30, 0, -1):
        rows.append((today - timedelta(days=i), prior_close))
    rows.append((today, today_close))
    return rows


def _fake_fetcher_factory(today: date, prior: float, current: float):
    def _fetch(_symbol):
        return _history_with_jump(today, prior, current)
    return _fetch


def test_daily_pipeline_does_not_fire_price_change_range(client):
    """PRICE_CHANGE_RANGE rules are intraday-driven only — the daily
    ingest+compute+notify path must NOT send them, even on a big day-over-
    day move. (Intraday path covered by tests/test_intraday.py.)"""
    seed()
    db = get_session_factory()()
    user = User(email="al@example.com", notify_email="al@example.com")
    db.add(user)
    db.flush()
    aapl = db.execute(select(Ticker).where(Ticker.symbol == "AAPL")).scalar_one()
    aapl_id, user_id = aapl.id, user.id
    db.add(WatchlistItem(user_id=user_id, ticker_id=aapl_id))
    db.add(
        NotificationRule(
            user_id=user_id, ticker_id=aapl_id,
            event_type=NotificationEventType.PRICE_CHANGE_RANGE,
            pct_low=-5, pct_high=5, enabled=True,
        )
    )
    db.commit()
    db.close()

    today = date.today()
    run_ingest(fetcher=_fake_fetcher_factory(today, 100.0, 110.0))
    run_compute()
    before = len(client.sent_emails)
    assert run_notify() == 0
    assert len(client.sent_emails) == before


def test_full_pipeline_fires_strict_new_low(client):
    """A 30-day-low rule fires only when today is strictly below the prior min."""
    seed()
    db = get_session_factory()()
    user = User(email="lo@example.com", notify_email="lo@example.com")
    db.add(user)
    db.flush()
    aapl = db.execute(select(Ticker).where(Ticker.symbol == "AAPL")).scalar_one()
    aapl_id, user_id = aapl.id, user.id
    db.add(WatchlistItem(user_id=user_id, ticker_id=aapl_id))
    db.add(
        NotificationRule(
            user_id=user_id, ticker_id=aapl_id,
            event_type=NotificationEventType.MONTH_LOW, enabled=True,
        )
    )
    db.commit()
    db.close()

    today = date.today()
    # First run: today equals the prior min → NOT a new low.
    run_ingest(fetcher=_fake_fetcher_factory(today, 100.0, 100.0))
    run_compute()
    assert run_notify() == 0

    # Second run (next day): today strictly below prior min → fires.
    next_day = today + timedelta(days=1)
    run_ingest(fetcher=_fake_fetcher_factory(next_day, 100.0, 90.0))
    run_compute()
    assert run_notify() == 1


def test_notify_suppressed_when_email_unconfirmed(client):
    seed()
    db = get_session_factory()()
    user = User(
        email="al@example.com", notify_email="new@example.com",
        notify_email_confirmed=False,
    )
    db.add(user)
    db.flush()
    aapl = db.execute(select(Ticker).where(Ticker.symbol == "AAPL")).scalar_one()
    aapl_id, user_id = aapl.id, user.id
    db.add(WatchlistItem(user_id=user_id, ticker_id=aapl_id))
    db.add(
        NotificationRule(
            user_id=user_id, ticker_id=aapl_id,
            event_type=NotificationEventType.WEEK_HIGH, enabled=True,
        )
    )
    db.commit()
    db.close()

    today = date.today()
    run_ingest(fetcher=_fake_fetcher_factory(today, 100.0, 110.0))
    run_compute()
    assert run_notify() == 0
