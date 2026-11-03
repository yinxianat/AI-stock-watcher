"""Job times come from configuration, and the panel says so truthfully.

Two different promises are checked here. The first is that no schedule is
written down twice: `scheduler.py` reads every time from settings, enforced
by scanning the source, so a hardcoded time cannot creep back in. The second
is that the panel compares configuration against the *running* scheduler,
because a change in Railway does nothing until the container restarts and
that gap is invisible otherwise.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.core.settings import get_settings
from app.services.diagnostics import (
    expected_jobs,
    schedule_report,
    scheduled_jobs,
)

ET = ZoneInfo("America/New_York")
SCHEDULER_SOURCE = Path("app/jobs/scheduler.py")


class FakeJob:
    def __init__(self, job_id: str, next_run_time: datetime | None):
        self.id = job_id
        self.next_run_time = next_run_time


class FakeScheduler:
    """Enough of APScheduler to answer "what did you actually register?"."""

    def __init__(self, jobs: list[FakeJob], running: bool = True):
        self._jobs = jobs
        self.running = running

    def get_jobs(self) -> list[FakeJob]:
        return self._jobs


def _at(hour: int, minute: int) -> datetime:
    """The next occurrence of an ET wall-clock time, as APScheduler reports."""
    now = datetime.now(ET)
    when = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return when if when > now else when + timedelta(days=1)


def _row(report: dict, job_id: str) -> dict:
    return next(r for r in report["jobs"] if r["job_id"] == job_id)


# ---------------------------------------------------------------------------
# Nothing is written down twice
# ---------------------------------------------------------------------------


def test_the_scheduler_hardcodes_no_times():
    """Every cron slot in the scheduler is read from settings.

    Scanned rather than trusted, the same way the delivery reason catalogue
    is: this is the kind of constant that gets reintroduced by someone adding
    a job in a hurry, and the symptom is a panel that confidently reports a
    time the scheduler is not using.
    """
    source = SCHEDULER_SOURCE.read_text()
    offenders = [
        call for call in re.findall(r"_cron\([^)]*\)", source)
        if re.search(r"(?:hour|minute)\s*=\s*\d", call)
    ]
    assert offenders == [], (
        "these cron triggers carry a literal time instead of reading one "
        f"from settings: {offenders}"
    )


def test_the_keepalive_interval_is_configured_too():
    source = SCHEDULER_SOURCE.read_text()
    assert "settings.keepalive_interval_seconds" in source
    assert "_KEEPALIVE_INTERVAL_SECONDS" not in source


@pytest.mark.parametrize("job_id,setting", [
    ("pipeline_0935", "BATCH_JOB_TIMES_ET"),
    ("watchlist_recap", "RECAP_TIME_ET"),
    ("daily_summary", "DAILY_SUMMARY_TIME_ET"),
    ("log_cleanup", "LOG_CLEANUP_TIME_ET"),
    ("pipeline_heartbeat", "HEARTBEAT_MINUTE"),
    ("intraday_capture", "INTRADAY_TICK_MINUTES"),
    ("keepalive_self_ping", "KEEPALIVE_INTERVAL_SECONDS"),
])
def test_every_job_names_the_setting_that_controls_it(job_id, setting):
    """So the answer to "how do I move this?" is on the panel itself."""
    jobs = {j.job_id: j for j in scheduled_jobs()}
    assert jobs[job_id].setting == setting


# ---------------------------------------------------------------------------
# The configured times really do follow the configuration
# ---------------------------------------------------------------------------


def test_changing_the_recap_time_moves_it_on_the_panel(monkeypatch):
    monkeypatch.setenv("RECAP_TIME_ET", "17:45")
    get_settings.cache_clear()
    jobs = {j.job_id: j for j in scheduled_jobs()}
    assert jobs["watchlist_recap"].configured == "17:45 ET"
    assert (jobs["watchlist_recap"].hour, jobs["watchlist_recap"].minute) == (17, 45)


def test_changing_the_batch_times_changes_the_job_list(monkeypatch):
    monkeypatch.setenv("BATCH_JOB_TIMES_ET", "10:00,15:00")
    get_settings.cache_clear()
    # `pipeline_heartbeat` shares the prefix and is not a batch slot.
    ids = [j.job_id for j in scheduled_jobs() if re.fullmatch(r"pipeline_\d{4}", j.job_id)]
    assert ids == ["pipeline_1000", "pipeline_1500"]


def test_changing_the_cleanup_time_moves_it(monkeypatch):
    monkeypatch.setenv("LOG_CLEANUP_TIME_ET", "04:40")
    get_settings.cache_clear()
    jobs = {j.job_id: j for j in scheduled_jobs()}
    assert jobs["log_cleanup"].configured == "04:40 ET"


def test_the_retention_sweep_is_expected_by_the_jobs_card(monkeypatch):
    """It used to be missing, so a cleanup that stopped was never flagged —
    which is how a disk fills up with nobody noticing."""
    monkeypatch.setenv("LOG_CLEANUP_TIME_ET", "02:05")
    get_settings.cache_clear()
    cleanup = next(j for j in expected_jobs() if j.job_id == "log_cleanup")
    assert (cleanup.hour, cleanup.minute) == (2, 5)
    assert cleanup.weekdays_only is False  # a disk fills up at weekends too


# ---------------------------------------------------------------------------
# Comparing configuration against the scheduler that is actually running
# ---------------------------------------------------------------------------


def test_a_matching_scheduler_agrees(monkeypatch):
    monkeypatch.setenv("RECAP_TIME_ET", "16:30")
    get_settings.cache_clear()
    report = schedule_report(FakeScheduler([FakeJob("watchlist_recap", _at(16, 30))]))
    assert _row(report, "watchlist_recap")["agrees"] is True
    assert not [d for d in report["disagreements"] if "watchlist_recap" in d]


def test_a_stale_container_is_called_out(monkeypatch):
    """The failure this panel exists for: the env var changed, the running
    scheduler did not, and nothing else would ever say so."""
    monkeypatch.setenv("RECAP_TIME_ET", "17:45")
    get_settings.cache_clear()
    report = schedule_report(FakeScheduler([FakeJob("watchlist_recap", _at(16, 30))]))
    row = _row(report, "watchlist_recap")
    assert row["agrees"] is False
    assert row["configured"] == "17:45 ET"
    assert any("redeploy" in d for d in report["disagreements"])


def test_an_enabled_job_that_never_registered_is_flagged(monkeypatch):
    monkeypatch.setenv("RECAP_ENABLED", "true")
    get_settings.cache_clear()
    report = schedule_report(FakeScheduler([]))
    assert _row(report, "watchlist_recap")["agrees"] is False
    assert any("not registered" in d for d in report["disagreements"])


def test_a_disabled_job_is_not_expected_to_be_registered(monkeypatch):
    """Silence is the right answer for something switched off, not a finding."""
    monkeypatch.setenv("RECAP_ENABLED", "false")
    get_settings.cache_clear()
    report = schedule_report(FakeScheduler([]))
    row = _row(report, "watchlist_recap")
    assert row["enabled"] is False
    assert row["agrees"] is None
    assert not [d for d in report["disagreements"] if "watchlist_recap" in d]


def test_an_hourly_job_is_compared_on_the_minute_only(monkeypatch):
    monkeypatch.setenv("HEARTBEAT_MINUTE", "20")
    get_settings.cache_clear()
    # Next run is some hour from now; only :20 is the configured part.
    nxt = datetime.now(ET).replace(minute=20, second=0, microsecond=0) + timedelta(hours=1)
    report = schedule_report(FakeScheduler([FakeJob("pipeline_heartbeat", nxt)]))
    assert _row(report, "pipeline_heartbeat")["agrees"] is True

    monkeypatch.setenv("HEARTBEAT_MINUTE", "45")
    get_settings.cache_clear()
    report = schedule_report(FakeScheduler([FakeJob("pipeline_heartbeat", nxt)]))
    assert _row(report, "pipeline_heartbeat")["agrees"] is False


def test_an_interval_job_has_no_slot_to_disagree_about():
    """Keepalive runs every N seconds, so there is no wall-clock time to
    compare and claiming otherwise would be a false alarm every hour."""
    report = schedule_report(
        FakeScheduler([FakeJob("keepalive_self_ping", _at(3, 7))])
    )
    assert _row(report, "keepalive_self_ping")["agrees"] is None


def test_a_registered_job_nobody_described_is_surfaced():
    # Either a job added without listing it, or a stale id left by a rename.
    report = schedule_report(FakeScheduler([FakeJob("mystery_job", None)]))
    assert report["unlisted"] == ["mystery_job"]


def test_the_panel_still_works_with_no_scheduler():
    """In tests, or with BATCH_JOBS_ENABLED=false, the configured half is
    still worth showing rather than a blank page."""
    report = schedule_report(None)
    assert report["scheduler_running"] is False
    assert len(report["jobs"]) > 5
    assert all(r["next_run"] is None for r in report["jobs"])
    assert report["disagreements"] == []


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------


def test_the_schedule_endpoint_is_service_safe():
    """It carries job times and no user rows, so the monitoring agent may
    read it — unlike the query console."""
    from app.api.admin_routes import SERVICE_SAFE_PATHS

    assert "/admin/schedule" in SERVICE_SAFE_PATHS


def test_the_schedule_endpoint_needs_a_credential(client):
    assert client.get("/admin/schedule").status_code in (401, 403)


def test_the_schedule_endpoint_reports_every_job(client, monkeypatch):
    client.post("/auth/request-link", json={"email": "owner@example.com"})
    token = client.sent_emails[-1]["body_text"].split("token=")[1].split("\n")[0].strip()
    data = client.post("/auth/verify", json={"token": token}).json()
    monkeypatch.setenv("ADMIN_EMAILS", "owner@example.com")
    get_settings.cache_clear()
    body = client.get(
        "/admin/schedule",
        headers={"Authorization": f"Bearer {data['session_token']}"},
    ).json()
    assert {r["job_id"] for r in body["jobs"]} >= {
        "watchlist_recap", "daily_summary", "log_cleanup",
        "pipeline_heartbeat", "keepalive_self_ping",
    }
    assert all(r["setting"] for r in body["jobs"])
    assert body["timezone"]["stored"] == "UTC"
