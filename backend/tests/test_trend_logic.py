"""Pure-function tests for trend math + rule evaluation."""

from app.models import NotificationEventType, NotificationRule, PriceSnapshot
from app.services.trend import compute_trend, rule_triggers, summarize_event


def _snap(**kw):
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


def test_pct_change_positive_and_negative():
    up = compute_trend(_snap(previous_price=100.0, price=110.0))
    assert round(up.pct_change, 2) == 10.0
    down = compute_trend(_snap(previous_price=100.0, price=90.0))
    assert round(down.pct_change, 2) == -10.0


def test_period_low_high_flags_on_match():
    s = _snap(price=50.0, week_low=50.0, year_high=50.0)
    t = compute_trend(s)
    assert t.is_week_low and t.is_year_high
    assert not t.is_month_low


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


def test_weekly_high_rule_fires():
    s = _snap(price=120.0, week_high=120.0)
    t = compute_trend(s)
    rule = NotificationRule(
        user_id=1, ticker_id=1,
        event_type=NotificationEventType.WEEK_HIGH,
        enabled=True,
    )
    assert rule_triggers(rule, t) is True


def test_summary_strings_render():
    s = _snap(previous_price=100.0, price=120.0, week_high=120.0)
    t = compute_trend(s)
    assert "AAPL" in summarize_event("AAPL", NotificationEventType.WEEK_HIGH, s, t)
    assert "AAPL" in summarize_event(
        "AAPL", NotificationEventType.PRICE_CHANGE_RANGE, s, t
    )
