"""Expected jobs are matched against the names jobs actually record.

A scheduler id and an audit name are two different vocabularies and they do
not always agree: the pipeline is registered three times as `pipeline_HHMM`
and records every run as `batch_pipeline`; the retention sweep is registered
as `log_cleanup` and records itself as `cleanup`. Comparing one against the
other finds nothing and reports a healthy job as missing — the most
expensive kind of monitoring bug, because it teaches the admin to ignore the
dashboard.

Scanned from source rather than listed here, the same way the delivery
reason catalogue is: a new job whose audit name does not match its id should
fail the build, not quietly produce a daily false alarm.
"""

from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path

import pytest

from app.core.settings import get_settings
from app.models import JobRun, utcnow
from app.services.diagnostics import expected_jobs, jobs_report

JOBS_DIR = Path("app/jobs")


def recorded_names() -> set[str]:
    """Every literal name passed to `record_job_run` anywhere in app/jobs."""
    names: set[str] = set()
    for path in JOBS_DIR.glob("*.py"):
        text = path.read_text()
        names |= set(re.findall(r'record_job_run\(\s*"([a-z_]+)"', text))
    return names


def test_the_scan_finds_the_jobs_we_know_about():
    """Guards the regex: a scan that silently matches nothing would make
    every assertion below vacuously true."""
    found = recorded_names()
    assert {"batch_pipeline", "cleanup", "watchlist_recap", "daily_summary"} <= found


@pytest.mark.parametrize("job", expected_jobs(), ids=lambda j: j.job_id)
def test_every_expected_job_matches_a_name_some_job_records(job):
    assert job.recorded_as in recorded_names(), (
        f"{job.job_id} is expected to appear in job_runs as "
        f"{job.recorded_as!r}, but no job records that name. The dashboard "
        f"will report it as never having run."
    )


def _run(db_session, name: str, when):
    db_session.add(JobRun(job_name=name, status="SUCCESS", started_at=when))


def test_a_pipeline_that_ran_is_not_reported_missing(db_session, monkeypatch):
    """The regression: `batch_pipeline` ran, and every slot said MISSING."""
    monkeypatch.setenv("BATCH_JOB_TIMES_ET", "09:35")
    get_settings.cache_clear()
    from app.services.diagnostics import _ET

    day = utcnow().astimezone(_ET).date()
    at = utcnow().astimezone(_ET).replace(
        year=day.year, month=day.month, day=day.day,
        hour=9, minute=35, second=0, microsecond=0,
    )
    _run(db_session, "batch_pipeline", at)
    db_session.commit()

    slot = next(s for s in jobs_report(day, db_session)["slots"]
                if s["job_id"] == "pipeline_0935")
    assert slot["status"] == "SUCCESS"
    assert slot["drift_minutes"] == 0


def test_the_retention_sweep_that_ran_is_not_reported_missing(db_session, monkeypatch):
    """`log_cleanup` is the scheduler id; `cleanup` is what it records."""
    monkeypatch.setenv("LOG_CLEANUP_TIME_ET", "03:15")
    get_settings.cache_clear()
    from app.services.diagnostics import _ET

    day = utcnow().astimezone(_ET).date()
    at = utcnow().astimezone(_ET).replace(
        year=day.year, month=day.month, day=day.day,
        hour=3, minute=15, second=0, microsecond=0,
    )
    _run(db_session, "cleanup", at)
    db_session.commit()

    slot = next(s for s in jobs_report(day, db_session)["slots"]
                if s["job_id"] == "log_cleanup")
    assert slot["status"] == "SUCCESS"


def test_each_pipeline_slot_claims_its_own_run(db_session, monkeypatch):
    """All three slots record `batch_pipeline`, so taking the first match
    for each would credit every slot with the 09:35 run and report the other
    two as hours late."""
    monkeypatch.setenv("BATCH_JOB_TIMES_ET", "09:35,12:30,16:05")
    get_settings.cache_clear()
    from app.services.diagnostics import _ET

    day = utcnow().astimezone(_ET).date()
    base = utcnow().astimezone(_ET).replace(
        year=day.year, month=day.month, day=day.day, second=0, microsecond=0,
    )
    for hour, minute in ((9, 35), (12, 30), (16, 5)):
        _run(db_session, "batch_pipeline", base.replace(hour=hour, minute=minute))
    db_session.commit()

    slots = {s["job_id"]: s for s in jobs_report(day, db_session)["slots"]}
    for job_id in ("pipeline_0935", "pipeline_1230", "pipeline_1605"):
        assert slots[job_id]["status"] == "SUCCESS", job_id
        assert slots[job_id]["drift_minutes"] == 0, job_id


def test_a_genuinely_missing_slot_is_still_reported(db_session, monkeypatch):
    """The fix must not make every slot look healthy."""
    monkeypatch.setenv("BATCH_JOB_TIMES_ET", "09:35,12:30")
    get_settings.cache_clear()
    from app.services.diagnostics import _ET

    day = utcnow().astimezone(_ET).date()
    at = utcnow().astimezone(_ET).replace(
        year=day.year, month=day.month, day=day.day,
        hour=9, minute=35, second=0, microsecond=0,
    )
    _run(db_session, "batch_pipeline", at)   # only one run, two slots
    db_session.commit()

    slots = {s["job_id"]: s for s in jobs_report(day, db_session)["slots"]}
    assert slots["pipeline_0935"]["status"] == "SUCCESS"
    assert slots["pipeline_1230"]["status"] == "MISSING"


def test_drift_is_still_detected(db_session, monkeypatch):
    """The timezone catch has to survive the matching change."""
    monkeypatch.setenv("BATCH_JOB_TIMES_ET", "09:35")
    get_settings.cache_clear()
    from app.services.diagnostics import _ET

    day = utcnow().astimezone(_ET).date()
    late = utcnow().astimezone(_ET).replace(
        year=day.year, month=day.month, day=day.day,
        hour=9, minute=35, second=0, microsecond=0,
    ) + timedelta(minutes=180)
    _run(db_session, "batch_pipeline", late)
    db_session.commit()

    slot = next(s for s in jobs_report(day, db_session)["slots"]
                if s["job_id"] == "pipeline_0935")
    assert slot["drift_minutes"] == 180
