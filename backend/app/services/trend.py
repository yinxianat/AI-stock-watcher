"""Trend analysis & notification decision logic.

Pure functions where possible — they take primitives, return primitives —
so they're trivially unit-testable without touching the DB or yfinance.

Option-B semantics (see app/models/models.py docstrings for full context):

* `snap.previous_price` is the prior trading day's close (from `daily_closes`),
  so `pct_change` is meaningful day-over-day, not "since the last 3-hour
  batch tick".
* `snap.week_low` / `week_high` / etc. are the min/max close in the prior
  window EXCLUDING today. `is_week_low` therefore means "today's close is
  STRICTLY lower than every close in the previous 7 days" — a genuine new
  low, not a tie.
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


def _strictly_below(price: float | None, prior_min: float | None) -> bool:
    """True iff today's price is strictly below the prior-window minimum.

    If `prior_min` is None (no history in that window yet), we can't claim a
    new low — return False rather than firing on the first-ever data point.
    """
    return price is not None and prior_min is not None and price < prior_min


def _strictly_above(price: float | None, prior_max: float | None) -> bool:
    """True iff today's price is strictly above the prior-window maximum."""
    return price is not None and prior_max is not None and price > prior_max


def compute_trend(snap: PriceSnapshot) -> TrendResult:
    """Derive percent change and strict new-low / new-high flags."""
    prev = snap.previous_price
    if prev is None or prev == 0:
        pct = 0.0
    else:
        pct = ((snap.price - prev) / prev) * 100.0

    return TrendResult(
        pct_change=pct,
        is_week_low=_strictly_below(snap.price, snap.week_low),
        is_week_high=_strictly_above(snap.price, snap.week_high),
        is_month_low=_strictly_below(snap.price, snap.month_low),
        is_month_high=_strictly_above(snap.price, snap.month_high),
        is_quarter_low=_strictly_below(snap.price, snap.quarter_low),
        is_quarter_high=_strictly_above(snap.price, snap.quarter_high),
        is_year_low=_strictly_below(snap.price, snap.year_low),
        is_year_high=_strictly_above(snap.price, snap.year_high),
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
        prev = snap.previous_price
        ref = f" from ${prev:.2f}" if prev is not None else ""
        return (
            f"{symbol} moved {direction} {abs(trend.pct_change):.2f}% to "
            f"${snap.price:.2f}{ref} (vs. prior trading day close)."
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
