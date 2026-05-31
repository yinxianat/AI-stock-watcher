"""Smoke-test the scheduler wiring without actually firing jobs.

We assemble the BackgroundScheduler, assert all six job IDs are present, then
shut it down before any trigger fires.
"""

from __future__ import annotations

import time

from app.jobs.scheduler import run_full_pipeline, start_scheduler


def test_start_scheduler_registers_all_jobs(monkeypatch):
    """All six configured jobs must be registered with the expected IDs."""
    # conftest defaults disable the daily summary; flip it on for this test so
    # we exercise the registration path.
    monkeypatch.setenv("DAILY_SUMMARY_ENABLED", "true")
    from app.core.settings import get_settings
    get_settings.cache_clear()

    sched = start_scheduler()
    try:
        ids = sorted(j.id for j in sched.get_jobs())
        assert "pipeline_0935" in ids
        assert "pipeline_1230" in ids
        assert "pipeline_1605" in ids
        assert "daily_summary" in ids
        assert "log_cleanup" in ids
        assert "pipeline_heartbeat" in ids
        assert len(ids) == 6
    finally:
        sched.shutdown(wait=False)


def test_daily_summary_skipped_when_disabled(monkeypatch):
    """Setting DAILY_SUMMARY_ENABLED=false omits that job from the schedule."""
    monkeypatch.setenv("DAILY_SUMMARY_ENABLED", "false")
    from app.core.settings import get_settings
    get_settings.cache_clear()

    sched = start_scheduler()
    try:
        ids = [j.id for j in sched.get_jobs()]
        assert "daily_summary" not in ids
        # Cleanup and heartbeat are always registered.
        assert "log_cleanup" in ids
        assert "pipeline_heartbeat" in ids
    finally:
        sched.shutdown(wait=False)


def test_run_full_pipeline_logs_completion(client, caplog, monkeypatch):
    """Orchestrator runs ingest+compute+notify and logs 'Pipeline complete'.

    We monkey-patch each stage to a fast no-op so this stays under a second.
    """
    from app.jobs import scheduler as sched_mod

    calls: list[str] = []
    monkeypatch.setattr(sched_mod, "run_ingest", lambda: calls.append("ingest") or 0)
    monkeypatch.setattr(sched_mod, "run_compute", lambda: calls.append("compute") or 0)
    monkeypatch.setattr(sched_mod, "run_notify", lambda: calls.append("notify") or 0)

    with caplog.at_level("INFO", logger="app.jobs.scheduler"):
        run_full_pipeline()

    assert calls == ["ingest", "compute", "notify"]
    assert any("Pipeline complete" in r.message for r in caplog.records)


def test_run_full_pipeline_halts_after_stage_failure(client, caplog, monkeypatch):
    """If ingest fails, compute and notify must NOT run."""
    from app.jobs import scheduler as sched_mod

    calls: list[str] = []

    def boom():
        calls.append("ingest_attempted")
        raise RuntimeError("synthetic ingest failure")

    monkeypatch.setattr(sched_mod, "run_ingest", boom)
    monkeypatch.setattr(sched_mod, "run_compute", lambda: calls.append("compute"))
    monkeypatch.setattr(sched_mod, "run_notify", lambda: calls.append("notify"))

    with caplog.at_level("ERROR", logger="app.jobs.scheduler"):
        run_full_pipeline()

    assert calls == ["ingest_attempted"]  # compute + notify skipped
    # Subject-pinpointing error logged.
    assert any("ingest" in r.message and "failed" in r.message for r in caplog.records)
