"""Every email records why it carried a story, or why it didn't.

Before `delivery_outcomes`, an alert that went out as a bare price line left
no trace of the reason, and "no story" has a dozen causes that look
identical from outside — from "the move was too small to be worth paying
for" (working as designed) to "the provider key is dead" (needs you now).
These tests pin that each of those causes is recorded at the moment of the
decision, by the job that sent the email, with the reason it actually used.

The suite leans on the end-to-end job rather than calling the recorder
directly wherever it can: the thing worth protecting is that a real send
writes a real row, not that a helper works in isolation.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import select

from app.db.database import get_session_factory
from app.jobs.intraday import run_intraday_capture
from app.models import DeliveryOutcome, StockArticle, Ticker, User, utcnow
from app.services import delivery, diagnostics
from app.services.articles import service
from app.services.articles.schemas import GenerationResult

from tests.test_articles import _fact, _kid, _story, stories_on  # noqa: F401
from tests.test_intraday import _seed_prior_day_close, _setup_user_with_range_rule


def _fetch(prices: dict[str, float]):
    return lambda symbol: prices[symbol]


def _outcomes() -> list[DeliveryOutcome]:
    db = get_session_factory()()
    rows = db.execute(
        select(DeliveryOutcome).order_by(DeliveryOutcome.id)
    ).scalars().all()
    db.close()
    return rows


def _one() -> DeliveryOutcome:
    rows = _outcomes()
    assert len(rows) == 1, f"expected exactly one outcome, got {[r.reason for r in rows]}"
    return rows[0]


# ---------------------------------------------------------------------------
# The catalogue has to cover everything the code can record
# ---------------------------------------------------------------------------


def test_every_reason_the_code_records_exists_in_the_catalogue():
    """A reason code with no catalogue entry is a dashboard row that says
    nothing, which is the exact failure this feature exists to fix. Scanned
    from the source so adding a code without its explanation fails here
    rather than in production six weeks later.
    """
    root = Path(__file__).resolve().parent.parent / "app"
    used: set[str] = set()
    for path in root.rglob("*.py"):
        for m in re.finditer(r'reason="([a-z_]+)"', path.read_text()):
            used.add(m.group(1))
    assert used, "found no reason= literals — did the scan path break?"
    missing = sorted(used - set(delivery.REASONS))
    assert not missing, f"reasons recorded by code but not explained: {missing}"


def test_catalogue_entries_are_actually_explanatory():
    """Each entry has to carry what happened, and anything that is not a
    healthy outcome has to carry at least one ranked cause and a fix —
    otherwise expanding the row tells the admin nothing new."""
    for code, r in delivery.REASONS.items():
        assert len(r.what) > 40, f"{code}: 'what' is too thin to be useful"
        assert r.label and len(r.label) < 40, f"{code}: label should be a short phrase"
        if not r.ok:
            assert r.causes, f"{code}: a problem with no candidate causes"
            assert r.fix, f"{code}: a problem with no remediation"
            for c in r.causes:
                assert c.check, f"{code}: a cause with no way to check it"


def test_explain_survives_a_code_it_has_never_seen():
    """Rows outlive the code that wrote them, so an unknown reason must
    render rather than raise."""
    out = diagnostics.explain("reason_from_a_future_deploy")
    assert out["ok"] is False
    assert "Unrecognised" in out["what"]


# ---------------------------------------------------------------------------
# The happy paths
# ---------------------------------------------------------------------------


def test_story_sent_is_recorded_against_the_user(client, stories_on):
    _, ticker_id = _setup_user_with_range_rule("AAPL", -5.0, 5.0)
    _seed_prior_day_close(ticker_id, close=100.0)

    assert run_intraday_capture(fetcher=_fetch({"AAPL": 106.4}), force=True) == 1

    row = _one()
    assert row.reason == "ready"
    assert row.story_state == delivery.STATE_STORY
    assert row.channel == delivery.CHANNEL_ALERT
    assert row.article_id is not None
    assert row.age_group == "6-9"  # the default preference
    assert "AAPL" in row.detail


def test_a_small_move_records_the_free_note_as_a_healthy_outcome(client, stories_on):
    """Under the bar there is no AI call and no cost. The dashboard must not
    count this as a miss, or every quiet day reads as a failure."""
    _, ticker_id = _setup_user_with_range_rule("AAPL", -0.5, 0.5)
    _seed_prior_day_close(ticker_id, close=100.0)

    assert run_intraday_capture(fetcher=_fetch({"AAPL": 100.8}), force=True) == 1

    row = _one()
    assert row.reason == "below_bar"
    assert row.story_state == delivery.STATE_SMALL_MOVE
    assert delivery.REASONS[row.reason].ok is True
    assert "0.80%" in row.detail


def test_an_age_substitution_is_visible_even_though_the_parent_cannot_see_it(
    client, stories_on, monkeypatch,
):
    """When the chosen age group's version fails a safety check the nearest
    age is sent instead and the parent is never told. That is the right call
    for the family and the wrong one for the admin, so it is recorded."""
    monkeypatch.setattr(
        service, "generate_article",
        lambda req: GenerationResult(
            status="ready", story=_story(versions=[_kid("13-17")]),
            facts=[_fact()], model="fake",
        ),
    )
    _, ticker_id = _setup_user_with_range_rule("AAPL", -5.0, 5.0)
    _seed_prior_day_close(ticker_id, close=100.0)

    run_intraday_capture(fetcher=_fetch({"AAPL": 106.4}), force=True)

    row = _one()
    assert row.reason == "age_substituted"
    assert row.age_group == "6-9"
    assert row.age_group_sent == "13-17"
    assert delivery.REASONS[row.reason].ok is True


# ---------------------------------------------------------------------------
# The reasons a family got nothing
# ---------------------------------------------------------------------------


def test_no_clear_cause_is_recorded_rather_than_inferred(client, stories_on, monkeypatch):
    """The commonest silent miss: research ran, nothing survived
    fact-checking, and the alert went out as a bare price line."""
    monkeypatch.setattr(
        service, "generate_article",
        lambda req: GenerationResult(status="no_clear_cause", model="fake"),
    )
    _, ticker_id = _setup_user_with_range_rule("AAPL", -5.0, 5.0)
    _seed_prior_day_close(ticker_id, close=100.0)

    run_intraday_capture(fetcher=_fetch({"AAPL": 106.4}), force=True)

    row = _one()
    assert row.reason == "no_clear_cause"
    assert row.story_state == delivery.STATE_NONE
    assert "no cause" in row.detail


def test_generation_failure_is_recorded_with_its_error(client, stories_on, monkeypatch):
    monkeypatch.setattr(
        service, "generate_article",
        lambda req: GenerationResult(
            status="failed", error="reviewer rejected every age version", model="fake",
        ),
    )
    _, ticker_id = _setup_user_with_range_rule("AAPL", -5.0, 5.0)
    _seed_prior_day_close(ticker_id, close=100.0)

    run_intraday_capture(fetcher=_fetch({"AAPL": 106.4}), force=True)

    row = _one()
    assert row.reason == "generation_failed"
    assert "reviewer rejected" in row.detail


def test_stories_switched_off_is_recorded_per_email(client, monkeypatch):
    """With the feature off nobody gets a story — not even the free
    small-move note, which is gated on the same switch. That is worth
    recording, because the alert itself looks completely normal."""
    monkeypatch.setenv("AI_ARTICLES_ENABLED", "false")
    from app.core.settings import get_settings

    get_settings.cache_clear()
    _, ticker_id = _setup_user_with_range_rule("AAPL", -5.0, 5.0)
    _seed_prior_day_close(ticker_id, close=100.0)

    run_intraday_capture(fetcher=_fetch({"AAPL": 106.4}), force=True)

    row = _one()
    assert row.reason == "ai_disabled"
    assert "AI_ARTICLES_ENABLED" in row.detail


def test_the_daily_cap_names_the_family_that_missed_out(client, stories_on, monkeypatch):
    monkeypatch.setenv("AI_ARTICLE_MAX_PER_DAY", "0")
    from app.core.settings import get_settings

    get_settings.cache_clear()
    _, ticker_id = _setup_user_with_range_rule("AAPL", -5.0, 5.0)
    _seed_prior_day_close(ticker_id, close=100.0)

    run_intraday_capture(fetcher=_fetch({"AAPL": 106.4}), force=True)

    row = _one()
    assert row.reason == "cap_reached"
    assert "AAPL" in row.detail


def test_a_missing_previous_close_is_recorded_rather_than_crashing(client, stories_on):
    """A guard, not a live path: the capture loop skips a ticker with no
    previous close before any rule is evaluated, so today this cannot
    happen end-to-end. It is tested because `stock_articles.prev_close` is
    NOT NULL — if an alert ever did reach here without one, the choice is
    between a recorded skip and an IntegrityError mid-send.
    """
    from app.jobs.intraday import _Alert, _stories_for
    from app.db.seed import seed

    seed()
    db = get_session_factory()()
    ticker = db.execute(select(Ticker).where(Ticker.symbol == "AAPL")).scalar_one()
    user = db.execute(select(User)).scalars().first() or User(
        email="x@example.com", notify_email="x@example.com", notify_email_confirmed=True,
    )
    alert = _Alert(user=user, ticker=ticker, pct=6.4, price=106.4,
                   prev_close=None, summary="test")
    stories, skipped = _stories_for(db, [alert])
    db.close()

    assert not stories
    assert skipped[ticker.id].reason == "no_prev_close"


# ---------------------------------------------------------------------------
# A pause must not withhold work already paid for
# ---------------------------------------------------------------------------


def test_a_paused_provider_still_hands_back_a_finished_story(client, stories_on, monkeypatch):
    """The pause exists to stop new AI calls, not to hide stories already
    written. The early return it replaced withheld both."""
    _, ticker_id = _setup_user_with_range_rule("AAPL", -5.0, 5.0)
    _seed_prior_day_close(ticker_id, close=100.0)
    run_intraday_capture(fetcher=_fetch({"AAPL": 106.4}), force=True)

    db = get_session_factory()()
    article = db.execute(select(StockArticle)).scalars().one()
    assert article.status == "ready"
    ticker = db.get(Ticker, ticker_id)

    monkeypatch.setattr(
        service, "_paused_until", {"anthropic": utcnow() + timedelta(minutes=30)},
    )
    assert service.stories_paused() is True

    plan = service.ensure_articles(
        db, [service.Candidate(ticker, 6.4, 106.4, 100.0)],
    )
    db.close()
    assert plan.stories.get(ticker_id) is not None, "a finished story was withheld"
    assert not plan.skipped


def test_a_pause_blocks_a_new_story_and_says_so(client, stories_on, monkeypatch):
    from app.db.seed import seed

    seed()
    db = get_session_factory()()
    ticker = db.execute(select(Ticker).where(Ticker.symbol == "AAPL")).scalar_one()
    monkeypatch.setattr(
        service, "_paused_until", {"anthropic": utcnow() + timedelta(minutes=30)},
    )
    plan = service.ensure_articles(db, [service.Candidate(ticker, 6.4, 106.4, 100.0)])
    db.close()
    assert not plan.stories
    assert plan.skipped[ticker.id].reason == "provider_paused"


# ---------------------------------------------------------------------------
# What the dashboard reads
# ---------------------------------------------------------------------------


def test_the_report_states_the_reason_rather_than_guessing(client, stories_on, monkeypatch):
    monkeypatch.setattr(
        service, "generate_article",
        lambda req: GenerationResult(status="no_clear_cause", model="fake"),
    )
    _, ticker_id = _setup_user_with_range_rule("AAPL", -5.0, 5.0)
    _seed_prior_day_close(ticker_id, close=100.0)
    run_intraday_capture(fetcher=_fetch({"AAPL": 106.4}), force=True)

    db = get_session_factory()()
    report = diagnostics.delivery_report(service.trading_date(), db)
    db.close()

    alert = report["alerts"][0]
    assert alert["inferred"] is False, "the reason was recorded, not deduced"
    assert alert["reason"] == "no_clear_cause"
    assert alert["explain"]["causes"], "a row must expand into candidate causes"
    assert alert["explain"]["ok"] is False

    stories = report["stories"]
    assert stories["emails"] == 1
    assert stories["with_story"] == 0
    assert stories["nothing_for_kids"] == 1
    assert stories["by_reason"] == {"no_clear_cause": 1}
    assert "1 had nothing for kids" in stories["summary"]
    assert stories["missed"][0]["explain"]["fix"]


def test_a_quiet_day_reads_as_healthy_not_as_a_failure(client, stories_on):
    """The free small-move note is the system working. If the summary called
    it a miss the dashboard would cry wolf on most days of the year."""
    _, ticker_id = _setup_user_with_range_rule("AAPL", -0.5, 0.5)
    _seed_prior_day_close(ticker_id, close=100.0)
    run_intraday_capture(fetcher=_fetch({"AAPL": 100.8}), force=True)

    db = get_session_factory()()
    report = diagnostics.delivery_report(service.trading_date(), db)
    db.close()

    stories = report["stories"]
    assert stories["nothing_for_kids"] == 0
    assert stories["with_fixed_note"] == 1
    assert "Nothing was unexplained" in stories["summary"]


def test_findings_flag_undelivered_stories_with_causes(client, stories_on, monkeypatch):
    monkeypatch.setattr(
        service, "generate_article",
        lambda req: GenerationResult(status="failed", error="boom", model="fake"),
    )
    _, ticker_id = _setup_user_with_range_rule("AAPL", -5.0, 5.0)
    _seed_prior_day_close(ticker_id, close=100.0)
    run_intraday_capture(fetcher=_fetch({"AAPL": 106.4}), force=True)

    db = get_session_factory()()
    report = diagnostics.day_report(service.trading_date(), db)
    db.close()

    f = next(f for f in report["findings"] if f["code"] == "stories_not_delivered")
    assert f["causes"], "the finding must expand into causes"
    assert f["evidence"]["by_reason"]["generation_failed"] == 1
    assert f["remediation"]


# ---------------------------------------------------------------------------
# Why a job did not run
# ---------------------------------------------------------------------------


# 2026-11-03 is EST, so Eastern is UTC-5 and 09:35 ET is 14:35 UTC.
def _slot(job_id: str, et: str, utc: str, status: str = "MISSING") -> dict:
    return {
        "job_id": job_id, "expected_et": et, "expected_utc": f"2026-11-03T{utc}:00",
        "actual_utc": None, "drift_minutes": None, "status": status, "error": None,
    }


def _run(job_name: str, utc: str) -> dict:
    return {
        "job_name": job_name, "status": "SUCCESS",
        "started_at": f"2026-11-03T{utc}:00", "duration_seconds": 1.0,
        "result_summary": "", "error": None,
    }


def test_a_dead_container_is_the_first_cause_when_nothing_ran_at_all():
    jobs = {"slots": [_slot("ingest", "09:35", "14:35")], "all_runs": []}
    ev, causes = diagnostics._job_missing_evidence(jobs, jobs["slots"][0])
    assert ev["runs_recorded_today"] == 0
    assert "container was not running" in causes[0]["cause"]


def test_a_job_specific_break_is_first_when_the_process_ran_after_the_gap():
    """Same symptom, different answer, and the drill-down is the only thing
    that tells them apart: a job missing at midday while jobs ran before and
    after it cannot be a sleeping container — it is that job."""
    jobs = {
        "slots": [
            _slot("ingest", "09:35", "14:35", "SUCCESS"),
            _slot("notify", "12:30", "17:30"),
            _slot("recap", "16:05", "21:05", "SUCCESS"),
        ],
        "all_runs": [_run("ingest", "14:35"), _run("recap", "21:05")],
    }
    ev, causes = diagnostics._job_missing_evidence(jobs, jobs["slots"][1])
    assert ev["something_ran_after_this_slot"] is True
    assert ev["jobs_that_did_run"] == ["ingest", "recap"]
    assert "This job alone is broken" in causes[0]["cause"]


def test_dying_midway_is_first_when_nothing_ran_after_the_gap():
    jobs = {
        "slots": [
            _slot("ingest", "09:35", "14:35", "SUCCESS"),
            _slot("recap", "16:30", "21:30"),
        ],
        "all_runs": [_run("ingest", "14:35")],
    }
    ev, causes = diagnostics._job_missing_evidence(jobs, jobs["slots"][1])
    assert ev["something_ran_after_this_slot"] is False
    assert "died after" in causes[0]["cause"]


# ---------------------------------------------------------------------------
# Monitoring must never break a send
# ---------------------------------------------------------------------------


def test_recording_never_raises_into_the_email_path(client):
    """A monitoring row is worth less than the email it describes."""

    class Broken:
        def add(self, _row):
            raise RuntimeError("database gone")

    delivery.record(
        Broken(), user=User(id=1, email="a@b.c"), ticker=None,
        channel=delivery.CHANNEL_ALERT, trading_date=date.today(),
        outcome=delivery.StoryOutcome(delivery.STATE_NONE, "no_clear_cause"),
    )


# ---------------------------------------------------------------------------
# The API surface
# ---------------------------------------------------------------------------


def test_the_delivery_endpoint_pseudonymises_for_the_agent(client, monkeypatch):
    """Same code path as the dashboard, but the monitoring agent must never
    receive a real address: its transcripts are stored outside our systems."""
    from app.core.settings import get_settings

    monkeypatch.setenv("ADMIN_API_TOKEN", "a-sufficiently-long-service-secret-value")
    get_settings.cache_clear()
    token = client.post(
        "/admin/service-login",
        json={"secret": "a-sufficiently-long-service-secret-value"},
    ).json()["token"]

    _, ticker_id = _setup_user_with_range_rule("AAPL", -0.5, 0.5, email="p@example.com")
    _seed_prior_day_close(ticker_id, close=100.0)
    run_intraday_capture(fetcher=_fetch({"AAPL": 100.8}), force=True)

    r = client.get("/admin/delivery", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    assert body["alerts"][0]["user"].startswith("u")
    assert "p@example.com" not in r.text
    assert body["stories"]["emails"] == 1


def test_the_reason_catalogue_is_served_with_its_causes(client, monkeypatch):
    from app.core.settings import get_settings

    monkeypatch.setenv("ADMIN_API_TOKEN", "a-sufficiently-long-service-secret-value")
    get_settings.cache_clear()
    token = client.post(
        "/admin/service-login",
        json={"secret": "a-sufficiently-long-service-secret-value"},
    ).json()["token"]

    r = client.get(
        "/admin/reason-catalogue", headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    reasons = r.json()["reasons"]
    assert reasons["no_clear_cause"]["causes"], "a reason must ship its causes"
    assert reasons["below_bar"]["ok"] is True
