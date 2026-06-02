"""APScheduler setup — fires ingest -> compute -> notify three times per day,
plus daily summary, log retention, heartbeat health-check, and a keepalive
self-ping that prevents Railway from sleeping the container."""

from __future__ import annotations

import logging
import time

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.core.settings import get_settings
from app.jobs.compute import run_compute
from app.jobs.ingest import run_ingest
from app.jobs.notify import run_notify

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Keepalive self-ping — prevents Railway from sleeping the container.
# Without this, the in-process scheduler dies when Railway scales to zero.
# ---------------------------------------------------------------------------

_KEEPALIVE_INTERVAL_SECONDS = 300  # 5 minutes


def _self_ping() -> None:
    """Hit our own /healthz endpoint to keep the container awake.

    Railway only counts EXTERNAL requests for its sleep/scale-to-zero
    detection — loopback requests to 127.0.0.1 don't count. So we ping
    via the public Railway domain (RAILWAY_PUBLIC_DOMAIN env var) when
    available, falling back to localhost for local dev.
    """
    import os

    import httpx

    try:
        public_domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip()
        if public_domain:
            url = f"https://{public_domain}/healthz"
        else:
            port = os.environ.get("PORT", "8000")
            url = f"http://127.0.0.1:{port}/healthz"
        r = httpx.get(url, timeout=10.0)
        log.info("Keepalive self-ping: %s → %d", url, r.status_code)
    except Exception as e:  # noqa: BLE001
        log.warning("Keepalive self-ping failed (%s): %s", url, e)


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
    log.warning("[AUDIT] Pipeline start")
    try:
        _run_stage("ingest", run_ingest)
        _run_stage("compute", run_compute)
        _run_stage("notify", run_notify)
    except Exception:
        # Already logged at ERROR by _run_stage; this just stops propagation
        # so APScheduler doesn't mark the trigger as dead.
        log.error("Pipeline aborted")
    else:
        log.warning("[AUDIT] Pipeline complete")


def start_scheduler() -> BackgroundScheduler:
    settings = get_settings()
    sched = BackgroundScheduler(timezone="US/Eastern")

    # ---- Log configuration summary so prod deploys are self-documenting ----
    log.info(
        "Scheduler config: BATCH_JOB_TIMES_ET=%s, INTRADAY_INGEST_ENABLED=%s, "
        "INTRADAY_TICK_MINUTES=%s, MARKET_WINDOW=%s-%s ET, "
        "STOCK_DATA_PROVIDER=%s, DAILY_SUMMARY_ENABLED=%s",
        settings.batch_job_times_et,
        settings.intraday_ingest_enabled,
        settings.intraday_tick_minutes,
        settings.intraday_market_open_et,
        settings.intraday_market_close_et,
        settings.stock_data_provider,
        settings.daily_summary_enabled,
    )

    # Main batch pipeline — Mon-Fri at each configured time.
    for hour, minute in settings.batch_times:
        sched.add_job(
            run_full_pipeline,
            CronTrigger(day_of_week="mon-fri", hour=hour, minute=minute),
            id=f"pipeline_{hour:02d}{minute:02d}",
            replace_existing=True,
        )
        log.info("Registered job: pipeline_%02d%02d (Mon-Fri %02d:%02d ET)", hour, minute, hour, minute)

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
        log.info("Registered job: daily_summary (daily %02d:%02d ET)", sh, sm)
    else:
        log.warning("Daily summary DISABLED — skipping registration")

    # Log retention cleanup — daily at 03:15 ET (off-peak).
    from app.jobs.cleanup import run_cleanup

    sched.add_job(
        run_cleanup,
        CronTrigger(hour=3, minute=15),
        id="log_cleanup",
        replace_existing=True,
    )
    log.info("Registered job: log_cleanup (daily 03:15 ET)")

    # Pipeline heartbeat — hourly on weekdays, alerts if no successful
    # pipeline in the last ~26 hours.
    from app.jobs.heartbeat import run_heartbeat

    sched.add_job(
        run_heartbeat,
        CronTrigger(day_of_week="mon-fri", minute=20),
        id="pipeline_heartbeat",
        replace_existing=True,
    )
    log.info("Registered job: pipeline_heartbeat (Mon-Fri hourly at :20)")

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
        # TODO: TEMPORARY — run 24/7 for testing. Restore market-hours cron:
        #   CronTrigger(day_of_week="mon-fri", hour=f"{open_h}-{close_h}", minute=f"*/{tick}")
        sched.add_job(
            lambda: run_intraday_capture(force=True),
            IntervalTrigger(minutes=tick),
            id="intraday_capture",
            replace_existing=True,
        )
        log.info(
            "Registered job: intraday_capture (TESTING MODE — every %d min, 24/7, force=True)",
            tick,
        )
    else:
        log.warning("Intraday ingest DISABLED — skipping registration")

    # Keepalive self-ping — prevents Railway from sleeping the container.
    sched.add_job(
        _self_ping,
        IntervalTrigger(seconds=_KEEPALIVE_INTERVAL_SECONDS),
        id="keepalive_self_ping",
        replace_existing=True,
    )
    log.info("Registered job: keepalive_self_ping (every %ds)", _KEEPALIVE_INTERVAL_SECONDS)

    total_jobs = len(sched.get_jobs())
    log.info("Scheduler starting with %d registered jobs", total_jobs)
    sched.start()
    # Persist to log_entries DB so we can verify the scheduler actually started
    # even if Railway stdout logs have rotated away.
    log.warning(
        "[AUDIT] Scheduler started with %d jobs: %s",
        total_jobs,
        ", ".join(j.id for j in sched.get_jobs()),
    )
    return sched
