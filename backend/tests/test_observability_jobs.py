"""Tests for cleanup, heartbeat, daily summary, ingest upstream-down alert,
and first-notification event hook."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import select

from app.core.settings import get_settings
from app.db.database import get_session_factory
from app.db.seed import seed
from app.jobs.cleanup import run_cleanup
from app.jobs.daily_summary import collect_summary, run_daily_summary
from app.jobs.heartbeat import run_heartbeat
from app.jobs.ingest import run_ingest
from app.jobs.notify import run_notify
from app.jobs.compute import run_compute
from app.models import (
    JobRun,
    LogEntry,
    NotificationEventType,
    NotificationRule,
    Ticker,
    User,
    WatchlistItem,
)


# -----------------------------------------------------------------------------
# Cleanup
# -----------------------------------------------------------------------------


def test_cleanup_deletes_rows_older_than_lifetime(client, monkeypatch):
    monkeypatch.setenv("LOG_LIFETIME", "7d")
    get_settings.cache_clear()

    db = get_session_factory()()
    now = datetime.utcnow()
    db.add_all([
        LogEntry(level="INFO", logger="x", message="old", created_at=now - timedelta(days=10)),
        LogEntry(level="INFO", logger="x", message="recent", created_at=now - timedelta(days=1)),
    ])
    db.commit()
    db.close()

    deleted = run_cleanup()
    assert deleted == 1

    db = get_session_factory()()
    remaining = db.execute(select(LogEntry).where(LogEntry.logger == "x")).scalars().all()
    db.close()
    assert [r.message for r in remaining] == ["recent"]


def test_cleanup_invalid_lifetime_returns_zero(client, monkeypatch):
    monkeypatch.setenv("LOG_LIFETIME", "totally-bogus")
    get_settings.cache_clear()
    assert run_cleanup() == 0


# -----------------------------------------------------------------------------
# Heartbeat
# -----------------------------------------------------------------------------


def test_heartbeat_ok_when_recent_pipeline_complete(client):
    db = get_session_factory()()
    db.add(
        JobRun(
            job_name="batch_pipeline",
            status="SUCCESS",
            started_at=datetime.utcnow() - timedelta(hours=1),
        )
    )
    db.commit()
    db.close()
    assert run_heartbeat() is True


def test_heartbeat_alerts_when_no_recent_pipeline(client):
    # No JobRun rows exist at all → heartbeat should report missing.
    assert run_heartbeat() is False


def test_heartbeat_ignores_old_pipeline_completes(client):
    db = get_session_factory()()
    db.add(
        JobRun(
            job_name="batch_pipeline",
            status="SUCCESS",
            started_at=datetime.utcnow() - timedelta(hours=48),
        )
    )
    db.commit()
    db.close()
    assert run_heartbeat() is False


# -----------------------------------------------------------------------------
# Ingest upstream-down alert
# -----------------------------------------------------------------------------


def test_ingest_zero_success_triggers_critical_alert(client, monkeypatch):
    """If fetcher returns None for everything, an upstream-down alert fires."""
    seed()
    db = get_session_factory()()
    user = User(email="x@example.com", notify_email="x@example.com")
    db.add(user)
    db.flush()
    aapl = db.execute(select(Ticker).where(Ticker.symbol == "AAPL")).scalar_one()
    db.add(WatchlistItem(user_id=user.id, ticker_id=aapl.id))
    db.commit()
    db.close()

    events: list[str] = []
    from app.jobs import ingest as ingest_mod
    monkeypatch.setattr(
        ingest_mod,
        "alert_event_upstream_api_down",
        lambda attempted, succeeded, errors: events.append(f"down: {attempted}/{succeeded}"),
    )

    count = run_ingest(fetcher=lambda symbol: None)
    assert count == 0
    assert any(e.startswith("down: 1/0") for e in events)


def test_ingest_partial_success_does_not_alert(client, monkeypatch):
    """Some succeed, some fail — no upstream-down alert."""
    seed()
    db = get_session_factory()()
    user = User(email="y@example.com", notify_email="y@example.com")
    db.add(user)
    db.flush()
    syms = ["AAPL", "MSFT", "GOOG", "AMZN"]
    ids = [
        db.execute(select(Ticker).where(Ticker.symbol == s)).scalar_one().id
        for s in syms
    ]
    for tid in ids:
        db.add(WatchlistItem(user_id=user.id, ticker_id=tid))
    db.commit()
    db.close()

    events: list[str] = []
    from app.jobs import ingest as ingest_mod
    monkeypatch.setattr(
        ingest_mod,
        "alert_event_upstream_api_down",
        lambda *a, **k: events.append("down"),
    )

    # 2 of 4 succeed → 50% — borderline, should NOT trigger CRITICAL alert
    # (only triggers when count == 0).
    today = date.today()
    history = [(today - timedelta(days=i), 100.0) for i in range(7, 0, -1)]
    history.append((today, 100.0))

    def fetcher(symbol):
        if symbol in ("AAPL", "MSFT"):
            return history
        return None

    count = run_ingest(fetcher=fetcher)
    assert count == 2
    assert events == []  # upstream-down alert never fired


# -----------------------------------------------------------------------------
# First-notification event hook
# -----------------------------------------------------------------------------


def _fetcher_with_week_high(symbol: str):
    """Return a history where today is strictly above the prior week's max."""
    today = date.today()
    rows = [(today - timedelta(days=i), 100.0) for i in range(7, 0, -1)]
    rows.append((today, 110.0))
    return rows


def _setup_user_with_week_high_rule(email: str = "new@example.com") -> tuple[int, int]:
    db = get_session_factory()()
    user = User(email=email, notify_email=email, notify_email_confirmed=True)
    db.add(user)
    db.flush()
    aapl = db.execute(select(Ticker).where(Ticker.symbol == "AAPL")).scalar_one()
    db.add(WatchlistItem(user_id=user.id, ticker_id=aapl.id))
    db.add(NotificationRule(
        user_id=user.id,
        ticker_id=aapl.id,
        event_type=NotificationEventType.WEEK_HIGH,
        enabled=True,
    ))
    db.commit()
    user_id, ticker_id = user.id, aapl.id
    db.close()
    return user_id, ticker_id


def test_first_notification_fires_event_alert(client, monkeypatch):
    seed()
    _setup_user_with_week_high_rule()

    events: list[tuple] = []
    from app.jobs import notify as notify_mod
    monkeypatch.setattr(
        notify_mod,
        "alert_event_first_notification",
        lambda email, sym, evt, summary: events.append((email, sym, evt)),
    )

    run_ingest(fetcher=_fetcher_with_week_high)
    run_compute()
    sent = run_notify()
    assert sent == 1
    assert events == [("new@example.com", "AAPL", "week_high")]


def test_subsequent_notifications_do_not_fire_event(client, monkeypatch):
    seed()
    _setup_user_with_week_high_rule(email="repeat-user@example.com")

    events: list[tuple] = []
    from app.jobs import notify as notify_mod
    monkeypatch.setattr(
        notify_mod,
        "alert_event_first_notification",
        lambda email, sym, evt, summary: events.append((email, sym, evt)),
    )

    # First run — fires.
    run_ingest(fetcher=_fetcher_with_week_high)
    run_compute()
    run_notify()
    assert len(events) == 1

    # Clear the per-batch dedup by removing the recent notification_log row
    # for this user so the rule can re-trigger; then second run must NOT
    # produce a new "first-notification" event.
    from app.models import NotificationLog
    db = get_session_factory()()
    for row in db.execute(select(NotificationLog)).scalars().all():
        db.delete(row)
    db.commit()
    # Re-insert a far-past notification so dedup window doesn't suppress.
    db.add(NotificationLog(
        user_id=1, ticker_id=1,
        event_type=NotificationEventType.WEEK_HIGH,
        sent_to="repeat-user@example.com",
        summary="old",
        sent_at=datetime.utcnow() - timedelta(days=1),
    ))
    db.commit()
    db.close()

    run_ingest(fetcher=_fetcher_with_week_high)
    run_compute()
    run_notify()
    # Still only the one event from the first run.
    assert len(events) == 1


# -----------------------------------------------------------------------------
# Daily summary
# -----------------------------------------------------------------------------


def test_collect_summary_includes_all_sections(client):
    seed()
    data = collect_summary()
    expected_top_keys = {
        "generated_at", "window_hours", "pipeline", "ingest", "notify",
        "smtp", "auth", "errors", "db_rows", "system", "schedule",
    }
    assert expected_top_keys.issubset(data.keys())
    # row counts always present
    assert "tickers" in data["db_rows"]
    # schedule reports batch times
    assert isinstance(data["schedule"]["batch_times_et"], list)


def test_run_daily_summary_calls_notify_admin(client, monkeypatch):
    captured: list[tuple] = []
    from app.jobs import daily_summary as ds_mod
    monkeypatch.setattr(
        ds_mod,
        "notify_admin",
        lambda event, subject, body, body_html=None, channel=None: captured.append(
            (event, subject, body)
        ),
    )

    data = run_daily_summary()
    assert isinstance(data, dict)
    assert len(captured) == 1
    event, subject, body = captured[0]
    assert event == "daily_summary"
    assert "AI Stock Watcher" in body
    assert "PIPELINE" in body
    assert "DATABASE ROW COUNTS" in body
