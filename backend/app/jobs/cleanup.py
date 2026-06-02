"""Daily retention cleanup.

Three retention policies, each backed by its own setting:

* `LOG_LIFETIME` → prunes `log_entries` (default 30 days).
* `PRICE_HISTORY_LIFETIME` → prunes `daily_closes` (default 365 days).
* `INTRADAY_RETENTION` → prunes `intraday_prices` (default 7 days).

All run inside one job so APScheduler only schedules a single off-peak
cron tick.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import delete

from app.core.settings import get_settings
from app.db.database import get_session_factory
from app.models import InfoLog, IntradayPrice, LogEntry
from app.services.alerts import parse_lifetime
from app.services.price_history import prune_daily_closes

log = logging.getLogger(__name__)


def _prune_log_entries(session, lifetime_spec: str) -> int:
    try:
        lifetime = parse_lifetime(lifetime_spec)
    except ValueError:
        log.error("Invalid LOG_LIFETIME=%r; skipping log_entries cleanup", lifetime_spec)
        return 0

    cutoff = datetime.utcnow() - lifetime
    result = session.execute(delete(LogEntry).where(LogEntry.created_at < cutoff))
    session.commit()
    deleted = result.rowcount or 0
    log.info(
        "Log cleanup: deleted %d log_entries rows older than %s (cutoff=%s)",
        deleted, lifetime_spec, cutoff.isoformat(),
    )
    return deleted


def _prune_price_history(session, lifetime_spec: str) -> int:
    try:
        lifetime = parse_lifetime(lifetime_spec)
    except ValueError:
        log.error(
            "Invalid PRICE_HISTORY_LIFETIME=%r; skipping daily_closes cleanup",
            lifetime_spec,
        )
        return 0
    return prune_daily_closes(session, lifetime)


def _prune_intraday(session, lifetime_spec: str) -> int:
    try:
        lifetime = parse_lifetime(lifetime_spec)
    except ValueError:
        log.error(
            "Invalid INTRADAY_RETENTION=%r; skipping intraday_prices cleanup",
            lifetime_spec,
        )
        return 0
    cutoff = datetime.utcnow() - lifetime
    result = session.execute(
        delete(IntradayPrice).where(IntradayPrice.captured_at < cutoff)
    )
    session.commit()
    deleted = result.rowcount or 0
    log.info(
        "Intraday cleanup: deleted %d intraday_prices rows older than %s",
        deleted, cutoff.isoformat(),
    )
    return deleted


def _prune_info_logs(session, lifetime_spec: str) -> int:
    try:
        lifetime = parse_lifetime(lifetime_spec)
    except ValueError:
        log.error(
            "Invalid INFO_LOG_LIFETIME=%r; skipping info_logs cleanup",
            lifetime_spec,
        )
        return 0
    cutoff = datetime.utcnow() - lifetime
    result = session.execute(
        delete(InfoLog).where(InfoLog.created_at < cutoff)
    )
    session.commit()
    deleted = result.rowcount or 0
    log.info("Info logs cleanup: deleted %d info_logs rows older than %s", deleted, cutoff.isoformat())
    return deleted


def _prune_job_runs(session, lifetime_spec: str) -> int:
    try:
        lifetime = parse_lifetime(lifetime_spec)
    except ValueError:
        log.error(
            "Invalid JOB_RUNS_RETENTION=%r; skipping job_runs cleanup",
            lifetime_spec,
        )
        return 0
    from app.models import JobRun

    cutoff = datetime.utcnow() - lifetime
    result = session.execute(
        delete(JobRun).where(JobRun.started_at < cutoff)
    )
    session.commit()
    deleted = result.rowcount or 0
    log.info("Job runs cleanup: deleted %d job_runs rows older than %s", deleted, cutoff.isoformat())
    return deleted


def run_cleanup() -> int:
    """Run all retention pruning. Returns total rows deleted across tables."""
    import time as _time

    from app.jobs.audit import record_job_run
    from app.models import utcnow

    started = utcnow()
    t0 = _time.monotonic()
    settings = get_settings()
    session = get_session_factory()()
    try:
        deleted = 0
        deleted += _prune_log_entries(session, settings.log_lifetime)
        deleted += _prune_price_history(session, settings.price_history_lifetime)
        deleted += _prune_intraday(session, settings.intraday_retention)
        deleted += _prune_job_runs(session, settings.job_runs_retention)
        deleted += _prune_info_logs(session, settings.info_log_lifetime)
        elapsed = _time.monotonic() - t0
        log.info("Cleanup complete: %d total rows deleted (%.2fs)", deleted, elapsed)
        record_job_run(
            "cleanup", "SUCCESS", started, elapsed,
            result_summary=f"deleted={deleted} rows",
            tables_updated=["log_entries", "daily_closes", "intraday_prices", "job_runs", "info_logs"],
        )
        return deleted
    finally:
        session.close()
