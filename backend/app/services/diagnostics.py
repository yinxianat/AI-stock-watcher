"""One place that answers "what happened in production, and why".

Three consumers read this module and nothing else:

* `app.api.admin_routes` — JSON for the /admin dashboard and the scheduled
  review agent.
* `app.jobs.daily_summary` — the nightly digest email.
* ad-hoc debugging in a shell.

Keeping every query here is the point: a metric added once shows up in the
dashboard, the email and the agent together, and they can never disagree
about what a number means.

Timezone note, because it bites every query in here: all stored datetimes
are naive UTC (`app.models.utcnow`), while `trading_date` / `date` columns
hold the US/Eastern calendar date the market ran on. `day_bounds()` turns an
Eastern date into the UTC window to filter timestamps by; never compare a
timestamp column against a trading date directly.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import date as date_
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DBSession

from app.core.settings import get_settings
from app.db.database import get_session_factory
from app.models import (
    ArticleFollowup,
    DailyClose,
    EmailOptOut,
    InfoLog,
    IntradayPrice,
    JobRun,
    LogEntry,
    MoneyIdeaSend,
    NotificationLog,
    RecapOptOut,
    RecapSend,
    StockArticle,
    Ticker,
    User,
    WatchlistItem,
)

log = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")
_UTC = ZoneInfo("UTC")

# Why a user saw what they saw. Mirrors the decision tree in
# `app.services.articles.service.kid_block` plus the gates in
# `ensure_articles`. Kept here as the human-readable catalogue so the
# dashboard and the agent explain outcomes the same way.
# Who is asking, which decides whether users are named.
#   SCOPE_ADMIN   — a signed-in admin; real email addresses.
#   SCOPE_SERVICE — the monitoring agent; stable pseudonyms only, so no
#                   address reaches an agent transcript stored outside our
#                   infrastructure. See `user_ref`.
SCOPE_ADMIN = "admin"
SCOPE_SERVICE = "service"

# How far back to look when counting a run of missed recaps.
_MISS_STREAK_DAYS = 14


def user_ref(user: User, scope: str) -> str:
    """How to refer to a user in output at this scope.

    The pseudonym is the internal row id, which means nothing outside our
    own database but is stable across days — so the agent can say "u7 has
    missed four days running" and you can look u7 up in the dashboard.
    """
    return user.email if scope == SCOPE_ADMIN else f"u{user.id}"


REASON_HELP: dict[str, str] = {
    "ready": "Story was generated and sent.",
    "below_bar": "Move was smaller than the story threshold; sent the fixed small-move note.",
    "cap_reached": "Daily story cap was already used up when this move fired.",
    "no_clear_cause": "Research found no cause it could verify; sent a market-move note or a plain alert.",
    "failed": "Generation errored. See stock_articles.error.",
    "ai_disabled": "AI_ARTICLES_ENABLED is false.",
    "provider_paused": "AI provider key problem; generation paused.",
    "opted_out": "User turned alert emails off.",
    "email_unconfirmed": "User's notification email is not confirmed, so nothing is sent to it.",
    "no_watchlist": "User watches no tickers.",
}

# What to do about each reason. The agent quotes these so its weekly
# write-up suggests a fix instead of only naming a symptom.
REMEDIATION: dict[str, str] = {
    "cap_reached": "Raise AI_ARTICLE_MAX_PER_DAY, or AI_ARTICLE_ALWAYS_PCT so big moves bypass the cap.",
    "no_clear_cause": "Check AI_NEWS_DOMAINS coverage and AI_ARTICLE_MAX_SEARCHES; recurring on a liquid "
                      "large-cap usually means the research prompt or the search budget is too tight.",
    "failed": "Read stock_articles.error. Key/billing problems also email the admin via report_key_problem.",
    "provider_paused": "Check the provider key and billing; stories resume after AI_KEY_PROBLEM_PAUSE_MINUTES.",
    "email_unconfirmed": "Ask the user to confirm their notification email; nothing reaches them until they do.",
    "no_watchlist": "Starter recap covers these users; confirm it is enabled and the default ticker is seeded.",
}


def day_bounds(day: date_) -> tuple[datetime, datetime]:
    """Naive-UTC [start, end) covering one Eastern calendar day."""
    start_et = datetime.combine(day, time.min, tzinfo=_ET)
    end_et = start_et + timedelta(days=1)
    return (
        start_et.astimezone(_UTC).replace(tzinfo=None),
        end_et.astimezone(_UTC).replace(tzinfo=None),
    )


def _own_session(db: DBSession | None):
    """`(session, we_opened_it)` — jobs pass a session in, tests rely on it."""
    return (db, False) if db is not None else (get_session_factory()(), True)


@dataclass
class ExpectedJob:
    """A job we believe should have run on a weekday, and when."""

    job_id: str
    hour: int
    minute: int
    weekdays_only: bool


def expected_jobs() -> list[ExpectedJob]:
    """What the current config says should run, from settings — not hardcoded,
    so changing BATCH_JOB_TIMES_ET keeps this honest."""
    s = get_settings()
    out = [
        ExpectedJob(f"pipeline_{h:02d}{m:02d}", h, m, True) for h, m in s.batch_times
    ]
    if s.recap_enabled:
        rh, rm = s.recap_time
        out.append(ExpectedJob("watchlist_recap", rh, rm, True))
    if s.daily_summary_enabled:
        sh, sm = s.daily_summary_time
        out.append(ExpectedJob("daily_summary", sh, sm, False))
    return out


def jobs_report(day: date_, db: DBSession | None = None) -> dict:
    """Which scheduled jobs ran, when, and how far off their slot they were.

    `drift_minutes` is the catch for the timezone class of bug: a job firing
    exactly 180 minutes late every day means its trigger is resolving in
    Pacific rather than Eastern.
    """
    session, own = _own_session(db)
    try:
        start, end = day_bounds(day)
        runs = session.execute(
            select(JobRun)
            .where(JobRun.started_at >= start, JobRun.started_at < end)
            .order_by(JobRun.started_at)
        ).scalars().all()

        by_id: dict[str, list[JobRun]] = {}
        for r in runs:
            by_id.setdefault(r.job_name, []).append(r)

        weekday = day.weekday() < 5
        slots = []
        for exp in expected_jobs():
            if exp.weekdays_only and not weekday:
                continue
            want_utc = datetime.combine(
                day, time(exp.hour, exp.minute), tzinfo=_ET
            ).astimezone(_UTC).replace(tzinfo=None)
            got = by_id.get(exp.job_id) or []
            first = got[0] if got else None
            drift = (
                round((first.started_at - want_utc).total_seconds() / 60)
                if first else None
            )
            slots.append({
                "job_id": exp.job_id,
                "expected_et": f"{exp.hour:02d}:{exp.minute:02d}",
                "expected_utc": want_utc.isoformat(),
                "actual_utc": first.started_at.isoformat() if first else None,
                "drift_minutes": drift,
                "status": first.status if first else "MISSING",
                "error": first.error if first else None,
            })
        return {
            "trading_day": weekday,
            "slots": slots,
            "all_runs": [
                {
                    "job_name": r.job_name,
                    "status": r.status,
                    "started_at": r.started_at.isoformat(),
                    "duration_seconds": r.duration_seconds,
                    "result_summary": r.result_summary,
                    "error": r.error,
                }
                for r in runs
            ],
        }
    finally:
        if own:
            session.close()


def prices_report(day: date_, db: DBSession | None = None) -> dict:
    session, own = _own_session(db)
    try:
        start, end = day_bounds(day)
        ticks = session.execute(
            select(func.count()).select_from(IntradayPrice)
            .where(IntradayPrice.captured_at >= start, IntradayPrice.captured_at < end)
        ).scalar_one()
        tickers_ticked = session.execute(
            select(func.count(func.distinct(IntradayPrice.ticker_id)))
            .where(IntradayPrice.captured_at >= start, IntradayPrice.captured_at < end)
        ).scalar_one()
        last_tick = session.execute(
            select(func.max(IntradayPrice.captured_at))
            .where(IntradayPrice.captured_at >= start, IntradayPrice.captured_at < end)
        ).scalar_one()
        closes = session.execute(
            select(func.count()).select_from(DailyClose).where(DailyClose.date == day)
        ).scalar_one()
        return {
            "intraday_ticks": ticks,
            "tickers_ticked": tickers_ticked,
            "last_tick_utc": last_tick.isoformat() if last_tick else None,
            "closes_saved": closes,
        }
    finally:
        if own:
            session.close()


def stories_report(day: date_, db: DBSession | None = None) -> dict:
    """Every story row for the day, with token spend and estimated cost."""
    from app.services.articles import costs

    session, own = _own_session(db)
    try:
        rows = session.execute(
            select(StockArticle, Ticker.symbol)
            .join(Ticker, Ticker.id == StockArticle.ticker_id)
            .where(StockArticle.trading_date == day)
            .order_by(func.abs(StockArticle.move_pct).desc())
        ).all()
        settings = get_settings()
        items, total_cost, cost_known = [], 0.0, True
        for a, symbol in rows:
            cost = costs.token_cost(a.model, a.input_tokens, a.output_tokens, settings) if a.model else None
            if cost is None:
                cost_known = False
            else:
                total_cost += cost
            facts = 0
            if a.content:
                try:
                    facts = len(json.loads(a.content).get("facts") or [])
                except (ValueError, TypeError):
                    facts = 0
            items.append({
                "symbol": symbol,
                "direction": a.direction,
                "status": a.status,
                "move_pct": round(a.move_pct, 2),
                "market_pct": round(a.market_pct, 2) if a.market_pct is not None else None,
                "facts": facts,
                "model": a.model,
                "input_tokens": a.input_tokens,
                "output_tokens": a.output_tokens,
                "est_cost_usd": round(cost, 4) if cost is not None else None,
                "error": a.error,
                "created_at": a.created_at.isoformat(),
                "completed_at": a.completed_at.isoformat() if a.completed_at else None,
            })
        by_status: dict[str, int] = {}
        for i in items:
            by_status[i["status"]] = by_status.get(i["status"], 0) + 1
        return {
            "count": len(items),
            "by_status": by_status,
            "input_tokens": sum(i["input_tokens"] for i in items),
            "output_tokens": sum(i["output_tokens"] for i in items),
            "est_cost_usd": round(total_cost, 4) if cost_known else None,
            "items": items,
        }
    finally:
        if own:
            session.close()


def delivery_report(
    day: date_, db: DBSession | None = None, scope: str = SCOPE_ADMIN,
) -> dict:
    """Per-user delivery for the day: who got an alert, who got a recap, and
    who was eligible for one but received nothing.

    `scope` decides whether users are named. SCOPE_ADMIN returns real email
    addresses for the dashboard; SCOPE_SERVICE returns a stable pseudonym
    (`u7`) instead, so the monitoring agent keeps full per-user granularity
    — including the ability to correlate the same user across days — without
    any address leaving the app. Look the ref up in the dashboard to act on it.

    Until `delivery_outcomes` exists the "why" for a miss is inferred from
    the surrounding tables rather than recorded at the moment of the
    decision, so `reason` here is best-effort. `inferred` says which it is,
    so neither the dashboard nor the agent presents a guess as a fact.
    """
    session, own = _own_session(db)
    try:
        start, end = day_bounds(day)

        alerts = session.execute(
            select(NotificationLog, User, Ticker.symbol)
            .join(User, User.id == NotificationLog.user_id)
            .join(Ticker, Ticker.id == NotificationLog.ticker_id)
            .where(NotificationLog.sent_at >= start, NotificationLog.sent_at < end)
            .order_by(NotificationLog.sent_at)
        ).all()
        story_status = {
            (tid, d): st
            for tid, d, st in session.execute(
                select(StockArticle.ticker_id, StockArticle.direction, StockArticle.status)
                .where(StockArticle.trading_date == day)
            ).all()
        }
        alert_rows = []
        for n, user, symbol in alerts:
            up = story_status.get((n.ticker_id, "up"))
            down = story_status.get((n.ticker_id, "down"))
            st = up or down
            alert_rows.append({
                "user": user_ref(user, scope),
                "symbol": symbol,
                "event_type": n.event_type.value if hasattr(n.event_type, "value") else str(n.event_type),
                "sent_at": n.sent_at.isoformat(),
                "story_status": st or "(no story row)",
                "reason": _infer_reason(st),
                "inferred": True,
            })

        recaps = session.execute(
            select(RecapSend, User, MoneyIdeaSend.idea_key)
            .join(User, User.id == RecapSend.user_id)
            .outerjoin(
                MoneyIdeaSend,
                (MoneyIdeaSend.user_id == RecapSend.user_id)
                & (MoneyIdeaSend.trading_date == RecapSend.trading_date),
            )
            .where(RecapSend.trading_date == day)
            .order_by(RecapSend.sent_at)
        ).all()
        recap_rows = [
            {
                "user": user_ref(u, scope),
                "sent_at": r.sent_at.isoformat(),
                "money_idea": idea_key,
            }
            for r, u, idea_key in recaps
        ]

        got_recap = {r.user_id for r, _, _ in recaps}
        streaks = _miss_streaks(session, day)
        opted_out = set(session.execute(select(EmailOptOut.user_id)).scalars().all())
        recap_off = set(session.execute(select(RecapOptOut.user_id)).scalars().all())
        watch_counts = dict(session.execute(
            select(WatchlistItem.user_id, func.count()).group_by(WatchlistItem.user_id)
        ).all())
        missed = []
        for u in session.execute(select(User)).scalars().all():
            if u.id in got_recap:
                continue
            if u.id in opted_out:
                reason = "opted_out"
            elif u.id in recap_off:
                reason = "recap_off"
            elif not u.notify_email_confirmed:
                reason = "email_unconfirmed"
            elif not watch_counts.get(u.id):
                reason = "no_watchlist"
            else:
                reason = "unexplained"
            missed.append({
                "user": user_ref(u, scope),
                "reason": reason,
                "watch_count": watch_counts.get(u.id, 0),
                "missed_days_running": streaks.get(u.id, 0),
                "help": REASON_HELP.get(reason, "No recap row and no gate explains it — investigate."),
                "remediation": REMEDIATION.get(reason),
            })
        return {
            "alerts": alert_rows,
            "recaps": recap_rows,
            "recap_missed": missed,
            "recap_unexplained": sum(1 for m in missed if m["reason"] == "unexplained"),
        }
    finally:
        if own:
            session.close()


def _miss_streaks(session, day: date_) -> dict[int, int]:
    """Consecutive trading-ish days up to `day` with no recap, per user.

    One query for the whole window rather than one per user: the point is a
    cheap signal the agent can act on ("u7 has had nothing for four days"),
    not a precise trading-calendar count.
    """
    since = day - timedelta(days=_MISS_STREAK_DAYS)
    rows = session.execute(
        select(RecapSend.user_id, RecapSend.trading_date)
        .where(RecapSend.trading_date >= since, RecapSend.trading_date <= day)
    ).all()
    sent: dict[int, set] = {}
    for uid, d in rows:
        sent.setdefault(uid, set()).add(d)
    out: dict[int, int] = {}
    for uid, days in sent.items():
        streak = 0
        probe = day
        while probe >= since and probe not in days:
            streak += 1
            probe -= timedelta(days=1)
        out[uid] = streak
    return out


def _infer_reason(story_status: str | None) -> str:
    if story_status == "ready":
        return "ready"
    if story_status in ("no_clear_cause", "failed"):
        return story_status
    if story_status == "pending":
        return "still_generating"
    return "below_bar_or_capped"


def errors_report(day: date_, db: DBSession | None = None, limit: int = 100) -> dict:
    session, own = _own_session(db)
    try:
        start, end = day_bounds(day)
        rows = session.execute(
            select(LogEntry)
            .where(LogEntry.created_at >= start, LogEntry.created_at < end)
            .order_by(LogEntry.created_at.desc())
            .limit(limit)
        ).scalars().all()
        by_logger: dict[str, int] = {}
        for r in rows:
            if r.level in ("ERROR", "CRITICAL"):
                by_logger[r.logger] = by_logger.get(r.logger, 0) + 1
        return {
            "total": len(rows),
            "errors": sum(1 for r in rows if r.level in ("ERROR", "CRITICAL")),
            "warnings": sum(1 for r in rows if r.level == "WARNING"),
            "by_logger": by_logger,
            "items": [
                {
                    "created_at": r.created_at.isoformat(),
                    "level": r.level,
                    "logger": r.logger,
                    "message": r.message[:500],
                    "context": r.context,
                    "has_traceback": bool(r.exc_info),
                }
                for r in rows
            ],
        }
    finally:
        if own:
            session.close()


def followups_report(
    day: date_, db: DBSession | None = None, scope: str = SCOPE_ADMIN,
) -> dict:
    """Users promised "we're still looking". `abandoned` means they were
    promised a story and never received one — a silent user-visible miss."""
    session, own = _own_session(db)
    try:
        rows = session.execute(
            select(ArticleFollowup, User, Ticker.symbol, StockArticle.status)
            .join(StockArticle, StockArticle.id == ArticleFollowup.article_id)
            .join(User, User.id == ArticleFollowup.user_id)
            .join(Ticker, Ticker.id == StockArticle.ticker_id)
            .where(StockArticle.trading_date == day)
            .order_by(ArticleFollowup.created_at)
        ).all()
        items = [
            {
                "user": user_ref(u, scope),
                "symbol": symbol,
                "story_status": st,
                "followup_status": f.status,
                "sent_at": f.sent_at.isoformat() if f.sent_at else None,
            }
            for f, u, symbol, st in rows
        ]
        return {
            "count": len(items),
            "abandoned": sum(1 for i in items if i["followup_status"] == "abandoned"),
            "items": items,
        }
    finally:
        if own:
            session.close()


# Tables that grow with traffic rather than with users, plus the setting
# that bounds each one. Anything here without a retention setting grows
# forever, which is the thing worth catching early.
_GROWTH_TABLES: list[tuple[str, type, str, str | None]] = [
    ("info_logs", InfoLog, "created_at", "info_log_lifetime"),
    ("log_entries", LogEntry, "created_at", "log_lifetime"),
    ("job_runs", JobRun, "started_at", "job_runs_retention"),
    ("intraday_prices", IntradayPrice, "captured_at", "intraday_retention"),
    ("daily_closes", DailyClose, "captured_at", "price_history_lifetime"),
    ("notification_logs", NotificationLog, "sent_at", None),
    ("stock_articles", StockArticle, "created_at", None),
]

# Rough bytes per row including per-row overhead and index share. Only used
# when the real Postgres size isn't available (e.g. SQLite in tests).
_EST_ROW_BYTES = {
    "info_logs": 260, "log_entries": 420, "job_runs": 300,
    "intraday_prices": 90, "daily_closes": 90,
    "notification_logs": 220, "stock_articles": 5200,  # content JSON dominates
}
# How many recent rows to sample when judging whether a log table is noise.
_SAMPLE = 2000
_DIGIT_RUN = re.compile(r"\d+")


def _retention_days(setting_name: str | None) -> float | None:
    """Configured retention in days, via the app's own parser."""
    if setting_name is None:
        return None
    from app.services.alerts import parse_lifetime

    raw = getattr(get_settings(), setting_name, None)
    if not raw:
        return None
    try:
        return parse_lifetime(raw).total_seconds() / 86400
    except Exception:  # noqa: BLE001 — a bad setting shouldn't break the report
        return None


def _pg_table_bytes(session, table: str) -> int | None:
    """Real on-disk size including indexes, or None off Postgres."""
    if session.bind.dialect.name != "postgresql":
        return None
    try:
        from sqlalchemy import text

        return session.execute(
            text("SELECT pg_total_relation_size(:t)"), {"t": table}
        ).scalar_one()
    except Exception:  # noqa: BLE001
        return None


def storage_report(db: DBSession | None = None) -> dict:
    """Table growth, whether retention is actually enforced, and how much of
    the log volume is repeated noise.

    The point is cost control: a log table whose rows are 90% the same five
    messages is paying to store nothing, and a table whose oldest row is
    older than its retention setting means cleanup has stopped running.
    """
    session, own = _own_session(db)
    try:
        now = datetime.utcnow()
        tables = []
        total_bytes = 0
        bytes_known = True
        for name, model, ts_col, setting in _GROWTH_TABLES:
            col = getattr(model, ts_col)
            rows = session.execute(select(func.count()).select_from(model)).scalar_one()
            oldest = session.execute(select(func.min(col))).scalar_one()
            added_24h = session.execute(
                select(func.count()).select_from(model).where(col >= now - timedelta(hours=24))
            ).scalar_one()
            keep_days = _retention_days(setting)
            age_days = (now - oldest).total_seconds() / 86400 if oldest else 0.0
            real = _pg_table_bytes(session, name)
            if real is None:
                bytes_known = False
                est = rows * _EST_ROW_BYTES.get(name, 200)
            else:
                est = real
            total_bytes += est
            # Where this table settles if today's rate continues and
            # retention is enforced. Unbounded tables have no steady state.
            steady = round(added_24h * keep_days) if keep_days else None
            tables.append({
                "table": name,
                "rows": rows,
                "rows_added_24h": added_24h,
                "oldest_row_age_days": round(age_days, 1),
                "retention_setting": setting,
                "retention_days": keep_days,
                "retention_enforced": None if keep_days is None else age_days <= keep_days + 1.5,
                "bytes": est,
                "bytes_measured": real is not None,
                "mb": round(est / 1_048_576, 2),
                "projected_steady_rows": steady,
                "projected_steady_mb": (
                    round(steady * _EST_ROW_BYTES.get(name, 200) / 1_048_576, 2)
                    if steady is not None else None
                ),
                "unbounded": keep_days is None,
            })
        tables.sort(key=lambda t: -t["rows_added_24h"])
        return {
            "total_mb": round(total_bytes / 1_048_576, 2),
            "total_bytes_measured": bytes_known,
            "projected_steady_mb": round(sum(
                t["projected_steady_mb"] or 0 for t in tables
            ), 2),
            "tables": tables,
            "log_quality": log_quality(session),
        }
    finally:
        if own:
            session.close()


def log_quality(db: DBSession | None = None) -> dict:
    """Is the log volume carrying information, or repeating itself?

    `duplicate_ratio` near 1.0 means almost every row repeats a message we
    already have — storage spent for no diagnostic value. The offenders in
    `top_messages` are the lines to downgrade to DEBUG or rate-limit.
    """
    session, own = _own_session(db)
    try:
        out = {}
        for name, model in (("info_logs", InfoLog), ("log_entries", LogEntry)):
            recent = session.execute(
                select(model.message)
                .order_by(model.created_at.desc())
                .limit(_SAMPLE)
            ).scalars().all()
            # Collapse digits so "ingested 31 tickers" and "ingested 44
            # tickers" count as one shape, which is what matters for noise.
            shapes: dict[str, int] = {}
            for m in recent:
                # Collapse each digit RUN to one '#': "7 of 1200" and
                # "13 of 940" are the same line. Per-character replacement
                # would make them differ by digit count.
                key = _DIGIT_RUN.sub("#", m or "")[:160]
                shapes[key] = shapes.get(key, 0) + 1
            sampled = len(recent)
            distinct = len(shapes)
            top = sorted(shapes.items(), key=lambda kv: -kv[1])[:5]
            by_logger = dict(session.execute(
                select(model.logger, func.count())
                .group_by(model.logger)
                .order_by(func.count().desc())
                .limit(5)
            ).all())
            out[name] = {
                "sampled_rows": sampled,
                "distinct_shapes": distinct,
                "duplicate_ratio": round(1 - distinct / sampled, 3) if sampled else None,
                "top_messages": [
                    {"shape": s, "count": c, "pct_of_sample": round(100 * c / sampled, 1)}
                    for s, c in top
                ],
                "top_loggers": by_logger,
            }
        return out
    finally:
        if own:
            session.close()


def ml_corpus_report(db: DBSession | None = None) -> dict:
    """Health of the data set behind the planned story-generation model.

    `stock_articles` holds the labels (story + checked facts) and is never
    pruned; `daily_closes` holds the features (what the price actually
    did) and *is* pruned. If price history is shorter than story history,
    the oldest stories are already unusable as training examples: the
    label survives and the features are gone. That loss is silent and
    irreversible, so it's measured rather than assumed.

    `content_feedback` is the expensive part — real human preference
    labels on generated content — and nothing prunes it.
    """
    session, own = _own_session(db)
    try:
        from app.models import ContentFeedback

        stories = session.execute(
            select(func.count(), func.min(StockArticle.trading_date))
            .select_from(StockArticle).where(StockArticle.status == "ready")
        ).one()
        oldest_close = session.execute(select(func.min(DailyClose.date))).scalar_one()
        ratings = session.execute(
            select(ContentFeedback.rating, func.count())
            .group_by(ContentFeedback.rating)
        ).all()
        usable_from = max(d for d in (stories[1], oldest_close) if d) if (
            stories[1] and oldest_close
        ) else None
        orphaned = 0
        if stories[1] and oldest_close and stories[1] < oldest_close:
            orphaned = session.execute(
                select(func.count()).select_from(StockArticle)
                .where(StockArticle.status == "ready",
                       StockArticle.trading_date < oldest_close)
            ).scalar_one()
        return {
            "labelled_stories": stories[0],
            "oldest_story": stories[1].isoformat() if stories[1] else None,
            "oldest_price_row": oldest_close.isoformat() if oldest_close else None,
            "usable_from": usable_from.isoformat() if usable_from else None,
            "stories_without_price_context": orphaned,
            "feedback_labels": {str(r): c for r, c in ratings},
            "feedback_total": sum(c for _, c in ratings),
            "price_retention": get_settings().price_history_lifetime,
        }
    finally:
        if own:
            session.close()


def day_report(
    day: date_, db: DBSession | None = None, scope: str = SCOPE_ADMIN,
) -> dict:
    """Everything about one Eastern trading day, in one object.

    This is what the dashboard renders and what the review agent reads.
    """
    session, own = _own_session(db)
    try:
        settings = get_settings()
        report = {
            "date": day.isoformat(),
            "generated_at": datetime.utcnow().isoformat(),
            "config": {
                "ai_articles_enabled": settings.ai_articles_enabled,
                "ai_provider": settings.ai_provider,
                "story_model": settings.story_model,
                "news_source": settings.ai_news_source,
                "max_searches": settings.ai_article_max_searches,
                "max_stories_per_day": settings.ai_article_max_per_day,
                "min_pct_stock": settings.ai_article_min_pct_stock,
                "min_pct_fund": settings.ai_article_min_pct_fund,
                "batch_times_et": settings.batch_job_times_et,
                "recap_enabled": settings.recap_enabled,
                "alert_receiver_set": bool(settings.effective_alert_receiver),
            },
            "jobs": jobs_report(day, session),
            "prices": prices_report(day, session),
            "stories": stories_report(day, session),
            "delivery": delivery_report(day, session, scope),
            "followups": followups_report(day, session, scope),
            "errors": errors_report(day, session),
            "storage": storage_report(session),
            "ml_corpus": ml_corpus_report(session),
        }
        report["findings"] = findings(report)
        return report
    finally:
        if own:
            session.close()


def findings(report: dict) -> list[dict]:
    """Machine-checked problems in a day report, worst first.

    These are the deterministic checks — things we can assert without
    judgement. The review agent reads these first and then looks for the
    things rules can't catch (quality, trends, security patterns).
    """
    out: list[dict] = []

    def add(severity: str, code: str, detail: str, fix: str | None = None) -> None:
        out.append({"severity": severity, "code": code, "detail": detail, "remediation": fix})

    jobs = report["jobs"]
    if jobs["trading_day"]:
        for slot in jobs["slots"]:
            if slot["status"] == "MISSING":
                add("high", "job_missing",
                    f"{slot['job_id']} never ran (expected {slot['expected_et']} ET).",
                    "Check the container was awake; APScheduler is in-process so a sleeping "
                    "container silently skips jobs.")
            elif slot["status"] == "FAILED":
                add("high", "job_failed", f"{slot['job_id']} failed: {slot['error']}", None)
            elif slot["drift_minutes"] is not None and abs(slot["drift_minutes"]) >= 30:
                add("high", "job_drift",
                    f"{slot['job_id']} ran {slot['drift_minutes']} min off its {slot['expected_et']} ET slot.",
                    "A consistent 180-min drift means the CronTrigger is resolving in Pacific, "
                    "not Eastern — every trigger needs an explicit timezone.")

    if not report["config"]["alert_receiver_set"]:
        add("high", "no_alert_receiver",
            "ALERT_RECEIVER and SMTP_USERNAME are both empty, so every admin alert is "
            "silently dropped.",
            "Set ALERT_RECEIVER in Railway.")

    if jobs["trading_day"] and report["prices"]["closes_saved"] == 0:
        add("medium", "no_closes",
            "No daily_closes rows for this trading day; recaps and sparklines use stale prices.",
            "Confirm the last batch of the day completed after the market close.")

    st = report["stories"]
    if report["config"]["ai_articles_enabled"] and jobs["trading_day"] and st["count"] == 0:
        add("medium", "no_stories",
            "Stories are enabled but none were created. Either no move cleared the "
            "threshold or generation never ran.", None)
    failed = st["by_status"].get("failed", 0)
    if failed and st["count"]:
        add("medium" if failed < st["count"] else "high", "stories_failed",
            f"{failed} of {st['count']} stories failed.",
            REMEDIATION["failed"])
    no_cause = st["by_status"].get("no_clear_cause", 0)
    if st["count"] and no_cause / st["count"] >= 0.5:
        add("medium", "mostly_no_cause",
            f"{no_cause} of {st['count']} stories found no verifiable cause.",
            REMEDIATION["no_clear_cause"])

    ab = report["followups"]["abandoned"]
    if ab:
        add("medium", "followups_abandoned",
            f"{ab} user(s) were promised a story and never got one.",
            "Raise AI_ARTICLE_WAIT_SECONDS, or investigate why generation outran the promise.")

    d = report["delivery"]
    if d["recap_unexplained"]:
        add("high", "recap_unexplained",
            f"{d['recap_unexplained']} user(s) were eligible for a recap but got none, and no "
            "gate explains it.",
            "Check the recap job log for per-user exceptions.")
    for m in d["recap_missed"]:
        if m["reason"] == "email_unconfirmed":
            add("low", "email_unconfirmed",
                f"{m['user']} receives nothing: notification email unconfirmed.",
                REMEDIATION["email_unconfirmed"])

    # Storage is optional so `findings` stays callable on a partial report.
    store = report.get("storage")
    if store:
        for t in store["tables"]:
            if t["retention_enforced"] is False:
                add("medium", "retention_not_enforced",
                    f"{t['table']}: oldest row is {t['oldest_row_age_days']}d but "
                    f"{t['retention_setting']} keeps {t['retention_days']}d — cleanup "
                    "has stopped pruning it.",
                    "Check the log_cleanup job ran (daily 03:15 ET) and look for errors from "
                    "app.jobs.cleanup.")
            if t["unbounded"] and t["rows_added_24h"] > 1000:
                add("medium", "unbounded_growth",
                    f"{t['table']} grows with no retention setting and added "
                    f"{t['rows_added_24h']} rows in 24h ({t['mb']} MB so far).",
                    "Decide a retention window and add it to the cleanup job, or this table "
                    "grows until storage costs money.")
        for name, q in store["log_quality"].items():
            if q["duplicate_ratio"] is not None and q["duplicate_ratio"] >= 0.9 and q["sampled_rows"] >= 200:
                worst = q["top_messages"][0] if q["top_messages"] else None
                add("low", "log_mostly_noise",
                    f"{name}: {q['duplicate_ratio']:.0%} of recent rows repeat an existing "
                    f"message shape"
                    + (f"; '{worst['shape'][:60]}' alone is {worst['pct_of_sample']}%." if worst else "."),
                    "Downgrade the repeated lines to DEBUG or shorten this table's retention — "
                    "you're paying to store the same sentence.")

    ml = report.get("ml_corpus")
    if ml and ml["stories_without_price_context"]:
        add("high", "ml_data_loss",
            f"{ml['stories_without_price_context']} finished stories are older than the "
            f"oldest price row ({ml['oldest_price_row']}), so their market context has "
            "already been pruned — they can't be used as training examples.",
            f"PRICE_HISTORY_LIFETIME is {ml['price_retention']}. Raise it; the loss "
            "already taken is irreversible, but this stops it continuing.")

    errs = report["errors"]
    if errs["errors"]:
        add("high" if errs["errors"] >= 10 else "medium", "errors_logged",
            f"{errs['errors']} ERROR+ entries. Top loggers: "
            + ", ".join(f"{k} ({v})" for k, v in sorted(
                errs["by_logger"].items(), key=lambda x: -x[1])[:3]),
            None)

    order = {"high": 0, "medium": 1, "low": 2}
    out.sort(key=lambda f: order.get(f["severity"], 9))
    return out
