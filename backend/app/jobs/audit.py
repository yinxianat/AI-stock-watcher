"""Job audit helper — writes structured rows to the `job_runs` table.

Every scheduled job calls `record_job_run` at the end of its execution.
Best-effort: failures to write are logged but never raised, so audit
recording can never break the job it's observing.
"""

from __future__ import annotations

import logging

from app.db.database import get_session_factory
from app.models import JobRun, utcnow

log = logging.getLogger(__name__)


def record_job_run(
    job_name: str,
    status: str,
    started_at,
    duration_seconds: float | None = None,
    result_summary: str | None = None,
    tables_updated: list[str] | None = None,
    error: str | None = None,
) -> None:
    """Persist a JobRun row. Best-effort — never raises."""
    try:
        db = get_session_factory()()
        try:
            db.add(JobRun(
                job_name=job_name,
                status=status,
                started_at=started_at,
                finished_at=utcnow(),
                duration_seconds=duration_seconds,
                result_summary=result_summary,
                tables_updated=",".join(tables_updated) if tables_updated else None,
                error=error,
            ))
            db.commit()
        finally:
            db.close()
    except Exception:  # noqa: BLE001
        log.warning("Failed to record job_run for %s (non-fatal)", job_name)
