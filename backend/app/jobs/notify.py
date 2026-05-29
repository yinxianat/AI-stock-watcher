"""Job 3 — Dispatch notification emails based on rules + trends.

Walks every enabled NotificationRule, looks at the matching TrendAnalysis,
and emails the user if the rule fires. Each send is logged to
NotificationLog so we don't double-send within the same batch window.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import and_, select
from sqlalchemy.orm import Session as DBSession

from app.core.settings import get_settings
from app.db.database import get_session_factory
from app.models import (
    NotificationLog,
    NotificationRule,
    PriceSnapshot,
    Ticker,
    TrendAnalysis,
    User,
    utcnow,
)
from app.services.emailer import send_email
from app.services.trend import TrendResult, rule_triggers, summarize_event

log = logging.getLogger(__name__)

# Suppress duplicate sends within this window (covers same batch run + retries).
DEDUP_WINDOW = timedelta(hours=3)


def _trend_to_dataclass(row: TrendAnalysis) -> TrendResult:
    return TrendResult(
        pct_change=row.pct_change,
        is_week_low=row.is_week_low,
        is_week_high=row.is_week_high,
        is_month_low=row.is_month_low,
        is_month_high=row.is_month_high,
        is_quarter_low=row.is_quarter_low,
        is_quarter_high=row.is_quarter_high,
        is_year_low=row.is_year_low,
        is_year_high=row.is_year_high,
    )


def run_notify(db: DBSession | None = None) -> int:
    """Send notification emails for all firing rules. Returns # of emails sent."""
    own = db is None
    if own:
        db = get_session_factory()()
    try:
        settings = get_settings()
        users = {u.id: u for u in db.execute(select(User)).scalars().all()}
        snaps = {
            s.ticker_id: s
            for s in db.execute(select(PriceSnapshot)).scalars().all()
        }
        trends = {
            (t.user_id, t.ticker_id): t
            for t in db.execute(select(TrendAnalysis)).scalars().all()
        }
        tickers = {t.id: t for t in db.execute(select(Ticker)).scalars().all()}

        sent = 0
        for rule in db.execute(select(NotificationRule)).scalars().all():
            if not rule.enabled:
                continue
            user = users.get(rule.user_id)
            if user is None or not user.notify_email_confirmed:
                continue
            trend_row = trends.get((rule.user_id, rule.ticker_id))
            snap = snaps.get(rule.ticker_id)
            if trend_row is None or snap is None:
                continue
            ticker = tickers.get(rule.ticker_id)
            if ticker is None:
                continue

            if not rule_triggers(rule, _trend_to_dataclass(trend_row)):
                continue

            # Dedup: same (user, ticker, event_type) sent recently?
            recent = db.execute(
                select(NotificationLog).where(
                    and_(
                        NotificationLog.user_id == user.id,
                        NotificationLog.ticker_id == ticker.id,
                        NotificationLog.event_type == rule.event_type,
                        NotificationLog.sent_at >= utcnow() - DEDUP_WINDOW,
                    )
                )
            ).scalar_one_or_none()
            if recent is not None:
                continue

            summary = summarize_event(
                ticker.symbol, rule.event_type, snap, _trend_to_dataclass(trend_row)
            )
            body = (
                f"{summary}\n\n"
                f"Manage your watchlist and rules: {settings.frontend_base_url}\n"
                "You're receiving this because you set a notification rule for this ticker."
            )
            try:
                send_email(user.notify_email, f"[AI Stock Watcher] {ticker.symbol}", body)
            except Exception as e:  # pragma: no cover
                log.warning("Email send failed for user=%d: %s", user.id, e)
                continue

            db.add(
                NotificationLog(
                    user_id=user.id,
                    ticker_id=ticker.id,
                    event_type=rule.event_type,
                    sent_to=user.notify_email,
                    summary=summary,
                )
            )
            sent += 1

        db.commit()
        log.info("Notify complete: %d emails sent", sent)
        return sent
    finally:
        if own:
            db.close()
