"""Trend analysis & notification decision logic.

Pure functions where possible — they take primitives, return primitives —
so they're trivially unit-testable without touching the DB or yfinance.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models import NotificationEventType, NotificationRule, PriceSnapshot


@dataclass
class TrendResult:
    """Output of `compute_trend` for one ticker."""

    pct_change: float
    is_week_low: bool
    is_week_high: bool
    is_month_low: bool
    is_month_high: bool
    is_quarter_low: bool
    is_quarter_high: bool
    is_year_low: bool
    is_year_high: bool


def _close(a: float | None, b: float | None, tol: float = 1e-6) -> bool:
    """True when both values exist and are within `tol` of each other."""
    return a is not None and b is not None and abs(a - b) <= tol


def compute_trend(snap: PriceSnapshot) -> TrendResult:
    """Derive percent change and period-low/high flags from a single snapshot row."""
    prev = snap.previous_price
    if prev is None or prev == 0:
        pct = 0.0
    else:
        pct = ((snap.price - prev) / prev) * 100.0

    return TrendResult(
        pct_change=pct,
        is_week_low=_close(snap.price, snap.week_low),
        is_week_high=_close(snap.price, snap.week_high),
        is_month_low=_close(snap.price, snap.month_low),
        is_month_high=_close(snap.price, snap.month_high),
        is_quarter_low=_close(snap.price, snap.quarter_low),
        is_quarter_high=_close(snap.price, snap.quarter_high),
        is_year_low=_close(snap.price, snap.year_low),
        is_year_high=_close(snap.price, snap.year_high),
    )


def rule_triggers(rule: NotificationRule, trend: TrendResult) -> bool:
    """Return True iff this rule should fire for this trend."""
    if not rule.enabled:
        return False

    e = rule.event_type
    if e == NotificationEventType.PRICE_CHANGE_RANGE:
        if rule.pct_low is None or rule.pct_high is None:
            return False
        # Fires when the move *exits* the [pct_low, pct_high] band.
        return trend.pct_change < rule.pct_low or trend.pct_change > rule.pct_high
    if e == NotificationEventType.WEEK_LOW:
        return trend.is_week_low
    if e == NotificationEventType.WEEK_HIGH:
        return trend.is_week_high
    if e == NotificationEventType.MONTH_LOW:
        return trend.is_month_low
    if e == NotificationEventType.MONTH_HIGH:
        return trend.is_month_high
    if e == NotificationEventType.QUARTER_LOW:
        return trend.is_quarter_low
    if e == NotificationEventType.QUARTER_HIGH:
        return trend.is_quarter_high
    if e == NotificationEventType.YEAR_LOW:
        return trend.is_year_low
    if e == NotificationEventType.YEAR_HIGH:
        return trend.is_year_high
    return False


def summarize_event(
    symbol: str, event: NotificationEventType, snap: PriceSnapshot, trend: TrendResult
) -> str:
    """One-line human summary of a triggered event."""
    if event == NotificationEventType.PRICE_CHANGE_RANGE:
        direction = "up" if trend.pct_change >= 0 else "down"
        return (
            f"{symbol} moved {direction} {abs(trend.pct_change):.2f}% to ${snap.price:.2f}."
        )
    period_and_kind = {
        NotificationEventType.WEEK_LOW: "weekly low",
        NotificationEventType.WEEK_HIGH: "weekly high",
        NotificationEventType.MONTH_LOW: "monthly low",
        NotificationEventType.MONTH_HIGH: "monthly high",
        NotificationEventType.QUARTER_LOW: "quarterly low",
        NotificationEventType.QUARTER_HIGH: "quarterly high",
        NotificationEventType.YEAR_LOW: "yearly low",
        NotificationEventType.YEAR_HIGH: "yearly high",
    }[event]
    return f"{symbol} hit a new {period_and_kind} at ${snap.price:.2f}."
