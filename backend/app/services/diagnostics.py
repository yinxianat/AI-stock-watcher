"""One place that answers "what happened in production, and why".

Three consumers read this module and nothing else:

* `app.api.admin_routes` — JSON for the /admin dashboard and the scheduled
  review agent.
* `app.jobs.daily_summary` — the nightly digest email.
* ad-hoc debugging in a shell.

Keeping every query here is the point: a metric added once shows up in the
dashboard, the email and the agent together, and they can never disagree
about what a number means.

Timezone note, because it bites every query in here: datetimes are UTC and
come back timezone-aware (`app.models.UTCDateTime`), while `trading_date` /
`date` columns hold the US/Eastern calendar date the market ran on — a day,
not an instant. `day_bounds()` turns an Eastern date into the UTC window to
filter timestamps by; never compare a timestamp column against a trading
date directly.

Every timestamp this module emits goes out twice: the ISO value, which
carries an explicit `+00:00`, and an `et()` sibling for a human comparing
against a schedule written in Eastern. `TIMEZONE_NOTE` ships the convention
in the payload so neither the dashboard nor the agent has to assume it.
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
    DeliveryOutcome,
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
    utcnow,
)

from app.services import delivery

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


# One sentence per reason, and what to do about it. Both are derived from
# `app.services.delivery.REASONS` rather than written again here: the
# sending jobs record those codes, so a second copy of the wording would
# drift and the dashboard would explain an outcome differently from the
# agent. Kept under these names because the API and the agent prompt use
# them.
REASON_HELP: dict[str, str] = {
    code: r.what for code, r in delivery.REASONS.items()
}
REMEDIATION: dict[str, str] = {
    code: r.fix for code, r in delivery.REASONS.items() if r.fix
}

# Pre-`delivery_outcomes` story statuses, mapped onto catalogue codes so a
# day from before this feature still reads the same way in the dashboard.
_LEGACY_STATUS_REASON = {
    "ready": "ready",
    "pending": "still_generating",
    "no_clear_cause": "no_clear_cause",
    "failed": "generation_failed",
}


def explain(reason: str) -> dict:
    """The catalogue entry for one reason, as JSON for a dashboard row.

    This is what a row expands into: what happened, whether it is a problem
    at all, the ranked possible causes with how to tell them apart, and the
    fix. Unknown codes get a usable shape rather than a KeyError, because a
    stored row can outlive the code that wrote it.
    """
    r = delivery.REASONS.get(reason)
    if r is None:
        return {
            "label": reason, "ok": False, "causes": [], "fix": None,
            "what": f"Unrecognised reason {reason!r} — recorded by a version of the "
                    "code that is no longer deployed.",
        }
    return {
        "label": r.label,
        "what": r.what,
        "ok": r.ok,
        "fix": r.fix,
        "causes": [{"cause": c.cause, "check": c.check} for c in r.causes],
    }


def et(dt: datetime | None) -> str | None:
    """A timestamp as Eastern wall-clock, labelled.

    Every monitoring timestamp is emitted twice: the ISO value, which now
    carries `+00:00` because `UTCDateTime` hands back aware datetimes, and
    this. Both, because they answer different questions — the ISO form is
    what a machine should parse and sort on, and this is what a human
    comparing against a 09:35 ET schedule can read without doing the
    arithmetic in their head. Getting that arithmetic wrong by an hour
    twice a year is exactly the class of bug the jobs card exists to catch.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_UTC)  # legacy rows, by the storage convention
    return dt.astimezone(_ET).strftime("%a %H:%M:%S ET")


# What every timestamp in a report means, stated in the payload rather than
# only in a docstring, so the dashboard and the monitoring agent can show
# it and neither has to assume.
TIMEZONE_NOTE = {
    "stored": "UTC",
    "iso_fields_carry_offset": True,
    "display": "America/New_York",
    "note": (
        "Timestamps are stored as UTC and returned as ISO-8601 with an explicit "
        "+00:00 offset. Fields ending in _et are the same instant as Eastern "
        "wall-clock, which is the timezone every job schedule is written in. "
        "Date-only fields (trading_date) are Eastern calendar days."
    ),
}


def day_bounds(day: date_) -> tuple[datetime, datetime]:
    """Aware-UTC [start, end) covering one Eastern calendar day.

    Eastern, not UTC, because "what happened on the 3rd" means the trading
    day a reader in New York means — which is 05:00 to 05:00 UTC in winter
    and 04:00 to 04:00 in summer, and this is the one place that difference
    is resolved.
    """
    start_et = datetime.combine(day, time.min, tzinfo=_ET)
    end_et = start_et + timedelta(days=1)
    return (start_et.astimezone(_UTC), end_et.astimezone(_UTC))


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
            ).astimezone(_UTC)
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
                "actual_et": et(first.started_at) if first else None,
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


def _story_delivery(
    alert_rows: list[dict], recap_rows: list[dict],
    outcomes: list[DeliveryOutcome], scope: str, session,
) -> dict:
    """Who got an AI story, who did not, and why — for one day.

    The counts answer "is this working at all". `missed` answers "what do I
    do about it": one row per family who got an email with nothing for kids
    in it, each carrying the ranked causes from the catalogue so the
    dashboard can expand a row without another request.

    `summary` is the same thing as a sentence, because the first question is
    always "is today normal?" and a reader should not have to add up columns
    to find out.
    """
    rows: list[dict] = []
    for r in alert_rows:
        rows.append({**r, "channel": "alert"})
    for r in recap_rows:
        if r.get("reason"):
            rows.append({**r, "channel": "recap", "symbol": r.get("symbol", "—")})
    # Follow-ups are a second email about a story already counted above, so
    # they are listed for completeness but never counted as a separate miss.
    followups = []
    for o in outcomes:
        if o.channel != "followup":
            continue
        user = session.get(User, o.user_id)
        ticker = session.get(Ticker, o.ticker_id) if o.ticker_id else None
        followups.append({
            "user": user_ref(user, scope) if user else f"u{o.user_id}",
            "symbol": ticker.symbol if ticker else "—",
            "channel": "followup",
            "reason": o.reason,
            "detail": o.detail,
            "story_state": o.story_state,
            "sent_at": o.sent_at.isoformat(),
            "sent_et": et(o.sent_at),
            "explain": explain(o.reason),
        })

    by_reason: dict[str, int] = {}
    for r in rows:
        by_reason[r["reason"]] = by_reason.get(r["reason"], 0) + 1

    with_story = [r for r in rows if r["reason"] in delivery.STORY_REASONS]
    notes = [r for r in rows if r.get("story_state") in
             (delivery.STATE_SMALL_MOVE, delivery.STATE_MARKET_DAY)]
    promised = [r for r in rows if r.get("story_state") == delivery.STATE_PENDING]
    missed = [r for r in rows if not explain(r["reason"])["ok"]]
    inferred = [r for r in rows if r.get("inferred")]

    return {
        "emails": len(rows),
        "with_story": len(with_story),
        "with_fixed_note": len(notes),
        "promised_followup": len(promised),
        "nothing_for_kids": len(missed),
        "by_reason": by_reason,
        "inferred_rows": len(inferred),
        "summary": _story_summary(len(rows), len(with_story), len(notes),
                                  len(promised), missed),
        "received": sorted({r["user"] for r in with_story}),
        "missed": [
            {
                "user": r["user"],
                "symbol": r.get("symbol", "—"),
                "channel": r["channel"],
                "reason": r["reason"],
                "detail": r.get("detail"),
                "inferred": bool(r.get("inferred")),
                "explain": r.get("explain") or explain(r["reason"]),
            }
            for r in missed
        ],
        "followups": followups,
    }


def _story_summary(
    total: int, with_story: int, notes: int, promised: int, missed: list[dict],
) -> str:
    """One sentence a human can read without adding up columns."""
    if not total:
        return "No alert or recap emails went out on this day."
    parts = [f"{with_story} of {total} email(s) carried an AI story"]
    if notes:
        parts.append(f"{notes} carried the fixed note instead (no AI cost)")
    if promised:
        parts.append(f"{promised} promised a follow-up")
    if not missed:
        return ". ".join(parts) + ". Nothing was unexplained."
    counts: dict[str, int] = {}
    for m in missed:
        label = explain(m["reason"])["label"]
        counts[label] = counts.get(label, 0) + 1
    worst = ", ".join(f"{n} × {label.lower()}" for label, n in
                      sorted(counts.items(), key=lambda kv: -kv[1]))
    parts.append(f"{len(missed)} had nothing for kids ({worst})")
    return ". ".join(parts) + "."


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

    Each row's "why" comes from the `delivery_outcomes` row the sending job
    wrote at the moment it decided, so `inferred` is False. Days from before
    that table existed fall back to inferring the reason from
    `stock_articles`, and say so: `inferred` True means the reason is a
    guess, and neither the dashboard nor the agent should present it as a
    fact.
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
        # Recorded outcomes, queued per (user, ticker) in send order so
        # repeated alerts on the same stock line up with the right one.
        outcomes = session.execute(
            select(DeliveryOutcome)
            .where(DeliveryOutcome.trading_date == day)
            .order_by(DeliveryOutcome.sent_at)
        ).scalars().all()
        queued: dict[tuple, list[DeliveryOutcome]] = {}
        for o in outcomes:
            queued.setdefault((o.channel, o.user_id, o.ticker_id), []).append(o)

        def take(channel: str, user_id: int, ticker_id: int | None) -> DeliveryOutcome | None:
            q = queued.get((channel, user_id, ticker_id))
            return q.pop(0) if q else None

        alert_rows = []
        for n, user, symbol in alerts:
            up = story_status.get((n.ticker_id, "up"))
            down = story_status.get((n.ticker_id, "down"))
            st = up or down
            o = take("alert", n.user_id, n.ticker_id)
            reason = (
                o.reason if o is not None
                else _LEGACY_STATUS_REASON.get(st or "", "below_bar")
            )
            alert_rows.append({
                "user": user_ref(user, scope),
                "symbol": symbol,
                "event_type": n.event_type.value if hasattr(n.event_type, "value") else str(n.event_type),
                "sent_at": n.sent_at.isoformat(),
                "sent_et": et(n.sent_at),
                "story_status": st or "(no story row)",
                "story_state": o.story_state if o else None,
                "reason": reason,
                "detail": o.detail if o else None,
                "age_group": o.age_group if o else None,
                "age_group_sent": o.age_group_sent if o else None,
                "article_id": o.article_id if o else None,
                "inferred": o is None,
                "explain": explain(reason),
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
        recap_rows = []
        for r, u, idea_key in recaps:
            o = next(
                (x for x in outcomes
                 if x.channel == "recap" and x.user_id == r.user_id), None,
            )
            reason = o.reason if o is not None else None
            recap_rows.append({
                "user": user_ref(u, scope),
                "sent_at": r.sent_at.isoformat(),
                "sent_et": et(r.sent_at),
                "money_idea": idea_key,
                "story_state": o.story_state if o else None,
                "reason": reason,
                "detail": o.detail if o else None,
                "inferred": o is None,
                "explain": explain(reason) if reason else None,
            })

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
                "explain": explain(reason),
            })
        return {
            "alerts": alert_rows,
            "recaps": recap_rows,
            "recap_missed": missed,
            "recap_unexplained": sum(1 for m in missed if m["reason"] == "unexplained"),
            "stories": _story_delivery(alert_rows, recap_rows, outcomes, scope, session),
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
                    "created_et": et(r.created_at),
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
                "sent_et": et(f.sent_at),
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
    # Unbounded on purpose, like notification_logs: it is the record of what
    # each family was actually sent, and it grows with emails rather than
    # with time. Listed here so that growth is visible rather than assumed.
    ("delivery_outcomes", DeliveryOutcome, "sent_at", None),
]

# Rough bytes per row including per-row overhead and index share. Only used
# when the real Postgres size isn't available (e.g. SQLite in tests).
_EST_ROW_BYTES = {
    "info_logs": 260, "log_entries": 420, "job_runs": 300,
    "intraday_prices": 90, "daily_closes": 90,
    "notification_logs": 220, "stock_articles": 5200,  # content JSON dominates
    "delivery_outcomes": 300,  # the detail sentence dominates
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
        now = utcnow()
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
            "generated_at": utcnow().isoformat(),
            "generated_et": et(utcnow()),
            "timezone": TIMEZONE_NOTE,
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


def _job_missing_evidence(jobs: dict, slot: dict) -> tuple[dict, list[dict]]:
    """Facts and ranked causes for one job that never ran.

    "The job did not run" is almost never the useful sentence; the useful
    one is which of three very different things happened. The three look
    identical in the job table and are told apart by what ran *around* the
    gap, so that is what this assembles:

    * nothing ran all day            → the process was not alive
    * earlier slots ran, later ones did not → it died mid-session
    * neighbours ran, this one did not      → this job specifically is broken

    Ordering matters more than completeness here: the admin reads the first
    cause and checks it, so the first cause has to be the likeliest one
    given the evidence, not the most common one in general.
    """
    slots = jobs["slots"]
    runs = jobs["all_runs"]
    mine = slot["expected_et"]
    earlier_ran = [x["job_id"] for x in slots
                   if x["expected_et"] < mine and x["status"] != "MISSING"]
    later_ran = [x["job_id"] for x in slots
                 if x["expected_et"] > mine and x["status"] != "MISSING"]
    others_ran = sorted({r["job_name"] for r in runs} - {slot["job_id"]})
    # The sharpest fact available: did anything at all run AFTER the slot
    # this job missed? If so the process was demonstrably alive then, which
    # rules out the whole "container asleep" family and leaves this job
    # itself. Comparing slot statuses can't do that — two slots a minute
    # apart say nothing about a process that died at noon.
    alive_after = any(r["started_at"] > slot["expected_utc"] for r in runs)

    evidence = {
        "job_id": slot["job_id"],
        "expected_et": slot["expected_et"],
        "expected_utc": slot["expected_utc"],
        "jobs_that_did_run": others_ran,
        "runs_recorded_today": len(runs),
        "earlier_slots_that_ran": earlier_ran,
        "later_slots_that_ran": later_ran,
        "last_run_today_utc": runs[-1]["started_at"] if runs else None,
        "something_ran_after_this_slot": alive_after,
    }

    asleep = {
        "cause": "The container was not running at that time, so the in-process "
                 "scheduler never fired. APScheduler lives inside the web process: no "
                 "process, no jobs, and no error either.",
        "check": "Check Railway for a restart, a failed deploy, or a service scaled to "
                 "zero around this time. `runs_recorded_today` being 0 is the tell.",
    }
    died_midway = {
        "cause": f"The process died after {', '.join(earlier_ran) or 'the earlier jobs'} "
                 "and never came back, so everything from this slot onwards was skipped.",
        "check": "Compare `last_run_today_utc` with this slot's expected time, then look "
                 "for a restart or an OOM kill in the Railway logs just after it.",
    }
    job_specific = {
        "cause": "This job alone is broken: its trigger is wrong, or it raised before it "
                 "could write its audit row.",
        "check": f"Other jobs ran fine ({', '.join(others_ran)}), so the scheduler was "
                 "alive. Look for an exception from this job's module, and confirm its "
                 "CronTrigger has an explicit US/Eastern timezone.",
    }
    switched_off = {
        "cause": "The job is switched off by configuration, so it is correctly not running "
                 "and only the expectation is wrong.",
        "check": "Check BATCH_JOBS_ENABLED, INTRADAY_INGEST_ENABLED and RECAP_ENABLED. "
                 "`expected_jobs` reads these, so a disabled job should not appear here — "
                 "if it does, the setting disagrees with the schedule.",
    }
    not_due = {
        "cause": "The slot has not actually arrived yet in Eastern time.",
        "check": "The /watch endpoint filters these out as `not_due_yet`; seeing it here "
                 "means the day being viewed is not today.",
    }

    if not runs:
        causes = [asleep, switched_off, not_due]
    elif alive_after:
        causes = [job_specific, switched_off, died_midway]
    elif earlier_ran:
        causes = [died_midway, asleep, job_specific]
    else:
        causes = [asleep, job_specific, switched_off]
    return evidence, causes


def findings(report: dict) -> list[dict]:
    """Machine-checked problems in a day report, worst first.

    These are the deterministic checks — things we can assert without
    judgement. The review agent reads these first and then looks for the
    things rules can't catch (quality, trends, security patterns).

    Each finding carries `evidence` (the facts behind it, so nobody has to
    take the one-line `detail` on trust) and `causes` (ranked candidate
    explanations, each with how to tell it from the others). "ingest never
    ran" is true but useless on its own; "ingest never ran, and so did
    nothing else all day, so the container was most likely asleep — check
    whether Railway scaled it to zero" is actionable. The dashboard shows
    `detail` and expands to the rest.
    """
    out: list[dict] = []

    def add(severity: str, code: str, detail: str, fix: str | None = None,
            evidence: dict | None = None, causes: list[dict] | None = None) -> None:
        out.append({
            "severity": severity, "code": code, "detail": detail, "remediation": fix,
            "evidence": evidence or {}, "causes": causes or [],
        })

    jobs = report["jobs"]
    if jobs["trading_day"]:
        for slot in jobs["slots"]:
            if slot["status"] == "MISSING":
                ev, causes = _job_missing_evidence(jobs, slot)
                add("high", "job_missing",
                    f"{slot['job_id']} never ran (expected {slot['expected_et']} ET).",
                    "Check the container was awake; APScheduler is in-process so a sleeping "
                    "container silently skips jobs.",
                    evidence=ev, causes=causes)
            elif slot["status"] == "FAILED":
                add("high", "job_failed", f"{slot['job_id']} failed: {slot['error']}", None,
                    evidence={
                        "job_id": slot["job_id"],
                        "expected_et": slot["expected_et"],
                        "started_utc": slot["actual_utc"],
                        "error": slot["error"],
                        "runs_today": [r for r in jobs["all_runs"]
                                       if r["job_name"] == slot["job_id"]],
                    },
                    causes=[
                        {"cause": "The job raised partway through, so its work is "
                                  "half-applied.",
                         "check": "Read the error above, then check whether the tables it "
                                  "writes have this day's rows."},
                        {"cause": "An upstream price API was down or rate-limited.",
                         "check": "Look for fetch warnings naming individual symbols just "
                                  "before the failure."},
                        {"cause": "A later run of the same job succeeded and this is the "
                                  "first attempt only.",
                         "check": "`runs_today` in the evidence lists every attempt."},
                    ])
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
            "threshold or generation never ran.", None,
            evidence={
                "min_pct_stock": report["config"].get("min_pct_stock"),
                "min_pct_fund": report["config"].get("min_pct_fund"),
                "alerts_sent": len(report["delivery"].get("alerts") or []),
                "recaps_sent": len(report["delivery"].get("recaps") or []),
            },
            causes=[
                {"cause": "Nothing moved enough to qualify — the ordinary quiet day.",
                 "check": "If alerts went out, their reasons say below_bar. If none did, "
                          "nothing crossed any rule either, which agrees."},
                {"cause": "The story step never ran because the provider is paused or the "
                          "switch is off on the sending service.",
                 "check": "Check the delivery reasons for provider_paused or ai_disabled; "
                          "they are recorded per email."},
                {"cause": "The intraday job did not run, so no alert triggered a story.",
                 "check": "Look at the jobs card for a missing intraday_capture."},
            ])
    failed = st["by_status"].get("failed", 0)
    if failed and st["count"]:
        add("medium" if failed < st["count"] else "high", "stories_failed",
            f"{failed} of {st['count']} stories failed.",
            REMEDIATION["generation_failed"],
            evidence={"failed": failed, "total": st["count"],
                      "by_status": st["by_status"]},
            causes=explain("generation_failed")["causes"])
    no_cause = st["by_status"].get("no_clear_cause", 0)
    if st["count"] and no_cause / st["count"] >= 0.5:
        add("medium", "mostly_no_cause",
            f"{no_cause} of {st['count']} stories found no verifiable cause.",
            REMEDIATION["no_clear_cause"],
            evidence={"no_clear_cause": no_cause, "total": st["count"],
                      "max_searches": report["config"].get("max_searches"),
                      "news_source": report["config"].get("news_source")},
            causes=explain("no_clear_cause")["causes"])

    # Per-family story delivery. The stories card counts rows in
    # `stock_articles`; this counts families who opened an email and found
    # nothing in it, which is the number that actually matters.
    sd = report["delivery"].get("stories") or {}
    if sd.get("nothing_for_kids"):
        worst: dict[str, int] = {}
        for m in sd["missed"]:
            worst[m["reason"]] = worst.get(m["reason"], 0) + 1
        top_reason = max(worst, key=lambda k: worst[k])
        add("medium" if sd["nothing_for_kids"] < sd["emails"] else "high",
            "stories_not_delivered",
            f"{sd['nothing_for_kids']} of {sd['emails']} email(s) carried nothing for "
            f"kids; most often {explain(top_reason)['label'].lower()}.",
            explain(top_reason)["fix"],
            evidence={"by_reason": sd["by_reason"], "summary": sd["summary"],
                      "missed": sd["missed"][:20]},
            causes=explain(top_reason)["causes"])
    if sd.get("inferred_rows"):
        add("low", "delivery_not_recorded",
            f"{sd['inferred_rows']} email(s) have no recorded outcome, so their reason "
            "is inferred rather than known.",
            "Expected for days before delivery_outcomes existed. On a current day it "
            "means a send path is not calling delivery.record.",
            evidence={"inferred_rows": sd["inferred_rows"], "emails": sd["emails"]},
            causes=[
                {"cause": "The day predates the delivery_outcomes table.",
                 "check": "Compare the date against when it was deployed."},
                {"cause": "An email is sent by a path that does not record an outcome.",
                 "check": "Every send should pair a NotificationLog or RecapSend row with "
                          "a delivery_outcomes row in the same transaction."},
            ])

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
