"""Pure-function tests for trend math + rule evaluation.

Under the Option-B redesign, `is_X_low/high` flags mean "today's price is
STRICTLY beyond the prior-window extreme" — `_snap(price=50, week_low=51)`
fires `is_week_low=True`, but `_snap(price=50, week_low=50)` does not.
"""

from app.models import NotificationEventType, NotificationRule, PriceSnapshot
from app.services.trend import compute_trend, rule_triggers, summarize_event


def _snap(**kw):
    # Defaults: today equals prior min and prior max → no new extreme.
    base = dict(
        ticker_id=1, price=100.0, previous_price=100.0,
        week_low=100.0, week_high=100.0,
        month_low=100.0, month_high=100.0,
        quarter_low=100.0, quarter_high=100.0,
        year_low=100.0, year_high=100.0,
    )
    base.update(kw)
    return PriceSnapshot(**base)


def test_pct_change_handles_zero_previous():
    s = _snap(previous_price=0)
    t = compute_trend(s)
    assert t.pct_change == 0.0


def test_pct_change_handles_none_previous():
    s = _snap(previous_price=None)
    t = compute_trend(s)
    assert t.pct_change == 0.0


def test_pct_change_positive_and_negative():
    up = compute_trend(_snap(previous_price=100.0, price=110.0))
    assert round(up.pct_change, 2) == 10.0
    down = compute_trend(_snap(previous_price=100.0, price=90.0))
    assert round(down.pct_change, 2) == -10.0


def test_new_low_fires_when_strictly_below_prior_min():
    s = _snap(price=49.0, week_low=50.0, month_low=50.0, year_low=50.0)
    t = compute_trend(s)
    assert t.is_week_low is True
    assert t.is_month_low is True
    assert t.is_year_low is True


def test_tie_with_prior_min_does_not_fire():
    """Today's price equal to the prior min is NOT a new low (strict)."""
    s = _snap(price=50.0, week_low=50.0)
    t = compute_trend(s)
    assert t.is_week_low is False


def test_new_high_fires_when_strictly_above_prior_max():
    s = _snap(price=120.0, week_high=119.0, year_high=119.0)
    t = compute_trend(s)
    assert t.is_week_high is True
    assert t.is_year_high is True


def test_missing_prior_window_never_fires():
    """First-ever data point has no prior min/max — can't be a 'new' low."""
    s = _snap(price=50.0, week_low=None, week_high=None,
              month_low=None, month_high=None,
              quarter_low=None, quarter_high=None,
              year_low=None, year_high=None)
    t = compute_trend(s)
    assert not any([
        t.is_week_low, t.is_week_high,
        t.is_month_low, t.is_month_high,
        t.is_quarter_low, t.is_quarter_high,
        t.is_year_low, t.is_year_high,
    ])


def test_rule_disabled_never_fires():
    s = _snap(previous_price=100.0, price=200.0)
    t = compute_trend(s)
    rule = NotificationRule(
        user_id=1, ticker_id=1,
        event_type=NotificationEventType.PRICE_CHANGE_RANGE,
        pct_low=-1, pct_high=1, enabled=False,
    )
    assert rule_triggers(rule, t) is False


def test_range_rule_fires_outside_band():
    t = compute_trend(_snap(previous_price=100.0, price=106.0))  # +6%
    rule = NotificationRule(
        user_id=1, ticker_id=1,
        event_type=NotificationEventType.PRICE_CHANGE_RANGE,
        pct_low=-5, pct_high=5, enabled=True,
    )
    assert rule_triggers(rule, t) is True


def test_range_rule_silent_inside_band():
    t = compute_trend(_snap(previous_price=100.0, price=103.0))  # +3%
    rule = NotificationRule(
        user_id=1, ticker_id=1,
        event_type=NotificationEventType.PRICE_CHANGE_RANGE,
        pct_low=-5, pct_high=5, enabled=True,
    )
    assert rule_triggers(rule, t) is False


def test_weekly_high_rule_fires_on_strict_new_high():
    s = _snap(price=120.0, week_high=119.99)
    t = compute_trend(s)
    rule = NotificationRule(
        user_id=1, ticker_id=1,
        event_type=NotificationEventType.WEEK_HIGH,
        enabled=True,
    )
    assert rule_triggers(rule, t) is True


def test_summary_strings_render():
    s = _snap(previous_price=100.0, price=120.0, week_high=119.0)
    t = compute_trend(s)
    assert "AAPL" in summarize_event("AAPL", NotificationEventType.WEEK_HIGH, s, t)
    assert "AAPL" in summarize_event(
        "AAPL", NotificationEventType.PRICE_CHANGE_RANGE, s, t
    )
