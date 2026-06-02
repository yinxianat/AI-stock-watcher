"""Daily summary email — full per-service health snapshot.

Gathers stats from the last 24 hours and emails the admin a single digest
with: pipeline runs + durations, ingest/compute/notify stats, signups, error
rollup, SMTP stats, DB row counts, retention setting, and next scheduled jobs.
"""

from __future__ import annotations

import logging
import platform
import sys
from collections import Counter
from datetime import datetime, timedelta

from sqlalchemy import func, select

from app.core.settings import get_settings
from app.db.database import get_session_factory
from app.models import (
    DailyClose,
    LogEntry,
    NotificationLog,
    PriceSnapshot,
    Ticker,
    TrendAnalysis,
    User,
    WatchlistItem,
)
from app.models import (
    Session as DBSessionModel,
)
from app.services.alerts import AlertChannel, notify_admin

log = logging.getLogger(__name__)

WINDOW = timedelta(hours=24)

# Track process start so the summary can report uptime even without an
# external supervisor. Set on module import.
_PROCESS_STARTED_AT = datetime.utcnow()


def _last24h(session) -> datetime:
    return datetime.utcnow() - WINDOW


def collect_summary() -> dict:
    """Pull every metric the summary needs. Pure read; safe to call ad-hoc."""
    settings = get_settings()
    session = get_session_factory()()
    try:
        since = _last24h(session)

        # ---- Pipeline (from log_entries) ----
        sched_logs = session.execute(
            select(LogEntry)
            .where(LogEntry.logger == "app.jobs.scheduler")
            .where(LogEntry.created_at >= since)
        ).scalars().all()
        pipeline_starts = sum(1 for r in sched_logs if r.message.startswith("Pipeline start"))
        pipeline_complete = sum(1 for r in sched_logs if r.message.startswith("Pipeline complete"))
        pipeline_aborted = sum(1 for r in sched_logs if "Pipeline aborted" in r.message)
        last_complete = next(
            (r.created_at for r in sorted(sched_logs, key=lambda r: r.created_at, reverse=True)
             if r.message.startswith("Pipeline complete")),
            None,
        )

        # ---- Stage outcomes ----
        stage_logs = [r for r in sched_logs if r.message.startswith("Pipeline stage")]
        stage_ok = sum(1 for r in stage_logs if " ok " in r.message)
        stage_fail = sum(1 for r in stage_logs if "failed" in r.message)

        # ---- Ingest details ----
        ingest_logs = session.execute(
            select(LogEntry)
            .where(LogEntry.logger == "app.jobs.ingest")
            .where(LogEntry.created_at >= since)
        ).scalars().all()
        ingest_runs = [r for r in ingest_logs if r.message.startswith("Ingest complete")]
        ingest_failures_total = sum(1 for r in ingest_logs if r.level in ("WARNING", "ERROR", "CRITICAL"))

        # ---- Notify details ----
        notify_logs = session.execute(
            select(LogEntry)
            .where(LogEntry.logger == "app.jobs.notify")
            .where(LogEntry.created_at >= since)
        ).scalars().all()
        notify_runs = [r for r in notify_logs if r.message.startswith("Notify complete")]
        notify_warnings = sum(1 for r in notify_logs if r.level == "WARNING")

        # ---- SMTP ----
        smtp_logs = session.execute(
            select(LogEntry)
            .where(LogEntry.logger == "app.services.emailer")
            .where(LogEntry.created_at >= since)
        ).scalars().all()
        smtp_failures = sum(1 for r in smtp_logs if r.level in ("ERROR", "WARNING"))

        # ---- Notifications dispatched (durable, from notification_logs table) ----
        notifications_sent_24h = session.execute(
            select(func.count(NotificationLog.id))
            .where(NotificationLog.sent_at >= since)
        ).scalar_one()

        # ---- Auth / users ----
        signups_24h = session.execute(
            select(func.count(User.id)).where(User.created_at >= since)
        ).scalar_one()
        logins_24h = session.execute(
            select(func.count(User.id)).where(User.last_login_at >= since)
        ).scalar_one()
        active_sessions = session.execute(
            select(func.count(DBSessionModel.id))
            .where(DBSessionModel.revoked_at.is_(None))
            .where(DBSessionModel.expires_at >= datetime.utcnow())
        ).scalar_one()

        # ---- Error rollup ----
        recent_errors = session.execute(
            select(LogEntry)
            .where(LogEntry.created_at >= since)
            .where(LogEntry.level.in_(("ERROR", "CRITICAL")))
            .order_by(LogEntry.created_at.desc())
        ).scalars().all()
        errors_by_logger = Counter(r.logger for r in recent_errors)
        top_error_messages = Counter(
            (r.logger, r.message[:120]) for r in recent_errors
        ).most_common(10)

        # ---- DB row counts ----
        row_counts = {
            "users": session.execute(select(func.count(User.id))).scalar_one(),
            "tickers": session.execute(select(func.count(Ticker.id))).scalar_one(),
            "watchlist_items": session.execute(select(func.count(WatchlistItem.id))).scalar_one(),
            "daily_closes": session.execute(select(func.count(DailyClose.id))).scalar_one(),
            "price_snapshots": session.execute(select(func.count(PriceSnapshot.id))).scalar_one(),
            "trend_analyses": session.execute(select(func.count(TrendAnalysis.id))).scalar_one(),
            "notification_logs": session.execute(select(func.count(NotificationLog.id))).scalar_one(),
            "log_entries": session.execute(select(func.count(LogEntry.id))).scalar_one(),
        }

        # ---- System / process ----
        uptime = datetime.utcnow() - _PROCESS_STARTED_AT

        # ---- Next scheduled jobs (computed from settings; APScheduler also
        # has a runtime view but we want this to work even when called from
        # a one-off `python -c "..."`) ----
        next_pipelines = settings.batch_times
        next_summary = settings.daily_summary_time

        return {
            "generated_at": datetime.utcnow(),
            "window_hours": WINDOW.total_seconds() / 3600,
            "pipeline": {
                "starts": pipeline_starts,
                "completed": pipeline_complete,
                "aborted": pipeline_aborted,
                "last_complete_at": last_complete,
                "stages_ok": stage_ok,
                "stages_failed": stage_fail,
            },
            "ingest": {
                "runs_completed": len(ingest_runs),
                "warnings_or_errors": ingest_failures_total,
                "last_run_summary": ingest_runs[-1].message if ingest_runs else None,
            },
            "notify": {
                "runs_completed": len(notify_runs),
                "warnings": notify_warnings,
                "notifications_sent_24h": notifications_sent_24h,
                "last_run_summary": notify_runs[-1].message if notify_runs else None,
            },
            "smtp": {
                "failures": smtp_failures,
                "alert_receiver": settings.effective_alert_receiver,
            },
            "auth": {
                "signups_24h": signups_24h,
                "logins_24h": logins_24h,
                "active_sessions": active_sessions,
            },
            "errors": {
                "total_24h": len(recent_errors),
                "by_logger": dict(errors_by_logger),
                "top_messages": [
                    {"logger": lg, "message": msg, "count": n}
                    for (lg, msg), n in top_error_messages
                ],
            },
            "db_rows": row_counts,
            "system": {
                "uptime": str(uptime).split(".")[0],
                "python": sys.version.split()[0],
                "platform": platform.platform(),
                "app_env": settings.app_env,
                "log_lifetime": settings.log_lifetime,
                "price_history_lifetime": settings.price_history_lifetime,
                "batch_jobs_enabled": settings.batch_jobs_enabled,
                "daily_summary_enabled": settings.daily_summary_enabled,
            },
            "schedule": {
                "batch_times_et": [f"{h:02d}:{m:02d}" for h, m in next_pipelines],
                "daily_summary_et": f"{next_summary[0]:02d}:{next_summary[1]:02d}",
            },
        }
    finally:
        session.close()


def _format_text(s: dict) -> str:
    p = s["pipeline"]
    i = s["ingest"]
    n = s["notify"]
    m = s["smtp"]
    a = s["auth"]
    e = s["errors"]
    sysd = s["system"]
    sched = s["schedule"]
    rows = s["db_rows"]

    health = "HEALTHY"
    if p["aborted"] > 0 or p["stages_failed"] > 0 or e["total_24h"] > 0:
        health = "DEGRADED"
    if m["failures"] >= 5 or p["completed"] == 0:
        health = "UNHEALTHY"

    last_complete = p["last_complete_at"].isoformat() + "Z" if p["last_complete_at"] else "(none in window)"

    lines = [
        f"AI Stock Watcher — daily summary  ({s['generated_at'].isoformat()}Z, last {int(s['window_hours'])}h)",
        f"Overall health: {health}",
        "",
        "PIPELINE",
        f"  Starts:           {p['starts']}",
        f"  Completed:        {p['completed']}",
        f"  Aborted:          {p['aborted']}",
        f"  Last completion:  {last_complete}",
        f"  Stage OK / fail:  {p['stages_ok']} / {p['stages_failed']}",
        "",
        "INGEST",
        f"  Runs completed:        {i['runs_completed']}",
        f"  Warnings or errors:    {i['warnings_or_errors']}",
        f"  Last run:              {i['last_run_summary'] or '(no run logged)'}",
        "",
        "NOTIFY",
        f"  Runs completed:           {n['runs_completed']}",
        f"  Email send warnings:      {n['warnings']}",
        f"  Notifications dispatched: {n['notifications_sent_24h']}",
        f"  Last run:                 {n['last_run_summary'] or '(no run logged)'}",
        "",
        "SMTP",
        f"  Failures (24h):   {m['failures']}",
        f"  Alert receiver:   {m['alert_receiver'] or '(none configured)'}",
        "",
        "AUTH",
        f"  New signups (24h):   {a['signups_24h']}",
        f"  Logins (24h):        {a['logins_24h']}",
        f"  Active sessions:     {a['active_sessions']}",
        "",
        "ERRORS",
        f"  Total ERROR+/24h:  {e['total_24h']}",
    ]
    if e["by_logger"]:
        lines.append("  By logger:")
        for lg, ct in sorted(e["by_logger"].items(), key=lambda x: x[1], reverse=True):
            lines.append(f"    {ct:>4}  {lg}")
    if e["top_messages"]:
        lines.append("  Top messages:")
        for em in e["top_messages"]:
            lines.append(f"    {em['count']:>4}  [{em['logger']}] {em['message']}")
    lines += [
        "",
        "DATABASE ROW COUNTS",
    ]
    for k, v in rows.items():
        lines.append(f"  {k:<22} {v}")
    lines += [
        "",
        "SYSTEM",
        f"  App env:           {sysd['app_env']}",
        f"  Uptime:            {sysd['uptime']}",
        f"  Python:            {sysd['python']}",
        f"  Platform:          {sysd['platform']}",
        f"  Log lifetime:        {sysd['log_lifetime']}",
        f"  Price history life:  {sysd['price_history_lifetime']}",
        f"  Batch enabled:       {sysd['batch_jobs_enabled']}",
        f"  Summary enabled:     {sysd['daily_summary_enabled']}",
        "",
        "SCHEDULE",
        f"  Batch times (ET):   {', '.join(sched['batch_times_et'])}",
        f"  Daily summary (ET): {sched['daily_summary_et']}",
        "",
        "— end of summary —",
    ]
    return "\n".join(lines)


def run_daily_summary() -> dict:
    """Build + send the daily summary. Returns the data dict (for tests)."""
    try:
        data = collect_summary()
    except Exception:
        log.exception("Daily summary collection failed")
        raise
    body = _format_text(data)
    health = "HEALTHY"
    if data["pipeline"]["aborted"] > 0 or data["pipeline"]["stages_failed"] > 0 or data["errors"]["total_24h"] > 0:
        health = "DEGRADED"
    if data["smtp"]["failures"] >= 5 or data["pipeline"]["completed"] == 0:
        health = "UNHEALTHY"
    notify_admin(
        "daily_summary",
        f"Daily summary — {health}",
        body,
        channel=AlertChannel.SUMMARY,
    )
    log.info("Daily summary sent (health=%s)", health)
    return data
