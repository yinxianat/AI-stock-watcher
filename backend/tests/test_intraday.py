"""Coverage for the intraday capture + day-over-day notification path."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.db.database import get_session_factory
from app.db.seed import seed
from app.jobs.intraday import (
    DEDUP_WINDOW,
    SIGNAL_DAILY,
    is_market_hours_now,
    run_intraday_capture,
)
from app.models import (
    DailyClose,
    IntradayPrice,
    NotificationEventType,
    NotificationLog,
    NotificationRule,
    Ticker,
    User,
    WatchlistItem,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _setup_user_with_range_rule(symbol: str, pct_low: float, pct_high: float,
                                email: str = "alerts@example.com") -> tuple[int, int]:
    """Returns (user_id, ticker_id) for a user watching `symbol` with a
    PRICE_CHANGE_RANGE rule and a confirmed notify email."""
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
        pct_low=pct_low, pct_high=pct_high, enabled=True,
    ))
    db.commit()
    out = (user.id, t.id)
    db.close()
    return out


def _staged_fetcher(prices: list[float]):
    """Returns a fetcher that yields prices in order across calls.

    The order across tickers is undefined, but for these tests we only have
    one watched ticker so we get exactly one call per `run_intraday_capture`.
    """
    state = {"i": 0}

    def _fetch(_symbol):
        v = prices[state["i"]]
        state["i"] += 1
        return v

    return _fetch


# ---------------------------------------------------------------------------
# is_market_hours_now
# ---------------------------------------------------------------------------


def test_market_hours_filter_weekend(client):
    # 2026-06-06 is a Saturday.
    sat = datetime(2026, 6, 6, 14, 0, tzinfo=timezone.utc)
    assert is_market_hours_now(sat) is False


def test_market_hours_filter_before_open(client):
    # 2026-06-08 Monday 12:00 UTC = 08:00 ET (DST) — before 09:30 ET open.
    early = datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc)
    assert is_market_hours_now(early) is False


def test_market_hours_filter_during_session(client):
    # 2026-06-08 Monday 17:00 UTC = 13:00 ET — inside the session.
    mid = datetime(2026, 6, 8, 17, 0, tzinfo=timezone.utc)
    assert is_market_hours_now(mid) is True


def test_market_hours_filter_after_close(client):
    # 2026-06-08 Monday 21:00 UTC = 17:00 ET — after 16:00 ET close.
    late = datetime(2026, 6, 8, 21, 0, tzinfo=timezone.utc)
    assert is_market_hours_now(late) is False


# ---------------------------------------------------------------------------
# Capture + day-over-day math
# ---------------------------------------------------------------------------


def test_first_tick_writes_row_without_firing(client):
    """A ticker with no previous close can't have a % change → no alert."""
    user_id, ticker_id = _setup_user_with_range_rule("AAPL", -1.0, 1.0)

    sent = run_intraday_capture(fetcher=_staged_fetcher([100.0]), force=True)
    assert sent == 0
    assert len(client.sent_emails) == 0

    db = get_session_factory()()
    rows = db.execute(
        select(IntradayPrice).where(IntradayPrice.ticker_id == ticker_id)
    ).scalars().all()
    db.close()
    assert len(rows) == 1
    assert rows[0].price == 100.0


def test_second_tick_outside_band_fires_alert(client):
    """+5% vs the previous close with a ±1% band → fires one email."""
    user_id, ticker_id = _setup_user_with_range_rule("AAPL", -1.0, 1.0)
    _seed_prior_day_close(ticker_id, close=100.0)

    run_intraday_capture(fetcher=_staged_fetcher([100.0]), force=True)
    sent = run_intraday_capture(fetcher=_staged_fetcher([105.0]), force=True)
    assert sent == 1
    assert len(client.sent_emails) == 1
    body = client.sent_emails[0]["body_text"]
    assert "AAPL" in body
    assert "5.00%" in body  # |+5.00%|
    assert "up" in body
    # Notification log written.
    db = get_session_factory()()
    logs = db.execute(select(NotificationLog)).scalars().all()
    db.close()
    assert len(logs) == 1
    assert logs[0].user_id == user_id


def test_second_tick_inside_band_does_not_fire(client):
    """+0.5% with a ±1% band → silent."""
    _, ticker_id = _setup_user_with_range_rule("AAPL", -1.0, 1.0)
    _seed_prior_day_close(ticker_id, close=100.0)

    run_intraday_capture(fetcher=_staged_fetcher([100.0]), force=True)
    sent = run_intraday_capture(fetcher=_staged_fetcher([100.5]), force=True)
    assert sent == 0
    assert len(client.sent_emails) == 0


def test_dedup_suppresses_repeat_fires_within_window(client):
    """Once a rule fires, repeated outside-band ticks in the dedup window
    must not re-send. After the window, a new tick can re-fire."""
    user_id, ticker_id = _setup_user_with_range_rule("AAPL", -1.0, 1.0)
    _seed_prior_day_close(ticker_id, close=100.0)

    run_intraday_capture(fetcher=_staged_fetcher([100.0]), force=True)
    assert run_intraday_capture(fetcher=_staged_fetcher([105.0]), force=True) == 1

    # Same magnitude move again 10 min later — dedup suppresses.
    assert run_intraday_capture(fetcher=_staged_fetcher([110.0]), force=True) == 0
    assert len(client.sent_emails) == 1

    # Backdate the prior NotificationLog past the dedup window.
    db = get_session_factory()()
    log_row = db.execute(select(NotificationLog)).scalar_one()
    log_row.sent_at = log_row.sent_at - (DEDUP_WINDOW + timedelta(minutes=1))
    db.commit()
    db.close()

    assert run_intraday_capture(fetcher=_staged_fetcher([115.0]), force=True) == 1
    assert len(client.sent_emails) == 2


def test_unconfirmed_email_blocks_alert(client):
    """Same rule, but notify_email_confirmed=False → no email sent."""
    seed()
    db = get_session_factory()()
    user = User(email="u@example.com", notify_email="u@example.com",
                notify_email_confirmed=False)
    db.add(user)
    db.flush()
    t = db.execute(select(Ticker).where(Ticker.symbol == "AAPL")).scalar_one()
    db.add(WatchlistItem(user_id=user.id, ticker_id=t.id))
    db.add(NotificationRule(
        user_id=user.id, ticker_id=t.id,
        event_type=NotificationEventType.PRICE_CHANGE_RANGE,
        pct_low=-1.0, pct_high=1.0, enabled=True,
    ))
    db.commit()
    ticker_id = t.id
    db.close()
    _seed_prior_day_close(ticker_id, close=100.0)

    run_intraday_capture(fetcher=_staged_fetcher([100.0]), force=True)
    assert run_intraday_capture(fetcher=_staged_fetcher([110.0]), force=True) == 0
    assert len(client.sent_emails) == 0


def test_disabled_rule_does_not_fire(client):
    """Rules with enabled=False are skipped, even on a big move."""
    _, ticker_id = _setup_user_with_range_rule("AAPL", -1.0, 1.0)
    _seed_prior_day_close(ticker_id, close=100.0)
    db = get_session_factory()()
    rule = db.execute(select(NotificationRule)).scalar_one()
    rule.enabled = False
    db.commit()
    db.close()

    run_intraday_capture(fetcher=_staged_fetcher([100.0]), force=True)
    assert run_intraday_capture(fetcher=_staged_fetcher([200.0]), force=True) == 0


def test_only_price_change_range_rules_fire_intraday(client):
    """Even a giant move shouldn't trigger a WEEK_HIGH rule from this path
    — high/low rules are handled by the daily compute job."""
    seed()
    db = get_session_factory()()
    user = User(email="hi@example.com", notify_email="hi@example.com",
                notify_email_confirmed=True)
    db.add(user)
    db.flush()
    t = db.execute(select(Ticker).where(Ticker.symbol == "AAPL")).scalar_one()
    db.add(WatchlistItem(user_id=user.id, ticker_id=t.id))
    db.add(NotificationRule(
        user_id=user.id, ticker_id=t.id,
        event_type=NotificationEventType.WEEK_HIGH, enabled=True,
    ))
    db.commit()
    ticker_id = t.id
    db.close()
    _seed_prior_day_close(ticker_id, close=100.0)

    run_intraday_capture(fetcher=_staged_fetcher([100.0]), force=True)
    assert run_intraday_capture(fetcher=_staged_fetcher([500.0]), force=True) == 0
    assert len(client.sent_emails) == 0


def _seed_prior_day_close(ticker_id: int, close: float) -> None:
    """Insert a DailyClose for ticker dated yesterday."""
    from datetime import date, timedelta as _td
    db = get_session_factory()()
    db.add(DailyClose(
        ticker_id=ticker_id,
        date=date.today() - _td(days=1),
        close=close,
    ))
    db.commit()
    db.close()


# ---------------------------------------------------------------------------
# Day-over-day baseline
# ---------------------------------------------------------------------------


def test_daily_baseline_fires_on_first_tick(client):
    """Even with no prior intraday tick, a single new tick can fire the
    day-over-day signal if there's a DailyClose from a prior day."""
    user_id, ticker_id = _setup_user_with_range_rule("AAPL", -1.0, 1.0)
    _seed_prior_day_close(ticker_id, close=100.0)

    sent = run_intraday_capture(fetcher=_staged_fetcher([110.0]), force=True)
    assert sent == 1
    assert len(client.sent_emails) == 1
    body = client.sent_emails[0]["body_text"]
    assert SIGNAL_DAILY in body
    assert "10.00%" in body  # +10% vs $100 prior close
    assert "previous trading day" in body


def test_big_move_sends_one_email_not_two(client):
    """Regression: the removed 10-minute signal compared the first tick of
    the day with yesterday's last one, so a big overnight move sent two
    emails. Now it's one, tagged [vs. prior day]."""
    _, ticker_id = _setup_user_with_range_rule("AAPL", -1.0, 1.0)
    _seed_prior_day_close(ticker_id, close=100.0)

    assert run_intraday_capture(fetcher=_staged_fetcher([100.0]), force=True) == 0
    assert run_intraday_capture(fetcher=_staged_fetcher([110.0]), force=True) == 1
    assert len(client.sent_emails) == 1
    body = client.sent_emails[0]["body_text"]
    assert SIGNAL_DAILY in body
    assert "10-min" not in body


def test_no_alert_without_previous_close(client):
    """Without a DailyClose (and with upstream suppressed in tests) there's
    nothing to compare against, so even a big intraday jump is silent."""
    _setup_user_with_range_rule("AAPL", -1.0, 1.0)
    run_intraday_capture(fetcher=_staged_fetcher([100.0]), force=True)
    run_intraday_capture(fetcher=_staged_fetcher([110.0]), force=True)
    assert client.sent_emails == []


def test_capture_disabled_by_settings(client, monkeypatch):
    """INTRADAY_INGEST_ENABLED=false makes the job a no-op (no force)."""
    monkeypatch.setenv("INTRADAY_INGEST_ENABLED", "false")
    from app.core.settings import get_settings
    get_settings.cache_clear()

    _setup_user_with_range_rule("AAPL", -1.0, 1.0)
    # No `force=True` → the disabled flag wins.
    assert run_intraday_capture(fetcher=_staged_fetcher([100.0])) == 0
    db = get_session_factory()()
    rows = db.execute(select(IntradayPrice)).scalars().all()
    db.close()
    assert rows == []
