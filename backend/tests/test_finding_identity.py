"""Finding identity, the title catalogue, and admin triage.

Three things are guarded here, in order of how expensive they are to get
wrong:

1. **Identity.** A fingerprint must separate one missing job from another,
   or acknowledging the 09:35 pipeline silences the 16:05 one too — and the
   outage you then cannot see is the one you muted yourself.
2. **The catalogue.** Every finding code needs a title, enforced by
   scanning the source rather than by remembering, the same way
   `delivery.REASONS` and the job audit names are enforced.
3. **Expiry.** No state may silence a finding for good. An acknowledgement
   runs out; a resolved finding that recurs comes back and says so.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path

import pytest

from app.models import FindingState
from app.services import diagnostics, triage

TODAY = date(2026, 11, 4)
DIAG_SRC = Path(diagnostics.__file__)


# ---------------------------------------------------------------------------
# The title catalogue
# ---------------------------------------------------------------------------


def emitted_codes() -> set[str]:
    """Every finding code `diagnostics.findings` can produce.

    Two sources, because one is not enough. The scan catches the ordinary
    `add("high", "job_missing", ...)` call. The recap findings are raised
    in a loop over `_RECAP_FINDINGS`, so their codes are read from that
    tuple — which is exactly why the tuple holds the finding code as a
    literal instead of building it with an f-string: a code assembled at
    runtime is a code no scan can check, and it was assembled at runtime
    until this test needed to see it.
    """
    text = DIAG_SRC.read_text()
    codes = set(re.findall(r'add\(\s*[^,\n]+,\s*"([a-z_0-9]+)"', text))
    codes |= {finding_code for _, finding_code, _, _ in diagnostics._RECAP_FINDINGS}
    return codes


def test_the_scan_finds_the_codes_we_know_about():
    """Guards the regex above from quietly matching nothing.

    Without this, breaking the pattern would make every catalogue test
    pass by checking an empty set — the failure mode that makes a
    source-scanning test worse than no test at all.
    """
    found = emitted_codes()
    assert len(found) >= 18, found
    for known in ("job_missing", "job_failed", "errors_logged", "recap_no_prices"):
        assert known in found, f"{known} missing from {sorted(found)}"


@pytest.mark.parametrize("code", sorted(emitted_codes()))
def test_every_finding_code_has_a_title(code):
    assert code in diagnostics.FINDING_TITLES, (
        f"{code!r} is raised by findings() but has no entry in FINDING_TITLES. "
        "Add one: it is the heading the dashboard groups by and the only "
        "description of the finding that does not change with the day."
    )


def test_no_unused_titles():
    """A title for a code nothing raises is dead wording that reads as live."""
    extra = set(diagnostics.FINDING_TITLES) - emitted_codes()
    assert not extra, f"FINDING_TITLES describes codes nothing raises: {sorted(extra)}"


def test_titles_describe_the_class_not_one_day():
    """A title is reused across occurrences, so it must not name one of them."""
    for code, title in diagnostics.FINDING_TITLES.items():
        assert not re.search(r"\d", title), f"{code}: {title!r} contains a number"
        assert title[0].isupper(), f"{code}: {title!r} should read as a heading"


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


def test_same_code_different_subject_is_a_different_finding():
    """The one that matters: triaging one missing job must not silence the
    others. This is the whole reason the subject is part of the key."""
    a = triage.fingerprint("job_missing", "pipeline_0935")
    b = triage.fingerprint("job_missing", "pipeline_1605")
    assert a != b
    assert a == "job_missing:pipeline_0935"


def test_a_fingerprint_is_stable_across_calls():
    assert triage.fingerprint("no_closes") == triage.fingerprint("no_closes")


def test_a_fingerprint_survives_a_url_path():
    for code, subject in (
        ("job_missing", "pipeline_0935"),
        ("retention_not_enforced", "log_entries"),
        ("errors_logged", None),
    ):
        fp = triage.fingerprint(code, subject)
        assert triage.FINGERPRINT_RE.match(fp), fp


def test_a_hostile_subject_cannot_escape_the_pattern():
    """Subjects are job ids and table names today, but the fingerprint
    reaches us back as a URL segment, so it is built narrow rather than
    trusted to stay tidy."""
    fp = triage.fingerprint("job_missing", "../../etc/passwd?x=1 OR 1=1")
    assert triage.FINGERPRINT_RE.match(fp), fp
    assert "/" not in fp and "?" not in fp and " " not in fp


def test_job_findings_name_their_slot(monkeypatch):
    """`subject` has to actually be populated — a correct fingerprint
    function over an empty subject still collapses every job into one."""
    report = {
        "jobs": {
            "trading_day": True,
            "all_runs": [],
            "slots": [
                {"job_id": "pipeline_0935", "status": "MISSING",
                 "expected_et": "09:35", "expected_utc": "2026-11-04T14:35:00+00:00",
                 "actual_utc": None, "actual_et": None,
                 "drift_minutes": None, "error": None},
                {"job_id": "pipeline_1605", "status": "MISSING",
                 "expected_et": "16:05", "expected_utc": "2026-11-04T21:05:00+00:00",
                 "actual_utc": None, "actual_et": None,
                 "drift_minutes": None, "error": None},
            ],
        },
        "config": {"alert_receiver_set": True, "ai_articles_enabled": False},
        "prices": {"closes_saved": 1},
        "stories": {"count": 0, "by_status": {}},
        "delivery": {"recap_missed": [], "recap_unexplained": 0, "stories": {}},
        "followups": {"abandoned": 0},
        "errors": {"errors": 0, "by_logger": {}},
    }
    out = diagnostics.findings(report)
    missing = [f for f in out if f["code"] == "job_missing"]
    assert len(missing) == 2
    assert {f["subject"] for f in missing} == {"pipeline_0935", "pipeline_1605"}
    assert len({f["fingerprint"] for f in missing}) == 2
    assert all(f["title"] == "A scheduled job never ran" for f in missing)


# ---------------------------------------------------------------------------
# Recurrence
# ---------------------------------------------------------------------------


class _BrokenSession:
    """A session whose every statement fails.

    Needed because `Session.close()` does not break a session: it returns
    the connection to the pool and the next statement checks out another
    one. A test that closed the session to simulate a broken database was
    asserting that an empty table returns nothing.
    """

    def execute(self, *a, **kw):
        raise RuntimeError("database is unreachable")

    def add(self, *a, **kw):
        raise RuntimeError("database is unreachable")

    def commit(self):
        raise RuntimeError("database is unreachable")

    def rollback(self):
        pass


def _finding(code="no_closes", subject=None, severity="medium"):
    return {
        "code": code, "subject": subject, "severity": severity,
        "detail": f"{code} happened", "fingerprint": triage.fingerprint(code, subject),
    }


def test_first_sighting_creates_an_untriaged_row(db_session):
    triage.observe(db_session, TODAY, [_finding()])
    row = db_session.query(FindingState).one()
    assert row.state == triage.STATE_NEW
    assert row.first_seen_date == row.last_seen_date == TODAY
    assert row.days_seen == 1


def test_looking_twice_in_one_day_is_still_one_day(db_session):
    """The count has to measure how often the problem happened, not how
    often the admin refreshed the page. The dashboard calls `observe` on
    every load, so this is the ordinary case, not an edge one."""
    for _ in range(5):
        triage.observe(db_session, TODAY, [_finding()])
    assert db_session.query(FindingState).one().days_seen == 1


def test_a_new_day_advances_the_count(db_session):
    triage.observe(db_session, TODAY, [_finding()])
    triage.observe(db_session, TODAY + timedelta(days=1), [_finding()])
    row = db_session.query(FindingState).one()
    assert row.days_seen == 2
    assert row.first_seen_date == TODAY
    assert row.last_seen_date == TODAY + timedelta(days=1)
    assert triage.effective(row, TODAY + timedelta(days=1))["age_days"] == 2


def test_an_older_day_does_not_move_last_seen_backwards(db_session):
    """Reading an older day must not un-expire a snooze or rewind the age."""
    triage.observe(db_session, TODAY, [_finding()])
    triage.observe(db_session, TODAY - timedelta(days=3), [_finding()])
    row = db_session.query(FindingState).one()
    assert row.last_seen_date == TODAY
    assert row.first_seen_date == TODAY - timedelta(days=3)
    assert row.days_seen == 2, "a distinct day still counts, forward or back"


def test_observing_never_raises(db_session):
    """Bookkeeping hangs off a monitoring read; it must not take the page
    down with it. The page is wanted most when the database is unhappy."""
    assert triage.observe(db_session, TODAY, [{"no": "fingerprint"}]) == 0
    assert triage.observe(_BrokenSession(), TODAY, [_finding()]) == 0


# ---------------------------------------------------------------------------
# Expiry — the guarantee
# ---------------------------------------------------------------------------


def test_an_acknowledgement_expires(db_session):
    f = _finding()
    triage.observe(db_session, TODAY, [f])
    triage.set_state(
        db_session, fingerprint_=f["fingerprint"], state="ack",
        snooze_days=3, note="checked, not urgent", today=TODAY,
    )

    held = triage.states_for(db_session, [f["fingerprint"]], TODAY)[f["fingerprint"]]
    assert held["state"] == "ack"
    assert held["note"] == "checked, not urgent"

    later = triage.states_for(
        db_session, [f["fingerprint"]], TODAY + timedelta(days=3),
    )[f["fingerprint"]]
    assert later["state"] == triage.STATE_NEW
    assert later["snooze_expired"] is True
    assert later["note"] == "checked, not urgent", "the note outlives the snooze"


def test_an_acknowledgement_cannot_be_permanent(db_session):
    f = _finding()
    triage.observe(db_session, TODAY, [f])
    for days in (0, -1, triage.MAX_SNOOZE_DAYS + 1, 10_000):
        with pytest.raises(triage.TriageError):
            triage.set_state(
                db_session, fingerprint_=f["fingerprint"], state="ack",
                snooze_days=days, today=TODAY,
            )


def test_an_ack_with_no_window_reads_as_expired(db_session):
    """A row written by hand, or by an older version, must fail open."""
    db_session.add(FindingState(
        fingerprint="no_closes", code="no_closes", state="ack",
        snooze_until=None, first_seen_date=TODAY, last_seen_date=TODAY,
    ))
    db_session.commit()
    row = db_session.query(FindingState).one()
    assert triage.effective(row, TODAY)["state"] == triage.STATE_NEW


def test_a_resolved_finding_that_recurs_comes_back_and_says_so(db_session):
    f = _finding()
    triage.observe(db_session, TODAY, [f])
    triage.set_state(
        db_session, fingerprint_=f["fingerprint"], state="resolved", today=TODAY,
    )
    assert triage.states_for(
        db_session, [f["fingerprint"]], TODAY,
    )[f["fingerprint"]]["state"] == "resolved"

    # It happens again the next day.
    tomorrow = TODAY + timedelta(days=1)
    triage.observe(db_session, tomorrow, [f])
    back = triage.states_for(db_session, [f["fingerprint"]], tomorrow)[f["fingerprint"]]
    assert back["state"] == triage.STATE_NEW
    assert back["regressed"] is True, "silence must mean fixed, never muted"


def test_resolving_again_does_not_instantly_reopen(db_session):
    """The stale-resolve trap: if `resolved_on` were left at the first
    date, re-resolving a finding already seen today would reopen it on the
    next read and the admin could never clear it."""
    f = _finding()
    triage.observe(db_session, TODAY, [f])
    triage.set_state(db_session, fingerprint_=f["fingerprint"], state="resolved", today=TODAY)
    tomorrow = TODAY + timedelta(days=1)
    triage.observe(db_session, tomorrow, [f])
    triage.set_state(db_session, fingerprint_=f["fingerprint"], state="resolved", today=tomorrow)
    again = triage.states_for(db_session, [f["fingerprint"]], tomorrow)[f["fingerprint"]]
    assert again["state"] == "resolved"
    assert again["regressed"] is False


def test_working_stays_on_screen(db_session):
    """Something being fixed is still broken. Only this state stays."""
    f = _finding()
    triage.observe(db_session, TODAY, [f])
    triage.set_state(db_session, fingerprint_=f["fingerprint"], state="working", today=TODAY)
    [out] = triage.annotate(db_session, [f], TODAY)
    assert out["triage_state"] == "working"
    assert out["hidden"] is False

    triage.set_state(db_session, fingerprint_=f["fingerprint"], state="backlog", today=TODAY)
    [out] = triage.annotate(db_session, [f], TODAY)
    assert out["hidden"] is True


def test_a_parked_finding_is_still_in_the_payload(db_session):
    """`hidden` is advisory. Dropping the row would make the parking
    invisible to the next reader, which is the failure this module exists
    to prevent."""
    f = _finding()
    triage.observe(db_session, TODAY, [f])
    triage.set_state(
        db_session, fingerprint_=f["fingerprint"], state="ack",
        snooze_days=30, note="known", today=TODAY,
    )
    [out] = triage.annotate(db_session, [f], TODAY)
    assert out["detail"] == f["detail"]
    assert out["triage"]["note"] == "known"


def test_triaging_one_subject_leaves_the_others_alone(db_session):
    """The headline guarantee, end to end."""
    a = _finding("job_missing", "pipeline_0935", "high")
    b = _finding("job_missing", "pipeline_1605", "high")
    triage.observe(db_session, TODAY, [a, b])
    triage.set_state(
        db_session, fingerprint_=a["fingerprint"], state="ack",
        snooze_days=7, today=TODAY,
    )
    out = {f["subject"]: f for f in triage.annotate(db_session, [a, b], TODAY)}
    assert out["pipeline_0935"]["hidden"] is True
    assert out["pipeline_1605"]["hidden"] is False


def test_unknown_states_are_refused(db_session):
    for bad in ("new", "ignored", "RESOLVED", "", "drop table"):
        with pytest.raises(triage.TriageError):
            triage.set_state(
                db_session, fingerprint_="no_closes", state=bad, today=TODAY,
            )


def test_every_settable_state_is_described():
    """The catalogue the dashboard renders. A state the UI can set but
    nothing describes becomes a button nobody knows the meaning of."""
    for code, spec in triage.STATES.items():
        assert spec.label and spec.what, code
        assert len(spec.what) > 40, f"{code}: describe what it does to the finding"
    assert triage.STATE_NEW not in triage.STATES, "new is storage-only"


def test_states_degrade_to_an_untriaged_list():
    """A triage lookup that fails must leave the page showing the plain
    findings it showed before any of this existed — not an error page.
    A diagnostic tool that needs a healthy database to describe an
    unhealthy one is useless exactly when it is wanted."""
    assert triage.states_for(_BrokenSession(), ["no_closes"], TODAY) == {}

    f = _finding()
    [out] = triage.annotate(_BrokenSession(), [f], TODAY)
    assert out["detail"] == f["detail"]
    assert out["triage"] is None
    assert out["triage_state"] == triage.STATE_NEW
    assert out["hidden"] is False, "an unreadable state must not hide a finding"


# ---------------------------------------------------------------------------
# The endpoints
# ---------------------------------------------------------------------------

SECRET = "test-admin-service-secret-long-enough-value"


@pytest.fixture
def agent(client, monkeypatch):
    """The monitoring agent's credential, via the real exchange."""
    from app.core.settings import get_settings

    monkeypatch.setenv("ADMIN_API_TOKEN", SECRET)
    get_settings.cache_clear()
    token = client.post("/admin/service-login", json={"secret": SECRET}).json()["token"]
    yield {"Authorization": f"Bearer {token}"}
    get_settings.cache_clear()


@pytest.fixture
def admin(client, monkeypatch):
    """A signed-in human admin."""
    from app.core.settings import get_settings

    client.post("/auth/request-link", json={"email": "owner@example.com"})
    token = client.sent_emails[-1]["body_text"].split("token=")[1].split("\n")[0].strip()
    data = client.post("/auth/verify", json={"token": token}).json()
    monkeypatch.setenv("ADMIN_EMAILS", "owner@example.com")
    monkeypatch.setenv("ADMIN_API_TOKEN", SECRET)
    get_settings.cache_clear()
    return {"Authorization": f"Bearer {data['session_token']}"}


def test_the_agent_cannot_triage_its_own_findings(client, agent):
    """The security decision in this batch. An agent that can mark findings
    resolved can hide its own blind spots, and the only reason to trust
    this page is that what it reports is not up to the thing being
    monitored. Reading is shared; deciding is not."""
    r = client.post(
        "/admin/findings/no_closes/state",
        json={"state": "resolved"}, headers=agent,
    )
    assert r.status_code in (401, 403), r.status_code


def test_the_write_route_is_not_on_the_agent_allow_list():
    from app.api.admin_routes import SERVICE_SAFE_PATHS

    assert "/admin/findings/{fingerprint}/state" not in SERVICE_SAFE_PATHS
    # The allow-list is for GETs. Nothing on it may be a write route, and
    # the broader guarantee is that this router has exactly one.
    from app.main import create_app

    writes = {
        getattr(r, "path", "")
        for r in create_app().routes
        if "POST" in (getattr(r, "methods", None) or set())
        and getattr(r, "path", "").startswith("/admin/findings")
    }
    assert writes == {"/admin/findings/{fingerprint}/state"}, writes
    assert not (writes & SERVICE_SAFE_PATHS)


def test_an_unauthenticated_caller_cannot_triage(client):
    r = client.post("/admin/findings/no_closes/state", json={"state": "ack"})
    assert r.status_code == 401


def test_a_signed_in_non_admin_cannot_triage(client, signed_in):
    r = client.post(
        "/admin/findings/no_closes/state",
        json={"state": "ack"}, headers=signed_in["auth"],
    )
    assert r.status_code == 403


def test_an_admin_can_triage_and_it_sticks(client, admin):
    r = client.post(
        "/admin/findings/no_closes/state",
        json={"state": "ack", "snooze_days": 5, "note": "quiet week, expected"},
        headers=admin,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "ack"
    assert body["note"] == "quiet week, expected"
    assert body["snooze_until"] is not None

    # Same decision twice is a refresh, not an error.
    again = client.post(
        "/admin/findings/no_closes/state",
        json={"state": "ack", "snooze_days": 5}, headers=admin,
    )
    assert again.status_code == 200


def test_a_bad_decision_is_a_422_not_a_500(client, admin):
    for payload in (
        {"state": "ignored"},
        {"state": "new"},
        {"state": "ack", "snooze_days": 0},
        {"state": "ack", "snooze_days": 9999},
    ):
        r = client.post(
            "/admin/findings/no_closes/state", json=payload, headers=admin,
        )
        assert r.status_code == 422, (payload, r.status_code)


def test_the_state_catalogue_is_served(client, agent):
    r = client.get("/admin/triage-states", headers=agent)
    assert r.status_code == 200
    body = r.json()
    codes = {s["code"] for s in body["states"]}
    assert codes == set(triage.STATES)
    assert body["default_snooze_days"] == triage.DEFAULT_SNOOZE_DAYS
    for s in body["states"]:
        assert s["label"] and s["what"]


def test_watch_carries_identity_and_triage(client, admin):
    r = client.get("/admin/watch", headers=admin)
    assert r.status_code == 200
    for day in ("today", "yesterday"):
        for f in r.json()[day]["findings"]:
            assert f["fingerprint"], f
            assert f["title"], f
            assert "triage_state" in f and "hidden" in f, f


def test_watch_does_not_leak_the_admin_who_triaged(client, admin, agent):
    """`finding_states` stores a user id rather than an address precisely
    so the agent's view of this table cannot carry one."""
    client.post(
        "/admin/findings/no_closes/state",
        json={"state": "working", "note": "looking at it"}, headers=admin,
    )
    r = client.get("/admin/watch", headers=agent)
    assert r.status_code == 200
    assert "owner@example.com" not in r.text
