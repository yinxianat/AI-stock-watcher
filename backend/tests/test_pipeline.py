"""Exercise the ingest -> compute -> notify pipeline end-to-end with a
mocked price fetcher.
"""

from sqlalchemy import select

from app.db.database import get_session_factory
from app.db.seed import seed
from app.jobs.compute import run_compute
from app.jobs.ingest import run_ingest
from app.jobs.notify import run_notify
from app.models import (
    NotificationEventType,
    NotificationLog,
    NotificationRule,
    PriceSnapshot,
    Ticker,
    User,
    WatchlistItem,
)


def _fake_fetcher(symbol: str):
    # Return a +10% move from prev with new week-high.
    return {
        "price": 110.0,
        "week_low": 95.0,
        "week_high": 110.0,
        "month_low": 90.0,
        "month_high": 110.0,
        "quarter_low": 85.0,
        "quarter_high": 110.0,
        "year_low": 80.0,
        "year_high": 110.0,
    }


def test_full_pipeline_sends_one_email(client):
    seed()
    db = get_session_factory()()
    # Build a user + watchlist + range rule directly in the DB.
    user = User(email="al@example.com", notify_email="al@example.com")
    db.add(user)
    db.flush()
    aapl = db.execute(select(Ticker).where(Ticker.symbol == "AAPL")).scalar_one()
    aapl_id = aapl.id
    user_id = user.id
    db.add(WatchlistItem(user_id=user_id, ticker_id=aapl_id))
    db.add(
        NotificationRule(
            user_id=user_id,
            ticker_id=aapl_id,
            event_type=NotificationEventType.PRICE_CHANGE_RANGE,
            pct_low=-5,
            pct_high=5,
            enabled=True,
        )
    )
    # Seed an initial PriceSnapshot so ingest produces a non-null previous_price.
    db.add(PriceSnapshot(ticker_id=aapl_id, price=100.0))
    db.commit()
    db.close()

    n = run_ingest(fetcher=_fake_fetcher)
    assert n == 1

    n2 = run_compute()
    assert n2 == 1

    before = len(client.sent_emails)
    sent = run_notify()
    assert sent == 1
    assert len(client.sent_emails) == before + 1
    assert client.sent_emails[-1]["to"] == "al@example.com"

    # Re-running notify in the same dedup window should NOT re-send.
    sent_again = run_notify()
    assert sent_again == 0


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
    db.add(PriceSnapshot(ticker_id=aapl_id, price=100.0))
    db.commit()
    db.close()

    run_ingest(fetcher=_fake_fetcher)
    run_compute()
    assert run_notify() == 0
