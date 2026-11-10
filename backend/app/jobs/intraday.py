"""Intraday price capture + PRICE_CHANGE_RANGE notification dispatch.

Scheduled every `INTRADAY_TICK_MINUTES` (default 30) during US market hours
on weekdays. For each watched ticker we:

1. Fetch the current price via the configured stock-data provider.
2. Insert an `IntradayPrice` row.
3. Compute the % change vs. the previous trading day's close (from
   `daily_closes`, with an upstream fallback if our history is missing).
4. For every enabled `PRICE_CHANGE_RANGE` rule on this ticker whose user
   has confirmed their notify email, fire an alert if that change is
   outside the user's [pct_low, pct_high] band.

(There used to be a second, 10-minute tick-over-tick signal. It was
removed: the first tick after the open compared against yesterday's last
tick, so it mostly duplicated the day-over-day alert.)

Stories: when AI_ARTICLES_ENABLED, alerts aren't sent straight away. All
alerts for the run are collected first; stocks that moved at least the
story bar (AI_ARTICLE_MIN_PCT_STOCK / _FUND) get an AI "why it moved" story
(see app.services.articles), given up to AI_ARTICLE_WAIT_SECONDS; then
every alert is sent, with its story if ready, or a "still looking" note
and a threaded follow-up later. Smaller moves get a fixed "small move"
note instead.

Dedup: one alert per (user, ticker) per 3-hour window, matched on the
"[vs. prior day]" prefix of the NotificationLog summary.

Daily high/low rules (WEEK_LOW, YEAR_HIGH, etc.) are NOT handled here —
they fire from the daily compute/notify path, since they're keyed off
closing prices and don't move intraday.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
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
    StockArticle,
    NotificationLog,
    NotificationRule,
    Ticker,
    User,
    WatchlistItem,
    utcnow,
)
from app.api.feedback_routes import feedback_links_html, feedback_links_text
from app.services import delivery
from app.services.alerts import alert_event_first_notification
from app.services.articles import service as articles
from app.services.articles.preferences import get_age_groups
from app.services.emailer import EmailSendError, send_email
from app.services.email_prefs import alert_email_headers, opted_out_user_ids

log = logging.getLogger(__name__)

DEDUP_WINDOW = timedelta(hours=3)

# Stable prefix at the start of each NotificationLog.summary (and alert
# email body); the dedup query matches on it.
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
        now_utc = utcnow()
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


def _previous_day_close(db: DBSession, ticker: Ticker) -> float | None:
    """The previous trading day's close for `ticker`, or None.

    Only a close we can still believe is the PREVIOUS trading day's counts.
    This used to return the most recent stored row of any age, which is how
    an alert came to say "+0.52% vs. the previous trading day's close" on a
    day the stock was up 2%: ingest had last succeeded four days earlier, so
    the baseline was the Friday close while the move everyone else quoted
    was against Monday's. The arithmetic was right and the label was false.

    So a stored row is used only while it is within
    PREV_CLOSE_MAX_AGE_DAYS; beyond that we ask the provider, whose
    previous-close field is the same one public quotes compare against.

    The upstream fallback is suppressed in the test environment so the
    pytest suite stays hermetic (no real network calls).
    """
    today = utcnow().date()
    row = db.execute(
        select(DailyClose)
        .where(DailyClose.ticker_id == ticker.id, DailyClose.date < today)
        .order_by(DailyClose.date.desc())
        .limit(1)
    ).scalar_one_or_none()
    max_age = get_settings().prev_close_max_age_days
    if row is not None:
        age = (today - row.date).days
        if age <= max_age:
            return row.close
        # Stale. Say so loudly: every alert built on this baseline would
        # have quoted a percentage nobody else's would agree with.
        log.warning(
            "Stale previous close for %s: newest daily_closes row is %s (%d days "
            "old, max %d) — asking the provider instead so the move matches a "
            "public quote. Has the daily ingest been failing?",
            ticker.symbol, row.date, age, max_age,
        )

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
    """Dedup. Looks for a NotificationLog row for this
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


@dataclass
class _Alert:
    """A rule that fired this run. Sent after stories have had their chance."""

    user: User
    ticker: Ticker
    pct: float
    price: float
    prev_close: float | None
    summary: str


def _send_one_alert(
    db: DBSession, alert: _Alert, story: StockArticle | None,
    skipped: delivery.StoryOutcome | None = None,
) -> bool:
    """Send the email and write the NotificationLog row. True iff the
    SMTP call didn't raise (so we should record it).

    `skipped` is why there is no story, when `_stories_for` already knows.
    It ends up in `delivery_outcomes` so a bare alert is never unexplained."""
    user, ticker = alert.user, alert.ticker
    subject = f"[🐵 Little Money Monkey] {ticker.symbol}"
    small_move = None
    if get_settings().ai_articles_enabled and not articles.needs_story(ticker, alert.pct):
        small_move = (ticker, alert.pct)
    body, body_html, story_pending, outcome = articles.build_alert_email(
        db, user, alert.summary, story, small_move=small_move, skipped=skipped,
    )
    if story is not None:
        ages = get_age_groups(db, user.id)
        art_ctx = {
            "ticker": ticker.symbol, "move_pct": round(alert.pct, 2),
            "direction": "up" if alert.pct >= 0 else "down",
            "trading_date": story.trading_date.isoformat(),
        }
        fb_text = feedback_links_text(
            user.id, "article", str(story.id),
            age_group=ages[0] if ages else None, context=art_ctx,
        )
        fb_html = feedback_links_html(
            user.id, "article", str(story.id),
            age_group=ages[0] if ages else None, context=art_ctx,
        )
        body = body.replace("\n━━━━━━━━━━━━━━━━━━━━\n", f"\n{fb_text}\n━━━━━━━━━━━━━━━━━━━━\n", 1)
        if body_html:
            body_html = body_html.replace("</div>\n</div>", f"{fb_html}</div>\n</div>", 1)
    headers = alert_email_headers(user)
    message_id = None
    if story is not None:
        # Lets a follow-up story show up in the same thread as this alert.
        message_id = articles.new_message_id()
        headers = {**headers, "Message-ID": message_id}
    try:
        send_email(user.notify_email, subject, body, body_html=body_html, headers=headers)
    except EmailSendError as e:
        log.warning("Intraday email send failed for user=%d: %s", user.id, e)
        delivery.record(
            db, user=user, ticker=ticker, channel=delivery.CHANNEL_ALERT,
            trading_date=articles.trading_date(),
            outcome=delivery.StoryOutcome(
                state=delivery.STATE_NONE, reason="send_failed",
                # EmailSendError already carries the SMTP exception's name.
                detail=f"The alert for {ticker.symbol} was built but SMTP refused it: {e}",
            ),
        )
        return False
    db.add(
        NotificationLog(
            user_id=user.id, ticker_id=ticker.id,
            event_type=NotificationEventType.PRICE_CHANGE_RANGE,
            sent_to=user.notify_email, summary=alert.summary,
        )
    )
    if story_pending and message_id is not None:
        articles.add_followup(db, story, user, message_id, subject)
    delivery.record(
        db, user=user, ticker=ticker, channel=delivery.CHANNEL_ALERT,
        trading_date=articles.trading_date(), outcome=outcome,
    )
    return True


def _detect_range_alerts(
    db: DBSession,
    ticker: Ticker,
    new_price: float,
    daily_pct: float,
    prev_day_close: float | None,
    captured_at: datetime,
) -> list[_Alert]:
    """Evaluate the day-over-day change against every PRICE_CHANGE_RANGE
    rule for `ticker`. Returns the alerts to send; sends nothing.
    """
    rules = db.execute(
        select(NotificationRule).where(
            NotificationRule.ticker_id == ticker.id,
            NotificationRule.event_type == NotificationEventType.PRICE_CHANGE_RANGE,
            NotificationRule.enabled.is_(True),
        )
    ).scalars().all()
    if not rules:
        return []

    user_ids = {r.user_id for r in rules}
    users = {
        u.id: u for u in db.execute(select(User).where(User.id.in_(user_ids))).scalars()
    }
    unsubscribed = opted_out_user_ids(db)

    alerts: list[_Alert] = []
    for rule in rules:
        user = users.get(rule.user_id)
        if user is None or not user.notify_email_confirmed:
            continue
        if user.id in unsubscribed:
            continue
        if rule.pct_low is None or rule.pct_high is None:
            continue
        if not (daily_pct < rule.pct_low or daily_pct > rule.pct_high):
            continue
        if _already_fired_recently(db, user.id, ticker.id, SIGNAL_DAILY, captured_at):
            continue
        alerts.append(_Alert(
            user=user, ticker=ticker, pct=daily_pct, price=new_price,
            prev_close=prev_day_close,
            summary=_summary_text(
                ticker, new_price, daily_pct, SIGNAL_DAILY,
                "vs. the previous trading day's close",
            ),
        ))

    return alerts


def _stories_for(
    db: DBSession, alerts: list[_Alert],
) -> tuple[dict[int, StockArticle], dict[int, delivery.StoryOutcome]]:
    """Stories (by ticker id) for stocks that moved at least the story bar,
    after waiting up to AI_ARTICLE_WAIT_SECONDS, plus the reason for each
    ticker that got none. Alerts go out either way — a story problem must
    never hold up a price alert — so the reasons are the only record of why
    an alert was bare."""
    settings = get_settings()
    if not settings.ai_articles_enabled:
        return {}, {
            a.ticker.id: delivery.StoryOutcome(
                state=delivery.STATE_NONE, reason="ai_disabled",
                detail=f"AI_ARTICLES_ENABLED is false, so {a.ticker.symbol} "
                       f"({a.pct:+.2f}%) got a price-only alert.",
            )
            for a in alerts
        }
    candidates: dict[int, articles.Candidate] = {}
    skipped: dict[int, delivery.StoryOutcome] = {}
    for a in alerts:
        if not articles.needs_story(a.ticker, a.pct):
            continue  # the small-move note covers these; see `_send_one_alert`
        if not a.prev_close:
            skipped[a.ticker.id] = delivery.StoryOutcome(
                state=delivery.STATE_NONE, reason="no_prev_close",
                detail=f"{a.ticker.symbol} has no previous trading day's close, which the "
                       "story step needs to describe the move.",
            )
            continue
        candidates.setdefault(
            a.ticker.id, articles.Candidate(a.ticker, a.pct, a.price, a.prev_close),
        )
    if not candidates:
        return {}, skipped
    try:
        plan = articles.ensure_articles(
            db, list(candidates.values()), settings.ai_article_wait_seconds,
        )
        articles.wait_for(db, list(plan.stories.values()), settings.ai_article_wait_seconds)
        return plan.stories, {**skipped, **plan.skipped}
    except Exception as e:  # noqa: BLE001
        log.warning("Stories unavailable this run: %s", e)
        db.rollback()
        etype = type(e).__name__
        for tid in candidates:
            sym = candidates[tid].ticker.symbol
            skipped[tid] = delivery.StoryOutcome(
                state=delivery.STATE_NONE, reason="generation_error",
                detail=(
                    f"The story step for {sym} raised {etype} before any story "
                    f"could be attached: {e}"
                ),
            )
        return {}, skipped


def _send_alerts(
    db: DBSession, alerts: list[_Alert], stories: dict[int, StockArticle],
    skipped: dict[int, delivery.StoryOutcome] | None = None,
) -> tuple[int, list[tuple[User, str, str, str]]]:
    """Returns (#sent, [(user, symbol, event, summary) for first-ever notifications])."""
    skipped = skipped or {}
    users_with_history: set[int] = {
        uid for (uid,) in db.execute(select(NotificationLog.user_id).distinct()).all()
    }
    sent = 0
    first_events: list[tuple[User, str, str, str]] = []
    for a in alerts:
        if not _send_one_alert(db, a, stories.get(a.ticker.id), skipped.get(a.ticker.id)):
            continue
        sent += 1
        if a.user.id not in users_with_history:
            users_with_history.add(a.user.id)
            first_events.append((
                a.user, a.ticker.symbol, NotificationEventType.PRICE_CHANGE_RANGE.value, a.summary,
            ))
    return sent, first_events


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

    log.info(
        "Intraday capture starting (provider=%s, tick=%d min, force=%s)",
        settings.stock_data_provider, settings.intraday_tick_minutes, force,
    )
    started = utcnow()
    t0 = time.monotonic()

    own = db is None
    if own:
        db = get_session_factory()()
    try:
        articles.sweep(db)
        tickers = _watched_tickers(db)
        if not tickers:
            log.warning("Intraday capture: no watched tickers — is anyone's watchlist populated?")
            return 0

        log.info("Intraday capture: %d watched tickers to fetch: %s",
                 len(tickers), ", ".join(t.symbol for t in tickers))
        now = utcnow()
        attempted = 0
        captured = 0
        failed_symbols: list[str] = []
        captured_prices: list[str] = []  # "AAPL=$192.34" for the audit log
        no_baseline: list[str] = []  # priced, but no trustworthy previous close
        alerts: list[_Alert] = []
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
            prev_day_close = _previous_day_close(db, t)
            db.add(IntradayPrice(ticker_id=t.id, captured_at=now, price=price))
            db.flush()
            captured += 1
            captured_prices.append(f"{t.symbol}=${price:.2f}")

            if prev_day_close is None or prev_day_close <= 0:
                # No baseline we trust, so no rule can be evaluated for this
                # ticker this tick. Deliberately not silent: this is the one
                # path that drops a ticker without any delivery_outcomes row
                # to explain it, because no alert was ever built.
                no_baseline.append(t.symbol)
                log.warning(
                    "No usable previous close for %s: skipping its rules this tick "
                    "rather than comparing against a stale or missing baseline",
                    t.symbol,
                )
                continue
            daily_pct = (price - prev_day_close) / prev_day_close * 100.0
            alerts.extend(_detect_range_alerts(
                db, t, new_price=price, daily_pct=daily_pct,
                prev_day_close=prev_day_close, captured_at=now,
            ))

        stories, story_skips = _stories_for(db, alerts)
        total_sent, first_events_all = _send_alerts(db, alerts, stories, story_skips)
        db.commit()
        # A story that finished just after the wait found no follow-ups to
        # send (they were committed above), so send them now.
        for story in stories.values():
            articles.send_waiting_followups(db, story)

        # Admin first-ever notification event — fire AFTER commit so SMTP
        # flakiness doesn't roll back the notification log.
        for user, symbol, evt, summary in first_events_all:
            alert_event_first_notification(user.notify_email, symbol, evt, summary)

        if failed_symbols:
            log.warning(
                "Intraday capture: %d/%d tickers FAILED to fetch: %s",
                len(failed_symbols), attempted, ", ".join(failed_symbols),
            )
        tables = []
        if captured:
            tables.append("intraday_prices")
        if total_sent:
            tables.append("notification_logs")
        elapsed = time.monotonic() - t0
        summary = (
            f"captured={captured}/{attempted}, failed={len(failed_symbols)}, "
            f"emails_sent={total_sent}, prices=[{', '.join(captured_prices) or 'none'}]"
        )
        if failed_symbols:
            summary += f", failed_symbols=[{', '.join(failed_symbols)}]"
        if no_baseline:
            summary += f", no_baseline=[{', '.join(no_baseline)}]"
        log.info("Intraday capture complete (%.2fs): %s", elapsed, summary)

        from app.jobs.audit import record_job_run

        record_job_run(
            "intraday_capture",
            "FAILED" if captured == 0 and attempted > 0 else "SUCCESS",
            started, elapsed,
            result_summary=summary,
            tables_updated=tables or None,
        )
        return total_sent
    finally:
        if own:
            db.close()
