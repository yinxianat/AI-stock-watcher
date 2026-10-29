"""Pipeline heartbeat check.

Runs hourly on weekdays. Looks at the most recent *scheduled* pipeline time
(BATCH_JOB_TIMES_ET, Mon-Fri) that's at least GRACE old and checks that a
pipeline run started for it:

* No run started → the scheduler isn't running jobs (crashed or suspended
  service, timezone mismatch). One CRITICAL log, which emails the admin,
  per missed time — not one every hour.
* A run started but failed → the pipeline already alerted when it failed,
  so this only notes it.

Checking against the schedule (rather than "any success in the last N
hours") means weekends and the gap before Monday's first run aren't
mistaken for an outage.
"""

from __future__ import annotations

import logging
import time as _time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.core.settings import get_settings
from app.db.database import get_session_factory
from app.models import JobRun

log = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")
# A scheduled run is only "missed" once it's this late.
GRACE = timedelta(minutes=30)
# A run's recorded start can be a moment before its slot (clock skew).
_EARLY = timedelta(minutes=2)
# Missed slots already reported, so each is alerted once.
_reported: set[datetime] = set()


def scheduled_slots(start_utc: datetime, end_utc: datetime) -> list[datetime]:
    """Scheduled pipeline times (naive UTC) in (start_utc, end_utc], oldest first."""
    times = get_settings().batch_times
    start_et = start_utc.replace(tzinfo=timezone.utc).astimezone(_ET)
    end_et = end_utc.replace(tzinfo=timezone.utc).astimezone(_ET)
    slots: list[datetime] = []
    day = start_et.date()
    while day <= end_et.date():
        if day.weekday() < 5:  # Mon-Fri, as scheduled
            for h, m in times:
                slot = datetime(day.year, day.month, day.day, h, m, tzinfo=_ET)
                if start_et < slot <= end_et:
                    slots.append(slot.astimezone(timezone.utc).replace(tzinfo=None))
        day += timedelta(days=1)
    return sorted(slots)


def last_due_slot(now_utc: datetime) -> datetime | None:
    """Most recent scheduled time that's at least GRACE in the past."""
    slots = scheduled_slots(now_utc - timedelta(days=8), now_utc - GRACE)
    return slots[-1] if slots else None


def _et_label(slot_utc: datetime) -> str:
    return slot_utc.replace(tzinfo=timezone.utc).astimezone(_ET).strftime("%a %Y-%m-%d %H:%M")


def run_heartbeat(now_utc: datetime | None = None) -> bool:
    """True if the last scheduled pipeline run started (even if it failed)."""
    from app.jobs.audit import record_job_run
    from app.models import utcnow

    started = utcnow()
    now = now_utc or started
    t0 = _time.monotonic()
    slot = last_due_slot(now)
    if slot is None:
        log.info("Heartbeat: no scheduled pipeline times configured")
        return True

    session = get_session_factory()()
    try:
        row = session.execute(
            select(JobRun)
            .where(JobRun.job_name == "batch_pipeline")
            .where(JobRun.started_at >= slot - _EARLY)
            .order_by(JobRun.started_at.asc())
            .limit(1)
        ).scalar_one_or_none()
    finally:
        session.close()

    elapsed = _time.monotonic() - t0
    label = _et_label(slot)
    if row is None:
        if slot not in _reported:
            _reported.add(slot)
            if len(_reported) > 50:
                _reported.discard(min(_reported))
            log.critical(
                "Scheduled pipeline run at %s ET never started. The scheduler may "
                "be stopped (crashed or suspended service, or a timezone mismatch).",
                label,
            )
        record_job_run(
            "heartbeat", "FAILED", started, elapsed,
            result_summary=f"missed_slot={label} ET",
        )
        return False

    if row.status != "SUCCESS":
        log.warning("Heartbeat: pipeline run for %s ET started but failed (alerted at the time)", label)
    else:
        log.info("Heartbeat OK; pipeline for %s ET ran", label)
    record_job_run(
        "heartbeat", "SUCCESS", started, elapsed,
        result_summary=f"slot={label} ET, pipeline_status={row.status}",
    )
    return True
