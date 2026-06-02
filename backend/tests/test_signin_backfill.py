"""Coverage for the sign-in backfill flow.

Contract:
* When a user verifies their magic link, the verify route schedules a
  BackgroundTask to backfill DailyClose history for every ticker the user
  has rules on.
* `tickers_needing_backfill` returns rule-tickers that don't have a
  DailyClose within the last 3 days.
* The backfill is a no-op for tickers with recent data, and skips entirely
  for users with no rules.
"""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select

from app.db.database import get_session_factory
from app.db.seed import seed
from app.models import (
    DailyClose,
    NotificationEventType,
    NotificationRule,
    Ticker,
    User,
    WatchlistItem,
)
from app.services.signin_backfill import (
    kickoff_signin_backfill,
    tickers_needing_backfill,
)


def _make_user_with_rule(symbol: str, email: str = "u@example.com") -> tuple[int, int]:
    seed()
    db = get_session_factory()()
    user = User(email=email, notify_email=email, notify_email_confirmed=True)
    db.add(user)
    db.flush()
    t = db.execute(select(Ticker).where(Ticker.symbol == symbol)).scalar_one()
    db.add(WatchlistItem(user_id=user.id, ticker_id=t.id))
    db.add(NotificationRule(
        user_id=user.id, ticker_id=t.id,
        event_type=NotificationEventType.PRICE_CHANGE_RANGE,
        pct_low=-5.0, pct_high=5.0, enabled=True,
    ))
    db.commit()
    out = (user.id, t.id)
    db.close()
    return out


def test_tickers_needing_backfill_returns_stale_tickers(client):
    user_id, ticker_id = _make_user_with_rule("AAPL")

    db = get_session_factory()()
    needs = tickers_needing_backfill(db, user_id)
    db.close()
    assert needs == [ticker_id]  # no DailyClose at all → needs backfill


def test_tickers_needing_backfill_skips_when_recent_data_exists(client):
    user_id, ticker_id = _make_user_with_rule("AAPL")

    db = get_session_factory()()
    db.add(DailyClose(ticker_id=ticker_id, date=date.today() - timedelta(days=1), close=100.0))
    db.commit()
    needs = tickers_needing_backfill(db, user_id)
    db.close()
    assert needs == []


def test_tickers_needing_backfill_treats_old_data_as_stale(client):
    user_id, ticker_id = _make_user_with_rule("AAPL")

    db = get_session_factory()()
    # >3 days old → counts as stale.
    db.add(DailyClose(
        ticker_id=ticker_id, date=date.today() - timedelta(days=10), close=100.0,
    ))
    db.commit()
    needs = tickers_needing_backfill(db, user_id)
    db.close()
    assert needs == [ticker_id]


def test_tickers_needing_backfill_ignores_disabled_rules(client):
    user_id, ticker_id = _make_user_with_rule("AAPL")

    db = get_session_factory()()
    rule = db.execute(select(NotificationRule)).scalar_one()
    rule.enabled = False
    db.commit()
    needs = tickers_needing_backfill(db, user_id)
    db.close()
    assert needs == []


def test_tickers_needing_backfill_empty_for_user_with_no_rules(client):
    seed()
    db = get_session_factory()()
    user = User(email="norules@example.com", notify_email="norules@example.com")
    db.add(user)
    db.commit()
    uid = user.id
    db.close()

    db = get_session_factory()()
    needs = tickers_needing_backfill(db, uid)
    db.close()
    assert needs == []


# ---------------------------------------------------------------------------
# Verify route integration
# ---------------------------------------------------------------------------


def test_verify_route_schedules_backfill(client, monkeypatch):
    """POST /auth/verify enqueues kickoff_signin_backfill with the user id."""
    from app.api import auth_routes

    calls: list[int] = []
    monkeypatch.setattr(
        auth_routes,
        "kickoff_signin_backfill",
        lambda uid: calls.append(uid),
    )

    # request-link → captures token from email → verify.
    r = client.post("/auth/request-link", json={"email": "back@example.com"})
    assert r.status_code == 204
    body = client.sent_emails[-1]["body_text"]
    token = body.split("token=")[1].split("\n")[0].strip()

    r2 = client.post("/auth/verify", json={"token": token})
    assert r2.status_code == 200
    user_id = r2.json()["user"]["id"]
    assert calls == [user_id]


# ---------------------------------------------------------------------------
# kickoff is a no-op in test env (hermetic suite)
# ---------------------------------------------------------------------------


def test_kickoff_is_noop_in_test_env(client):
    """`kickoff_signin_backfill` short-circuits when APP_ENV=test so the
    pytest suite never touches the network."""
    user_id, ticker_id = _make_user_with_rule("AAPL")
    # Should return cleanly without trying to fetch.
    kickoff_signin_backfill(user_id)
    db = get_session_factory()()
    closes = db.execute(
        select(DailyClose).where(DailyClose.ticker_id == ticker_id)
    ).scalars().all()
    db.close()
    assert closes == []
