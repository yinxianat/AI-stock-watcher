"""Regression tests for production deploy behavior.

Two contracts these tests lock down:

1. `seed()` is idempotent and auto-runs on every app startup (lifespan), so
   redeploys to production always have the ticker catalog populated.
2. NEITHER `seed()` NOR the lifespan startup ever deletes user profile data.
   This guards against the historical bug where `Base.metadata.drop_all`
   was being called on every restart, silently wiping users/rules/logs.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import select

from app.db.database import get_session_factory
from app.db.seed import seed
from app.models import (
    DailyClose,
    NotificationEventType,
    NotificationLog,
    NotificationRule,
    Session as DBSessionModel,
    Ticker,
    TickerType,
    User,
    WatchlistItem,
    utcnow,
)


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_seed_is_idempotent(client):
    """Lifespan already seeded on TestClient setup. Re-running seed yields
    zero new rows but leaves the catalog fully populated."""
    # Sanity: catalog is already there from lifespan.
    db = get_session_factory()()
    initial_count = len(db.execute(select(Ticker)).scalars().all())
    db.close()
    assert initial_count > 50

    # Both repeats are no-ops, confirming idempotency under prod redeploys.
    assert seed() == 0
    assert seed() == 0

    db = get_session_factory()()
    final_count = len(db.execute(select(Ticker)).scalars().all())
    db.close()
    assert final_count == initial_count


def test_seed_updates_drifted_name_on_seeded_row(client):
    """If the in-code catalog name changes, the existing seeded row updates."""
    seed()
    db = get_session_factory()()
    aapl = db.execute(select(Ticker).where(Ticker.symbol == "AAPL")).scalar_one()
    aapl.name = "Old name"
    db.commit()
    db.close()

    seed()

    db = get_session_factory()()
    aapl = db.execute(select(Ticker).where(Ticker.symbol == "AAPL")).scalar_one()
    db.close()
    assert aapl.name == "Apple Inc."


def test_seed_does_not_touch_user_added_tickers(client):
    """User-added tickers (is_seeded=False) are never modified by seed,
    even if they share a symbol with a seed entry.

    Simulate: lifespan seeded AAPL, then we 'demote' it to user-owned
    (is_seeded=False) with a custom name — as if the user had added it.
    Subsequent seeds must leave that row untouched.
    """
    db = get_session_factory()()
    aapl = db.execute(select(Ticker).where(Ticker.symbol == "AAPL")).scalar_one()
    aapl.is_seeded = False
    aapl.name = "My custom AAPL"
    db.commit()
    db.close()

    seed()
    seed()

    db = get_session_factory()()
    rows = db.execute(select(Ticker).where(Ticker.symbol == "AAPL")).scalars().all()
    db.close()
    assert len(rows) == 1
    assert rows[0].is_seeded is False
    assert rows[0].name == "My custom AAPL"


# ---------------------------------------------------------------------------
# User-data preservation — the big one
# ---------------------------------------------------------------------------


def _build_full_user_profile(db) -> dict:
    """Populate every user-data table. Returns IDs/values to verify after."""
    user = User(
        email="prod-user@example.com",
        notify_email="prod-user@example.com",
        notify_email_confirmed=True,
        last_login_at=utcnow(),
    )
    db.add(user)
    db.flush()

    # Use a USER-ADDED ticker (is_seeded=False) so we also verify seed
    # doesn't disturb user-owned catalog rows.
    custom = Ticker(symbol="ZZZZ", name="My custom ZZZZ",
                    type=TickerType.STOCK, is_seeded=False)
    db.add(custom)
    db.flush()

    db.add(WatchlistItem(user_id=user.id, ticker_id=custom.id))
    db.add(NotificationRule(
        user_id=user.id, ticker_id=custom.id,
        event_type=NotificationEventType.WEEK_LOW, enabled=True,
    ))
    db.add(NotificationLog(
        user_id=user.id, ticker_id=custom.id,
        event_type=NotificationEventType.WEEK_LOW,
        sent_to="prod-user@example.com",
        summary="ZZZZ hit a new weekly low at $5.00.",
        sent_at=utcnow() - timedelta(hours=1),
    ))
    db.add(DailyClose(
        ticker_id=custom.id, date=date.today() - timedelta(days=1), close=5.0,
    ))
    db.add(DBSessionModel(
        user_id=user.id, token_hash="x" * 64,
        expires_at=datetime.utcnow() + timedelta(days=30),
    ))
    db.commit()
    return {"user_id": user.id, "ticker_id": custom.id}


def _snapshot_user_data(db) -> dict:
    return {
        "users": [(u.id, u.email, u.notify_email) for u in db.execute(select(User)).scalars()],
        "watchlist": [
            (w.user_id, w.ticker_id) for w in db.execute(select(WatchlistItem)).scalars()
        ],
        "rules": [
            (r.user_id, r.ticker_id, r.event_type) for r in db.execute(select(NotificationRule)).scalars()
        ],
        "notif_logs": [
            (n.user_id, n.ticker_id, n.summary)
            for n in db.execute(select(NotificationLog)).scalars()
        ],
        "daily_closes": [
            (d.ticker_id, d.date, d.close)
            for d in db.execute(select(DailyClose)).scalars()
        ],
        "sessions": [
            (s.user_id, s.token_hash) for s in db.execute(select(DBSessionModel)).scalars()
        ],
        "custom_ticker": db.execute(
            select(Ticker).where(Ticker.symbol == "ZZZZ")
        ).scalar_one_or_none(),
    }


def test_seed_preserves_all_user_profile_data(client):
    """Running seed() multiple times must not modify ANY user-data table."""
    db = get_session_factory()()
    _build_full_user_profile(db)
    db.close()

    db = get_session_factory()()
    before = _snapshot_user_data(db)
    db.close()
    assert before["custom_ticker"] is not None

    # Simulate three production redeploys.
    for _ in range(3):
        seed()

    db = get_session_factory()()
    after = _snapshot_user_data(db)
    db.close()

    assert after["users"] == before["users"]
    assert after["watchlist"] == before["watchlist"]
    assert after["rules"] == before["rules"]
    assert after["notif_logs"] == before["notif_logs"]
    assert after["daily_closes"] == before["daily_closes"]
    assert after["sessions"] == before["sessions"]
    # User-added ticker still intact (is_seeded=False, name unchanged).
    assert after["custom_ticker"].name == "My custom ZZZZ"
    assert after["custom_ticker"].is_seeded is False


# ---------------------------------------------------------------------------
# Lifespan integration — proves seed runs on every startup
# ---------------------------------------------------------------------------


def test_lifespan_runs_seed_on_startup(engine, monkeypatch):
    """Spinning up TestClient triggers lifespan, which must seed the catalog."""
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import sessionmaker

    from app.db import database as db_mod
    from app.main import create_app

    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    monkeypatch.setattr(db_mod, "_engine", engine, raising=False)
    monkeypatch.setattr(db_mod, "_SessionLocal", SessionLocal, raising=False)
    monkeypatch.setattr(db_mod, "get_engine", lambda: engine)
    monkeypatch.setattr(db_mod, "get_session_factory", lambda: SessionLocal)

    app = create_app()
    with TestClient(app):
        pass

    db = SessionLocal()
    count = len(db.execute(select(Ticker)).scalars().all())
    db.close()
    assert count > 50  # ETFS + STOCKS combined
