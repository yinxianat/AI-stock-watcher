"""Intraday price capture + PRICE_CHANGE_RANGE notification dispatch.

Scheduled every `INTRADAY_TICK_MINUTES` (default 10) during US market hours
on weekdays. For each watched ticker we:

1. Fetch the current price via the configured stock-data provider.
2. Insert an `IntradayPrice` row.
3. Compute TWO independent % signals:
   * `tick`  — vs. the prior intraday tick for this ticker (~10 min ago).
   * `daily` — vs. the previous trading day's close (from `daily_closes`,
               with an upstream fallback if our history is missing).
4. For every enabled `PRICE_CHANGE_RANGE` rule on this ticker whose user
   has confirmed their notify email, fire an alert if EITHER signal is
   outside the user's [pct_low, pct_high] band.

Dedup: each signal has its own 3-hour window — so users get one tick
alert AND one daily-baseline alert per (user, ticker), not just one or the
other. Implemented by tagging the NotificationLog summary with a stable
"[tick]" / "[daily]" prefix and matching the prefix in the dedup query.

Daily high/low rules (WEEK_LOW, YEAR_HIGH, etc.) are NOT handled here —
they fire from the daily compute/notify path, since they're keyed off
closing prices and don't move intraday.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy import and_, select
from sqlalchemy.orm import Session as DBSession

from app.core.settings import get_settings
from app.db.database import get_session_factory
from app.models import (
    DailyClose,
    IntradayPrice,
    NotificationEventType,
    NotificationLog,
    NotificationRule,
    Ticker,
    User,
    WatchlistItem,
    utcnow,
)
from app.services.alerts import alert_event_first_notification
from app.services.emailer import send_email

log = logging.getLogger(__name__)

# Per-signal dedup window. Each signal (tick / daily) has its own clock,
# so the user gets one tick alert AND one daily-baseline alert per window
# instead of one combined alert.
DEDUP_WINDOW = timedelta(hours=3)

# Stable, human-readable prefixes embedded at the start of each
# NotificationLog.summary so we can dedup by signal without a schema change.
# These show up at the start of every alert email body.
SIGNAL_TICK = "[10-min change]"
SIGNAL_DAILY = "[vs. prior day]"

# Single price-point fetcher: returns the current price for `symbol`, or None.
IntradayFetcher = Callable[[str], float | None]


def default_intraday_fetcher(symbol: str) -> float | None:  # pragma: no cover (network)
    """Latest available trade price via the configured provider."""
    from app.services.stock_data import get_current_price

    return get_current_price(symbol)


# ---------------------------------------------------------------------------
# Market-hours guard
# ---------------------------------------------------------------------------


def is_market_hours_now(now_utc: datetime | None = None) -> bool:
    """True iff `now_utc` (defaults to now) is inside the configured
    US/Eastern intraday window on a weekday.

    Uses a fixed UTC-5/UTC-4 offset rule via zoneinfo if available; falls
    back to a naive offset that's good enough for cron-bounded jobs.
    """
    if now_utc is None:
        now_utc = datetime.utcnow().replace(tzinfo=timezone.utc)
    elif now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)

    try:
        from zoneinfo import ZoneInfo

        et = now_utc.astimezone(ZoneInfo("America/New_York"))
    except Exception:
        # Fallback: subtract 5h. Wrong by 1h during DST — acceptable because
        # the scheduler also restricts to hours 9-16 ET in its cron.
        et = (now_utc - timedelta(hours=5)).replace(tzinfo=None)

    if et.weekday() >= 5:  # 5=Sat, 6=Sun
        return False

    settings = get_settings()
    open_h, open_m = settings.intraday_market_open
    close_h, close_m = settings.intraday_market_close

    minute_of_day = et.hour * 60 + et.minute
    return open_h * 60 + open_m <= minute_of_day <= close_h * 60 + close_m


# ---------------------------------------------------------------------------
# Capture + notify
# ---------------------------------------------------------------------------


def _prior_tick_price(db: DBSession, ticker_id: int, before: datetime) -> float | None:
    """Most recent IntradayPrice strictly before `before` for the ticker."""
    row = db.execute(
        select(IntradayPrice)
        .where(
            IntradayPrice.ticker_id == ticker_id,
            IntradayPrice.captured_at < before,
        )
        .order_by(IntradayPrice.captured_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    return row.price if row is not None else None


def _previous_day_close(db: DBSession, ticker: Ticker) -> float | None:
    """Most recent DailyClose strictly before today's date for `ticker`.

    Falls back to the upstream provider's previous-close if our history
    table has nothing recent — covers the case where a user just signed up
    and the daily ingest hasn't filled in yet. None on all failures.

    The upstream fallback is suppressed in the test environment so the
    pytest suite stays hermetic (no real network calls).
    """
    today = datetime.utcnow().date()
    row = db.execute(
        select(DailyClose)
        .where(DailyClose.ticker_id == ticker.id, DailyClose.date < today)
        .order_by(DailyClose.date.desc())
        .limit(1)
    ).scalar_one_or_none()
    if row is not None:
        return row.close

    if get_settings().app_env == "test":
        return None

    try:
        from app.services.stock_data import get_previous_close

        return get_previous_close(ticker.symbol)
    except Exception as e:  # noqa: BLE001
        log.warning("Previous-close fallback failed for %s: %s", ticker.symbol, e)
        return None


def _watched_tickers(db: DBSession) -> list[Ticker]:
    watched_ids = {
        tid for (tid,) in db.execute(
            select(WatchlistItem.ticker_id).distinct()
        ).all()
    }
    if not watched_ids:
        return []
    return db.execute(
        select(Ticker).where(Ticker.id.in_(watched_ids))
    ).scalars().all()


def _summary_text(
    ticker: Ticker, new_price: float, pct_change: float,
    signal: str, baseline_label: str,
) -> str:
    direction = "up" if pct_change >= 0 else "down"
    return (
        f"{signal} {ticker.symbol} moved {direction} {abs(pct_change):.2f}% "
        f"{baseline_label} to ${new_price:.2f}."
    )


def _already_fired_recently(
    db: DBSession, user_id: int, ticker_id: int,
    signal_prefix: str, captured_at: datetime,
) -> bool:
    """Per-signal dedup. Looks for a NotificationLog row for this
    (user, ticker, event_type) whose summary STARTS WITH `signal_prefix`
    within the dedup window."""
    row = db.execute(
        select(NotificationLog).where(
            and_(
                NotificationLog.user_id == user_id,
                NotificationLog.ticker_id == ticker_id,
                NotificationLog.event_type == NotificationEventType.PRICE_CHANGE_RANGE,
                NotificationLog.sent_at >= captured_at - DEDUP_WINDOW,
                NotificationLog.summary.like(f"{signal_prefix}%"),
            )
        )
    ).scalar_one_or_none()
    return row is not None


def _send_one_alert(
    db: DBSession, user: User, ticker: Ticker, summary: str, frontend_url: str,
) -> bool:
    """Send the email and write the NotificationLog row. True iff the
    SMTP call didn't raise (so we should record it)."""
    body = (
        f"{summary}\n\n"
        f"Manage your watchlist and rules: {frontend_url}\n"
        "You're receiving this because you set a price-change rule for this ticker."
    )
    try:
        send_email(user.notify_email, f"[AI Stock Watcher] {ticker.symbol}", body)
    except Exception as e:  # pragma: no cover
        log.warning("Intraday email send failed for user=%d: %s", user.id, e)
        return False
    db.add(
        NotificationLog(
            user_id=user.id, ticker_id=ticker.id,
            event_type=NotificationEventType.PRICE_CHANGE_RANGE,
            sent_to=user.notify_email, summary=summary,
        )
    )
    return True


def _dispatch_range_alerts(
    db: DBSession,
    ticker: Ticker,
    new_price: float,
    tick_pct: float | None,
    daily_pct: float | None,
    captured_at: datetime,
) -> tuple[int, list[tuple[User, str, str]]]:
    """Evaluate both signals against every PRICE_CHANGE_RANGE rule for
    `ticker`. Returns (#sent, [(user, event, summary) for first-ever notifications]).
    """
    rules = db.execute(
        select(NotificationRule).where(
            NotificationRule.ticker_id == ticker.id,
            NotificationRule.event_type == NotificationEventType.PRICE_CHANGE_RANGE,
            NotificationRule.enabled.is_(True),
        )
    ).scalars().all()
    if not rules:
        return 0, []

    user_ids = {r.user_id for r in rules}
    users = {
        u.id: u for u in db.execute(select(User).where(User.id.in_(user_ids))).scalars()
    }
    users_with_history: set[int] = {
        uid for (uid,) in db.execute(
            select(NotificationLog.user_id).distinct()
        ).all()
    }

    settings = get_settings()
    sent_count = 0
    first_events: list[tuple[User, str, str]] = []

    # Define the two independent signals to check.
    tick_label = f"over the last {settings.intraday_tick_minutes} minutes"
    daily_label = "vs. the previous trading day's close"
    signals: list[tuple[str, str, float | None]] = [
        (SIGNAL_TICK, tick_label, tick_pct),
        (SIGNAL_DAILY, daily_label, daily_pct),
    ]

    for rule in rules:
        user = users.get(rule.user_id)
        if user is None or not user.notify_email_confirmed:
            continue
        if rule.pct_low is None or rule.pct_high is None:
            continue
        for prefix, baseline, pct in signals:
            if pct is None:
                continue
            if not (pct < rule.pct_low or pct > rule.pct_high):
                continue
            if _already_fired_recently(db, user.id, ticker.id, prefix, captured_at):
                continue
            summary = _summary_text(ticker, new_price, pct, prefix, baseline)
            if not _send_one_alert(db, user, ticker, summary, settings.frontend_base_url):
                continue
            sent_count += 1
            if user.id not in users_with_history:
                users_with_history.add(user.id)
                first_events.append((
                    user, NotificationEventType.PRICE_CHANGE_RANGE.value, summary,
                ))

    return sent_count, first_events


def run_intraday_capture(
    db: DBSession | None = None,
    fetcher: IntradayFetcher = default_intraday_fetcher,
    force: bool = False,
) -> int:
    """Capture one tick per watched ticker and fire any PRICE_CHANGE_RANGE
    alerts that the new tick triggered. Returns # of emails sent.

    `force=True` skips the market-hours / enabled checks — useful for tests
    and manual runs.
    """
    settings = get_settings()
    if not force and not settings.intraday_ingest_enabled:
        log.info("Intraday capture skipped: INTRADAY_INGEST_ENABLED=false")
        return 0
    if not force and not is_market_hours_now():
        log.debug("Intraday capture skipped: outside market hours")
        return 0

    log.warning(
        "[AUDIT] Intraday capture starting (provider=%s, tick=%d min, force=%s)",
        settings.stock_data_provider, settings.intraday_tick_minutes, force,
    )

    own = db is None
    if own:
        db = get_session_factory()()
    try:
        tickers = _watched_tickers(db)
        if not tickers:
            log.warning("Intraday capture: no watched tickers — is anyone's watchlist populated?")
            return 0

        log.info("Intraday capture: %d watched tickers to fetch", len(tickers))
        now = utcnow()
        attempted = 0
        captured = 0
        failed_symbols: list[str] = []
        total_sent = 0
        first_events_all: list[tuple] = []
        for t in tickers:
            attempted += 1
            try:
                price = fetcher(t.symbol)
            except Exception as e:
                log.warning("Intraday fetch failed for %s: %s", t.symbol, e)
                failed_symbols.append(t.symbol)
                price = None
            if price is None or price <= 0:
                if price is None:
                    failed_symbols.append(t.symbol) if t.symbol not in failed_symbols else None
                    log.warning("Intraday fetch returned None for %s", t.symbol)
                elif price <= 0:
                    log.warning("Intraday fetch returned invalid price %.4f for %s", price, t.symbol)
                continue
            prior_tick = _prior_tick_price(db, t.id, before=now)
            prev_day_close = _previous_day_close(db, t)
            db.add(IntradayPrice(ticker_id=t.id, captured_at=now, price=price))
            db.flush()
            captured += 1

            tick_pct = (
                (price - prior_tick) / prior_tick * 100.0
                if prior_tick is not None and prior_tick > 0
                else None
            )
            daily_pct = (
                (price - prev_day_close) / prev_day_close * 100.0
                if prev_day_close is not None and prev_day_close > 0
                else None
            )
            if tick_pct is None and daily_pct is None:
                # No baseline at all on either signal — first observation,
                # nothing more to do for this ticker this tick.
                continue
            sent_n, first_events = _dispatch_range_alerts(
                db, t, new_price=price,
                tick_pct=tick_pct, daily_pct=daily_pct,
                captured_at=now,
            )
            total_sent += sent_n
            first_events_all.extend(first_events)

        db.commit()

        # Admin first-ever notification event — fire AFTER commit so SMTP
        # flakiness doesn't roll back the notification log.
        for user, evt, summary in first_events_all:
            alert_event_first_notification(
                user.notify_email,
                # Look up symbol via a fresh session is awkward here; the
                # caller already wrote the summary which includes the symbol.
                summary.split(" ", 1)[0],
                evt,
                summary,
            )

        if failed_symbols:
            log.warning(
                "Intraday capture: %d/%d tickers FAILED to fetch: %s",
                len(failed_symbols), attempted, ", ".join(failed_symbols),
            )
        log.warning(
            "[AUDIT] Intraday capture complete: captured=%d/%d, failed=%d, emails_sent=%d",
            captured, attempted, len(failed_symbols), total_sent,
        )
        return total_sent
    finally:
        if own:
            db.close()
