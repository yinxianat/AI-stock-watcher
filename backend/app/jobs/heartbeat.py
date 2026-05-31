"""Pipeline heartbeat check.

Runs hourly on weekdays. If no successful pipeline has logged "Pipeline
complete" in the last 26 hours, emits a CRITICAL log — which triggers the
email alert handler.

The 26-hour window covers: last batch is 16:05 ET, so by 17:00 the next day
we'd be ~25 hours past the most recent expected run. 26h gives a small grace
window before alerting.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import select

from app.db.database import get_session_factory
from app.models import LogEntry

log = logging.getLogger(__name__)

HEARTBEAT_WINDOW = timedelta(hours=26)


def run_heartbeat() -> bool:
    """Return True if a recent pipeline completion is on record."""
    cutoff = datetime.utcnow() - HEARTBEAT_WINDOW
    session = get_session_factory()()
    try:
        row = session.execute(
            select(LogEntry)
            .where(LogEntry.logger == "app.jobs.scheduler")
            .where(LogEntry.message.like("Pipeline complete%"))
            .where(LogEntry.created_at >= cutoff)
            .order_by(LogEntry.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
    finally:
        session.close()

    if row is None:
        log.critical(
            "No successful pipeline run in the last %s — scheduler may be dead "
            "(timezone mismatch, suspended dyno, or batch failure)",
            HEARTBEAT_WINDOW,
        )
        return False
    log.info("Heartbeat OK; last pipeline complete at %s", row.created_at.isoformat())
    return True
