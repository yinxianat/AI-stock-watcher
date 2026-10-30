"""Tests for cleanup, heartbeat, daily summary, ingest upstream-down alert,
and first-notification event hook."""

from __future__ import annotations

import pytest

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


ET = __import__("zoneinfo").ZoneInfo("America/New_York")


def _utc(y, mo, d, h, mi):
    """A US/Eastern wall-clock time as naive UTC (how the DB stores times)."""
    from datetime import timezone as _tz
    return datetime(y, mo, d, h, mi, tzinfo=ET).astimezone(_tz.utc).replace(tzinfo=None)


def _pipeline_run(started_at, status="SUCCESS", summary=None):
    db = get_session_factory()()
    db.add(JobRun(job_name="batch_pipeline", status=status, started_at=started_at,
                  result_summary=summary))
    db.commit()
    db.close()


@pytest.fixture
def heartbeat_alerts(monkeypatch, caplog):
    from app.jobs import heartbeat as hb
    monkeypatch.setattr(hb, "_reported", set())
    caplog.set_level("INFO", logger="app.jobs.heartbeat")
    return lambda: [r for r in caplog.records
                    if r.name == "app.jobs.heartbeat" and r.levelname == "CRITICAL"]


def test_heartbeat_ok_when_the_last_scheduled_run_started(client, heartbeat_alerts):
    _pipeline_run(_utc(2026, 10, 29, 12, 30))  # Thu 12:30 slot
    assert run_heartbeat(now_utc=_utc(2026, 10, 29, 13, 20)) is True
    assert heartbeat_alerts() == []


def test_heartbeat_quiet_on_monday_morning(client, heartbeat_alerts):
    """Friday 16:05 ran; nothing is due again until Monday 09:35. The old
    "26 hours" rule sent ~10 false alarms here every Monday."""
    _pipeline_run(_utc(2026, 10, 30, 16, 5))  # Fri
    for hour in range(0, 10):  # Mon 00:20 .. 09:20 ET
        assert run_heartbeat(now_utc=_utc(2026, 11, 2, hour, 20)) is True
    assert heartbeat_alerts() == []


def test_heartbeat_alerts_once_per_missed_run(client, heartbeat_alerts):
    _pipeline_run(_utc(2026, 10, 28, 16, 5))  # Wed ran; Thu 09:35 never started
    assert run_heartbeat(now_utc=_utc(2026, 10, 29, 10, 20)) is False
    assert run_heartbeat(now_utc=_utc(2026, 10, 29, 11, 20)) is False  # still missed
    alerts = heartbeat_alerts()
    assert len(alerts) == 1
    assert "Thu 2026-10-29 09:35" in alerts[0].getMessage()


def test_heartbeat_waits_until_a_run_is_late(client, heartbeat_alerts):
    _pipeline_run(_utc(2026, 10, 28, 16, 5))
    # 09:50: the 09:35 run could still be going; Wed 16:05 is what's checked.
    assert run_heartbeat(now_utc=_utc(2026, 10, 29, 9, 50)) is True


def test_heartbeat_does_not_repeat_alerts_for_failed_runs(client, heartbeat_alerts):
    _pipeline_run(_utc(2026, 10, 29, 9, 35), status="FAILED")
    assert run_heartbeat(now_utc=_utc(2026, 10, 29, 10, 20)) is True
    assert heartbeat_alerts() == []  # the pipeline already alerted when it failed


def test_no_runs_are_scheduled_at_weekends():
    from app.jobs.heartbeat import scheduled_slots
    assert scheduled_slots(_utc(2026, 10, 31, 0, 0), _utc(2026, 11, 2, 0, 0)) == []
    assert len(scheduled_slots(_utc(2026, 10, 29, 0, 0), _utc(2026, 10, 30, 0, 0))) == 3


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
    assert "Little Money Monkey" in body
    assert "PIPELINE" in body
    assert "DATABASE ROW COUNTS" in body


def test_summary_counts_pipeline_runs_from_job_runs(client):
    now = datetime.utcnow()
    _pipeline_run(now - timedelta(hours=3),
                  summary="ingest=SUCCESS(elapsed=1.0s, result=12) | compute=SUCCESS(elapsed=0.2s) "
                          "| notify=SUCCESS(elapsed=0.1s, emails_sent=2)")
    _pipeline_run(now - timedelta(hours=1), status="FAILED",
                  summary="ingest=FAILED | compute=SKIPPED | notify=SKIPPED")
    p = collect_summary()["pipeline"]
    assert (p["starts"], p["completed"], p["aborted"]) == (2, 1, 1)
    assert (p["stages_ok"], p["stages_failed"]) == (3, 1)
    i = collect_summary()["ingest"]
    assert i["runs_completed"] == 1
    assert i["last_run_summary"].startswith("ingest=SUCCESS(")


def _summary_data(expected, completed, aborted=0, errors=0, smtp=0):
    return {
        "pipeline": {"expected": expected, "completed": completed, "aborted": aborted,
                     "stages_failed": 0},
        "errors": {"total_24h": errors},
        "smtp": {"failures": smtp},
    }


def test_summary_health():
    from app.jobs.daily_summary import health
    assert health(_summary_data(expected=3, completed=3)) == "HEALTHY"
    assert health(_summary_data(expected=0, completed=0)) == "HEALTHY"  # weekend
    assert health(_summary_data(expected=3, completed=0)) == "UNHEALTHY"
    assert health(_summary_data(expected=3, completed=2, aborted=1)) == "DEGRADED"
    assert health(_summary_data(expected=3, completed=3, smtp=5)) == "UNHEALTHY"
