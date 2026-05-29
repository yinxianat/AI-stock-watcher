"""APScheduler setup — fires ingest -> compute -> notify three times per day."""

from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.settings import get_settings
from app.jobs.compute import run_compute
from app.jobs.ingest import run_ingest
from app.jobs.notify import run_notify

log = logging.getLogger(__name__)


def run_full_pipeline() -> None:
    """Run the three jobs in order. Each opens its own DB session."""
    log.info("Pipeline start")
    try:
        run_ingest()
        run_compute()
        run_notify()
    except Exception:
        log.exception("Pipeline failed")
    else:
        log.info("Pipeline complete")


def start_scheduler() -> BackgroundScheduler:
    settings = get_settings()
    sched = BackgroundScheduler(timezone="US/Eastern")
    # Run only on US market weekdays (Mon–Fri).
    for hour, minute in settings.batch_times:
        sched.add_job(
            run_full_pipeline,
            CronTrigger(day_of_week="mon-fri", hour=hour, minute=minute),
            id=f"pipeline_{hour:02d}{minute:02d}",
            replace_existing=True,
        )
    sched.start()
    return sched
