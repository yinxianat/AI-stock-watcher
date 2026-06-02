"""APScheduler setup — fires ingest -> compute -> notify three times per day,
plus daily summary, log retention, and heartbeat health-check jobs."""

from __future__ import annotations

import logging
import time

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.settings import get_settings
from app.jobs.compute import run_compute
from app.jobs.ingest import run_ingest
from app.jobs.notify import run_notify

log = logging.getLogger(__name__)


def _run_stage(name: str, fn) -> None:
    """Run a pipeline stage with timing + granular error logging.

    Errors are logged at ERROR with the stage name so the alert email subject
    pinpoints which job failed, then re-raised so the orchestrator halts.
    Running compute/notify on stale or missing data would produce misleading
    output, so failures stop the pipeline.
    """
    started = time.monotonic()
    try:
        result = fn()
    except Exception:
        log.exception("Pipeline stage %r failed", name)
        raise
    log.info("Pipeline stage %r ok (%.2fs, result=%s)", name, time.monotonic() - started, result)


def run_full_pipeline() -> None:
    """Run the three jobs in order. Each opens its own DB session."""
    log.info("Pipeline start")
    try:
        _run_stage("ingest", run_ingest)
        _run_stage("compute", run_compute)
        _run_stage("notify", run_notify)
    except Exception:
        # Already logged at ERROR by _run_stage; this just stops propagation
        # so APScheduler doesn't mark the trigger as dead.
        log.error("Pipeline aborted")
    else:
        log.info("Pipeline complete")


def start_scheduler() -> BackgroundScheduler:
    settings = get_settings()
    sched = BackgroundScheduler(timezone="US/Eastern")

    # Main batch pipeline — Mon-Fri at each configured time.
    for hour, minute in settings.batch_times:
        sched.add_job(
            run_full_pipeline,
            CronTrigger(day_of_week="mon-fri", hour=hour, minute=minute),
            id=f"pipeline_{hour:02d}{minute:02d}",
            replace_existing=True,
        )

    # Daily summary — every day at configured time (default 17:30 ET).
    if settings.daily_summary_enabled:
        from app.jobs.daily_summary import run_daily_summary

        sh, sm = settings.daily_summary_time
        sched.add_job(
            run_daily_summary,
            CronTrigger(hour=sh, minute=sm),
            id="daily_summary",
            replace_existing=True,
        )

    # Log retention cleanup — daily at 03:15 ET (off-peak).
    from app.jobs.cleanup import run_cleanup

    sched.add_job(
        run_cleanup,
        CronTrigger(hour=3, minute=15),
        id="log_cleanup",
        replace_existing=True,
    )

    # Pipeline heartbeat — hourly on weekdays, alerts if no successful
    # pipeline in the last ~26 hours.
    from app.jobs.heartbeat import run_heartbeat

    sched.add_job(
        run_heartbeat,
        CronTrigger(day_of_week="mon-fri", minute=20),
        id="pipeline_heartbeat",
        replace_existing=True,
    )

    # Intraday capture — every INTRADAY_TICK_MINUTES during US market hours
    # on weekdays. The job itself also re-checks market hours so it's safe
    # if the cron schedule wakes it slightly outside the bounded window.
    if settings.intraday_ingest_enabled:
        from app.jobs.intraday import run_intraday_capture

        tick = max(1, int(settings.intraday_tick_minutes))
        open_h, _ = settings.intraday_market_open
        close_h, _ = settings.intraday_market_close
        # Bound the cron to the market-hours window — cheaper than waking
        # the job 144 times/day to no-op.
        sched.add_job(
            run_intraday_capture,
            CronTrigger(
                day_of_week="mon-fri",
                hour=f"{open_h}-{close_h}",
                minute=f"*/{tick}",
            ),
            id="intraday_capture",
            replace_existing=True,
        )

    sched.start()
    return sched
