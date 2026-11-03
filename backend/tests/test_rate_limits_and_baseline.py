"""Provider rate limiting, and the freshness of the alert baseline.

Two separate bugs live here. The first is impatience: both free tiers are
per-minute, and the jobs used to fire one request per ticker as fast as
httpx would go, so a watchlist bigger than the ceiling burst past it and
the overflow came back 429. The second is worse, because it was silent:
`_previous_day_close` returned the newest stored close of ANY age, so a
run on a day when ingest had been failing compared today's price against
a close from the previous week and still labelled it "vs. the previous
trading day's close".
"""

from __future__ import annotations

import time
from datetime import date, timedelta

from app.core.settings import get_settings
from app.db.database import get_session_factory
from app.jobs.intraday import _previous_day_close
from app.models import DailyClose, Ticker, TickerType
from app.services import stock_data


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


def test_calls_under_the_limit_are_not_delayed():
    stock_data.reset_rate_limits_for_tests()
    t0 = time.monotonic()
    for _ in range(10):
        assert stock_data._rate_limit("p", 10) is True
    assert time.monotonic() - t0 < 0.5


def test_the_limit_is_per_provider_not_global():
    """Finnhub's 50 and Twelve Data's 8 must not consume each other."""
    stock_data.reset_rate_limits_for_tests()
    for _ in range(8):
        assert stock_data._rate_limit("twelvedata", 8) is True
    # Twelve Data is now full for the minute, but Finnhub is untouched.
    t0 = time.monotonic()
    assert stock_data._rate_limit("finnhub", 50) is True
    assert time.monotonic() - t0 < 0.5


def test_a_caller_that_would_wait_too_long_is_told_to_skip(monkeypatch):
    """Better to drop one ticker than hold the whole run open. With the
    window full and a 1/min limit, the wait is ~60s, over the ceiling."""
    stock_data.reset_rate_limits_for_tests()
    monkeypatch.setattr(stock_data, "_MAX_RATE_WAIT", 0.2)
    assert stock_data._rate_limit("p", 1) is True
    assert stock_data._rate_limit("p", 1) is False


def test_a_zero_limit_disables_throttling():
    stock_data.reset_rate_limits_for_tests()
    for _ in range(100):
        assert stock_data._rate_limit("p", 0) is True


def test_the_window_slides_so_old_calls_stop_counting(monkeypatch):
    """A call from over a minute ago must not occupy a slot."""
    stock_data.reset_rate_limits_for_tests()
    assert stock_data._rate_limit("p", 1) is True
    # Rewind the recorded call past the 60s window.
    with stock_data._rate_lock:
        stock_data._call_times["p"][0] -= 61.0
    t0 = time.monotonic()
    assert stock_data._rate_limit("p", 1) is True
    assert time.monotonic() - t0 < 0.5


def test_defaults_sit_under_each_published_ceiling():
    """50 < 60 for Finnhub, and 8 is Twelve Data's own ceiling."""
    s = get_settings()
    assert s.finnhub_max_per_min == 50
    assert s.twelvedata_max_per_min == 8


# ---------------------------------------------------------------------------
# Baseline freshness
# ---------------------------------------------------------------------------


def _ticker_with_close(symbol: str, days_ago: int, close: float) -> Ticker:
    db = get_session_factory()()
    t = Ticker(symbol=symbol, name=symbol, type=TickerType.STOCK)
    db.add(t)
    db.flush()
    db.add(DailyClose(
        ticker_id=t.id, date=date.today() - timedelta(days=days_ago), close=close,
    ))
    db.commit()
    db.refresh(t)
    db.close()
    return t


def test_yesterdays_close_is_used(client):
    t = _ticker_with_close("FRESH", days_ago=1, close=100.0)
    db = get_session_factory()()
    assert _previous_day_close(db, t) == 100.0
    db.close()


def test_a_close_from_across_a_weekend_is_still_used(client):
    """Friday's close on a Monday is 3 days old and perfectly valid."""
    t = _ticker_with_close("WKND", days_ago=3, close=100.0)
    db = get_session_factory()()
    assert _previous_day_close(db, t) == 100.0
    db.close()


def test_a_stale_close_is_refused_rather_than_quoted(client):
    """The MSFT case: ingest last succeeded 5 days ago. Using that close
    produced "+0.52%" on a day every public quote said +2%, because the
    baseline was the wrong day. No number beats a wrong number.

    (In the test environment the provider fallback is suppressed, so this
    returns None; in production it asks the provider instead.)
    """
    t = _ticker_with_close("STALE", days_ago=5, close=100.0)
    db = get_session_factory()()
    assert _previous_day_close(db, t) is None
    db.close()


def test_the_staleness_cutoff_is_configurable(client, monkeypatch):
    t = _ticker_with_close("CONFIG", days_ago=5, close=100.0)
    monkeypatch.setenv("PREV_CLOSE_MAX_AGE_DAYS", "10")
    get_settings.cache_clear()
    try:
        db = get_session_factory()()
        assert _previous_day_close(db, t) == 100.0
        db.close()
    finally:
        get_settings.cache_clear()


def test_a_stale_baseline_is_logged_so_it_can_be_diagnosed(client, caplog):
    """It must not be possible for this to happen quietly — a silently
    wrong percentage is the whole bug."""
    import logging
    t = _ticker_with_close("LOUD", days_ago=9, close=100.0)
    db = get_session_factory()()
    with caplog.at_level(logging.WARNING):
        _previous_day_close(db, t)
    db.close()
    msg = "\n".join(r.getMessage() for r in caplog.records)
    assert "LOUD" in msg
    assert "9 days" in msg


def test_no_close_at_all_is_not_treated_as_stale(client):
    """A brand-new ticker has no row; that is the signup case, not rot."""
    db = get_session_factory()()
    t = Ticker(symbol="BRANDNEW", name="BrandNew", type=TickerType.STOCK)
    db.add(t)
    db.commit()
    db.refresh(t)
    assert _previous_day_close(db, t) is None
    db.close()
