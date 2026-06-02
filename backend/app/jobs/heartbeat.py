"""Pipeline heartbeat check.

Runs hourly on weekdays. If no successful pipeline has logged a SUCCESS
job_run in the last 26 hours, emits a CRITICAL log — which triggers the
email alert handler.

The 26-hour window covers: last batch is 16:05 ET, so by 17:00 the next day
we'd be ~25 hours past the most recent expected run. 26h gives a small grace
window before alerting.
"""

from __future__ import annotations

import logging
import time as _time
from datetime import datetime, timedelta

from sqlalchemy import select

from app.db.database import get_session_factory
from app.models import JobRun

log = logging.getLogger(__name__)

HEARTBEAT_WINDOW = timedelta(hours=26)


def run_heartbeat() -> bool:
    """Return True if a recent pipeline completion is on record."""
    from app.jobs.audit import record_job_run
    from app.models import utcnow

    started = utcnow()
    t0 = _time.monotonic()
    cutoff = datetime.utcnow() - HEARTBEAT_WINDOW
    session = get_session_factory()()
    try:
        row = session.execute(
            select(JobRun)
            .where(JobRun.job_name == "batch_pipeline")
            .where(JobRun.status == "SUCCESS")
            .where(JobRun.started_at >= cutoff)
            .order_by(JobRun.started_at.desc())
            .limit(1)
        ).scalar_one_or_none()
    finally:
        session.close()

    elapsed = _time.monotonic() - t0
    if row is None:
        log.critical(
            "No successful pipeline run in the last %s — scheduler may be dead "
            "(timezone mismatch, suspended dyno, or batch failure)",
            HEARTBEAT_WINDOW,
        )
        record_job_run(
            "heartbeat", "FAILED", started, elapsed,
            result_summary="no successful pipeline in heartbeat window",
        )
        return False
    log.info("Heartbeat OK; last pipeline at %s", row.started_at.isoformat())
    record_job_run(
        "heartbeat", "SUCCESS", started, elapsed,
        result_summary=f"last_pipeline={row.started_at.isoformat()}",
    )
    return True
