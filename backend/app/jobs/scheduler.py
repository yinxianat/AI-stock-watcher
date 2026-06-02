"""APScheduler setup — fires ingest -> compute -> notify three times per day,
plus daily summary, log retention, heartbeat health-check, and a keepalive
self-ping that prevents Railway from sleeping the container."""

from __future__ import annotations

import logging
import time
import traceback

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.core.settings import get_settings
from app.jobs.audit import record_job_run
from app.jobs.compute import run_compute
from app.jobs.ingest import run_ingest
from app.jobs.notify import run_notify
from app.models import utcnow

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

    started = utcnow()
    t0 = time.monotonic()
    url = ""
    try:
        public_domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip()
        if public_domain:
            url = f"https://{public_domain}/healthz"
        else:
            port = os.environ.get("PORT", "8000")
            url = f"http://127.0.0.1:{port}/healthz"
        r = httpx.get(url, timeout=10.0)
        elapsed = time.monotonic() - t0
        log.info("Keepalive self-ping: %s → %d", url, r.status_code)
        record_job_run(
            "keepalive", "SUCCESS", started, elapsed,
            result_summary=f"url={url}, status={r.status_code}",
        )
    except Exception as e:  # noqa: BLE001
        elapsed = time.monotonic() - t0
        log.warning("Keepalive self-ping failed (%s): %s", url, e)
        record_job_run(
            "keepalive", "FAILED", started, elapsed,
            error=str(e),
        )


def _run_stage(name: str, fn) -> tuple[float, int | None]:
    """Run a pipeline stage with timing + granular error logging.

    Returns (elapsed_seconds, result).

    Errors are logged at ERROR with the stage name so the alert email subject
    pinpoints which job failed, then re-raised so the orchestrator halts.
    """
    started = time.monotonic()
    try:
        result = fn()
    except Exception:
        log.exception("Pipeline stage %r failed", name)
        raise
    elapsed = time.monotonic() - started
    log.info("Pipeline stage %r ok (%.2fs, result=%s)", name, elapsed, result)
    return elapsed, result


def run_full_pipeline() -> None:
    """Run the three jobs in order. Each opens its own DB session."""
    log.info("Pipeline start")
    started = utcnow()
    pipeline_start = time.monotonic()
    stages: dict[str, dict] = {}
    failed_stage = None
    error_detail = None
    try:
        elapsed, result = _run_stage("ingest", run_ingest)
        stages["ingest"] = {
            "status": "SUCCESS", "elapsed": f"{elapsed:.2f}s",
            "tickers_ingested": result,
            "tables_updated": ["price_snapshots", "daily_closes"],
        }

        elapsed, result = _run_stage("compute", run_compute)
        stages["compute"] = {
            "status": "SUCCESS", "elapsed": f"{elapsed:.2f}s",
            "trend_rows_written": result,
            "tables_updated": ["trend_analyses"],
        }

        elapsed, result = _run_stage("notify", run_notify)
        stages["notify"] = {
            "status": "SUCCESS", "elapsed": f"{elapsed:.2f}s",
            "emails_sent": result,
            "tables_updated": ["notification_logs"] if result else [],
        }
    except Exception:
        for name in ("ingest", "compute", "notify"):
            if name not in stages:
                failed_stage = name
                stages[name] = {"status": "FAILED"}
                break
        error_detail = traceback.format_exc()
        log.error("Pipeline aborted at stage %r", failed_stage)
    total_elapsed = time.monotonic() - pipeline_start

    # Build summary and collect all updated tables
    stage_parts = []
    all_tables: list[str] = []
    for name in ("ingest", "compute", "notify"):
        s = stages.get(name)
        if s is None:
            stage_parts.append(f"{name}=SKIPPED")
        elif s["status"] == "FAILED":
            stage_parts.append(f"{name}=FAILED")
        else:
            details = ", ".join(
                f"{k}={v}" for k, v in s.items()
                if k not in ("status", "tables_updated")
            )
            tables = s.get("tables_updated", [])
            all_tables.extend(tables)
            tables_str = f", db=[{','.join(tables)}]" if tables else ""
            stage_parts.append(f"{name}=SUCCESS({details}{tables_str})")

    overall = "FAILED" if failed_stage else "SUCCESS"
    summary = " | ".join(stage_parts)
    log.info("Pipeline %s (%.2fs): %s", overall, total_elapsed, summary)

    record_job_run(
        "batch_pipeline", overall, started, total_elapsed,
        result_summary=summary,
        tables_updated=all_tables or None,
        error=error_detail,
    )


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
        log.info(
            "Registered job: intraday_capture (Mon-Fri %02d:%02d-%02d:%02d ET, every %d min)",
            open_h, settings.intraday_market_open[1], close_h, settings.intraday_market_close[1], tick,
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
    job_ids = ", ".join(j.id for j in sched.get_jobs())
    log.info("Scheduler started with %d jobs: %s", total_jobs, job_ids)
    # Record startup as a job_run so it's queryable even after stdout rotates.
    record_job_run(
        "scheduler_start", "SUCCESS", utcnow(), 0.0,
        result_summary=f"{total_jobs} jobs: {job_ids}",
    )
    return sched
