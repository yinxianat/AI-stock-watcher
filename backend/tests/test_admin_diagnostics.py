"""Admin diagnostics endpoints and the diagnostics module behind them.

The security boundary is the important part: these endpoints expose
cross-user delivery data, so an unset token must disable them rather than
open them, and a wrong token must never get through.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.settings import get_settings
from app.models import (
    ArticleFollowup,
    DailyClose,
    JobRun,
    NotificationEventType,
    NotificationLog,
    RecapSend,
    StockArticle,
    Ticker,
    TickerType,
    User,
    WatchlistItem,
)
from app.services import diagnostics

TOKEN = "test-admin-token-long-enough"
DAY = date(2026, 11, 2)  # a Monday


@pytest.fixture
def admin_on(monkeypatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", TOKEN)
    get_settings.cache_clear()
    yield {"Authorization": f"Bearer {TOKEN}"}
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Auth boundary
#
# Two kinds of caller, deliberately kept apart:
#   * a human admin — session token + ADMIN_EMAILS or an admin_roles row;
#     reaches everything, sees real addresses.
#   * the monitoring agent — ADMIN_API_TOKEN; reaches only the paths in
#     SERVICE_SAFE_PATHS, and sees pseudonyms.
# ---------------------------------------------------------------------------

HUMAN_ONLY_PATHS = ("/admin/diagnostics", "/admin/health-summary")


@pytest.fixture
def admin_session(client, monkeypatch):
    """A signed-in user who is an admin via ADMIN_EMAILS."""
    client.post("/auth/request-link", json={"email": "owner@example.com"})
    token = client.sent_emails[-1]["body_text"].split("token=")[1].split("\n")[0].strip()
    data = client.post("/auth/verify", json={"token": token}).json()
    monkeypatch.setenv("ADMIN_EMAILS", "owner@example.com")
    get_settings.cache_clear()
    return {"Authorization": f"Bearer {data['session_token']}"}


def test_service_access_disabled_when_token_unset(client, monkeypatch):
    """No service credential configured must mean "off", never "open"."""
    monkeypatch.setenv("ADMIN_API_TOKEN", "")
    get_settings.cache_clear()
    r = client.get("/admin/watch", headers={"Authorization": "Bearer anything"})
    assert r.status_code == 503


def test_no_auth_rejected(client, admin_on):
    for path in ("/admin/watch", "/admin/diagnostics"):
        assert client.get(path).status_code == 401, path


def test_wrong_token_rejected(client, admin_on):
    r = client.get("/admin/watch", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 403


def test_non_bearer_scheme_rejected(client, admin_on):
    r = client.get("/admin/watch", headers={"Authorization": f"Basic {TOKEN}"})
    assert r.status_code == 401


def test_service_token_reaches_every_allow_listed_path(client, admin_on):
    from app.api.admin_routes import SERVICE_SAFE_PATHS

    for path in sorted(SERVICE_SAFE_PATHS):
        assert client.get(path, headers=admin_on).status_code == 200, path


def test_service_token_cannot_reach_human_only_paths(client, admin_on):
    """The firehose endpoints carry unfiltered per-user data, so the agent's
    credential must not open them even though it is a valid credential."""
    for path in HUMAN_ONLY_PATHS:
        r = client.get(path, headers=admin_on)
        assert r.status_code in (401, 403), f"{path} → {r.status_code}"


def test_admin_session_reaches_everything(client, admin_session):
    from app.api.admin_routes import SERVICE_SAFE_PATHS

    for path in sorted(set(SERVICE_SAFE_PATHS) | set(HUMAN_ONLY_PATHS)):
        assert client.get(path, headers=admin_session).status_code == 200, path


def test_signed_in_non_admin_is_refused(client, signed_in, admin_on, monkeypatch):
    """An ordinary user with a valid session must not reach diagnostics."""
    monkeypatch.setenv("ADMIN_EMAILS", "someone-else@example.com")
    get_settings.cache_clear()
    for path in ("/admin/watch", "/admin/diagnostics"):
        assert client.get(path, headers=signed_in["auth"]).status_code == 403, path


def test_admin_role_row_grants_access_without_a_deploy(client, signed_in, monkeypatch):
    """The point of the table: granting a second operator is one INSERT."""
    monkeypatch.setenv("ADMIN_EMAILS", "")
    get_settings.cache_clear()
    auth = signed_in["auth"]
    assert client.get("/admin/watch", headers=auth).status_code == 403

    from app.db.database import get_session_factory
    from app.models import AdminRole
    db = get_session_factory()()
    db.add(AdminRole(user_id=signed_in["user"]["id"], role="admin"))
    db.commit()
    db.close()

    assert client.get("/admin/watch", headers=auth).status_code == 200


def test_admin_emails_works_without_any_db_row(client, admin_session):
    """The bootstrap: losing DB access can't lock the owner out."""
    from app.db.database import get_session_factory
    from app.models import AdminRole
    db = get_session_factory()()
    assert db.query(AdminRole).count() == 0
    db.close()
    assert client.get("/admin/diagnostics", headers=admin_session).status_code == 200


def test_admin_emails_matching_is_case_insensitive(client, monkeypatch):
    client.post("/auth/request-link", json={"email": "mixed@example.com"})
    token = client.sent_emails[-1]["body_text"].split("token=")[1].split("\n")[0].strip()
    data = client.post("/auth/verify", json={"token": token}).json()
    monkeypatch.setenv("ADMIN_EMAILS", "MIXED@Example.COM")
    get_settings.cache_clear()
    auth = {"Authorization": f"Bearer {data['session_token']}"}
    assert client.get("/admin/watch", headers=auth).status_code == 200


# --- the capability Caller buys: one endpoint, two views -------------------


def test_same_endpoint_shows_emails_to_a_human_and_pseudonyms_to_the_agent(
    client, admin_session, admin_on, db_session,
):
    """Scope follows identity rather than being hardcoded per route, so the
    dashboard stays useful without the agent ever seeing an address."""
    _seed_identifiable(db_session)

    human = client.get("/admin/watch", headers=admin_session)
    agent = client.get("/admin/watch", headers=admin_on)
    assert human.status_code == 200 and agent.status_code == 200
    assert not EMAIL_RE.search(agent.text), "agent saw an address"
    # The human view is allowed to contain addresses; what matters is that
    # the two differ, i.e. the scope really did follow the caller.
    assert human.text != agent.text


# ---------------------------------------------------------------------------
# Timezone handling — the thing most likely to be silently wrong
# ---------------------------------------------------------------------------


def test_day_bounds_covers_eastern_day_in_utc():
    start, end = diagnostics.day_bounds(DAY)
    # 2026-11-02 is EST (UTC-5): midnight ET == 05:00 UTC same day.
    assert start == datetime(2026, 11, 2, 5, 0)
    assert end == datetime(2026, 11, 3, 5, 0)
    assert start.tzinfo is None and end.tzinfo is None  # naive, like the DB


def test_day_bounds_handles_dst_offset():
    """A July day is EDT (UTC-4), so the window shifts by an hour."""
    start, _ = diagnostics.day_bounds(date(2026, 7, 1))
    assert start == datetime(2026, 7, 1, 4, 0)


# ---------------------------------------------------------------------------
# Job drift — catches the Pacific/Eastern class of bug
# ---------------------------------------------------------------------------


def _job(db, name: str, started: datetime, status: str = "SUCCESS") -> None:
    db.add(JobRun(job_name=name, status=status, started_at=started, duration_seconds=1.0))
    db.commit()


def test_job_on_time_has_no_drift(db_session, monkeypatch):
    monkeypatch.setenv("BATCH_JOB_TIMES_ET", "09:35")
    monkeypatch.setenv("RECAP_ENABLED", "false")
    monkeypatch.setenv("DAILY_SUMMARY_ENABLED", "false")
    get_settings.cache_clear()
    # 09:35 ET == 14:35 UTC on this date.
    _job(db_session, "pipeline_0935", datetime(2026, 11, 2, 14, 35))
    rep = diagnostics.jobs_report(DAY, db_session)
    slot = next(s for s in rep["slots"] if s["job_id"] == "pipeline_0935")
    assert slot["status"] == "SUCCESS"
    assert slot["drift_minutes"] == 0


def test_pacific_drift_is_flagged_as_a_finding(db_session, monkeypatch):
    """A job firing at 09:35 Pacific instead of Eastern is 180 min late and
    must be reported, since that is exactly the bug we shipped once."""
    monkeypatch.setenv("BATCH_JOB_TIMES_ET", "09:35")
    monkeypatch.setenv("RECAP_ENABLED", "false")
    monkeypatch.setenv("DAILY_SUMMARY_ENABLED", "false")
    get_settings.cache_clear()
    _job(db_session, "pipeline_0935", datetime(2026, 11, 2, 17, 35))  # 09:35 PT
    rep = diagnostics.jobs_report(DAY, db_session)
    slot = next(s for s in rep["slots"] if s["job_id"] == "pipeline_0935")
    assert slot["drift_minutes"] == 180
    found = diagnostics.findings({
        "jobs": rep,
        "config": {"alert_receiver_set": True, "ai_articles_enabled": False},
        "prices": {"closes_saved": 1},
        "stories": {"count": 0, "by_status": {}},
        "followups": {"abandoned": 0},
        "delivery": {"recap_unexplained": 0, "recap_missed": []},
        "errors": {"errors": 0, "by_logger": {}},
    })
    drift = [f for f in found if f["code"] == "job_drift"]
    assert drift and drift[0]["severity"] == "high"
    assert "Pacific" in drift[0]["remediation"]


def test_missing_job_is_flagged(db_session, monkeypatch):
    monkeypatch.setenv("BATCH_JOB_TIMES_ET", "09:35")
    monkeypatch.setenv("RECAP_ENABLED", "false")
    monkeypatch.setenv("DAILY_SUMMARY_ENABLED", "false")
    get_settings.cache_clear()
    rep = diagnostics.jobs_report(DAY, db_session)  # nothing recorded
    slot = next(s for s in rep["slots"] if s["job_id"] == "pipeline_0935")
    assert slot["status"] == "MISSING"


def test_weekend_expects_no_weekday_jobs(db_session, monkeypatch):
    monkeypatch.setenv("BATCH_JOB_TIMES_ET", "09:35")
    get_settings.cache_clear()
    rep = diagnostics.jobs_report(date(2026, 11, 1), db_session)  # Sunday
    assert rep["trading_day"] is False
    assert not [s for s in rep["slots"] if s["job_id"] == "pipeline_0935"]


# ---------------------------------------------------------------------------
# Per-user delivery: who got what, and who didn't
# ---------------------------------------------------------------------------


def _user(db, email: str, confirmed: bool = True) -> User:
    u = User(email=email, notify_email=email, notify_email_confirmed=confirmed)
    db.add(u)
    db.commit()
    return u


def _ticker(db, symbol: str = "SPY") -> Ticker:
    """`db_session` runs on a bare engine with no seed, so make our own."""
    t = db.execute(select(Ticker).where(Ticker.symbol == symbol)).scalar_one_or_none()
    if t is None:
        t = Ticker(symbol=symbol, name=f"{symbol} Fund", type=TickerType.ETF)
        db.add(t)
        db.commit()
    return t


def test_missed_recap_reports_unconfirmed_email(db_session):
    _user(db_session, "pending@example.com", confirmed=False)
    rep = diagnostics.delivery_report(DAY, db_session)
    miss = next(m for m in rep["recap_missed"] if m["user"] == "pending@example.com")
    assert miss["reason"] == "email_unconfirmed"
    assert miss["remediation"]  # a fix is always suggested


def test_missed_recap_reports_empty_watchlist(db_session):
    _user(db_session, "empty@example.com")
    rep = diagnostics.delivery_report(DAY, db_session)
    miss = next(m for m in rep["recap_missed"] if m["user"] == "empty@example.com")
    assert miss["reason"] == "no_watchlist"


def test_eligible_user_with_no_recap_is_unexplained(db_session):
    """A confirmed user who watches tickers but got no recap is the case
    that needs a human: no gate explains it."""
    u = _user(db_session, "should@example.com")
    t = _ticker(db_session)
    db_session.add(WatchlistItem(user_id=u.id, ticker_id=t.id))
    db_session.commit()
    rep = diagnostics.delivery_report(DAY, db_session)
    miss = next(m for m in rep["recap_missed"] if m["user"] == "should@example.com")
    assert miss["reason"] == "unexplained"
    assert rep["recap_unexplained"] == 1


def test_user_who_got_recap_is_not_reported_missed(db_session):
    u = _user(db_session, "got@example.com")
    db_session.add(RecapSend(user_id=u.id, trading_date=DAY))
    db_session.commit()
    rep = diagnostics.delivery_report(DAY, db_session)
    assert not [m for m in rep["recap_missed"] if m["user"] == "got@example.com"]
    assert [r for r in rep["recaps"] if r["user"] == "got@example.com"]


def test_alert_rows_carry_story_status(db_session):
    u = _user(db_session, "alerted@example.com")
    t = _ticker(db_session)
    db_session.add(StockArticle(
        ticker_id=t.id, trading_date=DAY, direction="down", status="ready",
        move_pct=-2.0, price=100.0, prev_close=102.0, content='{"facts":[{}]}',
    ))
    db_session.add(NotificationLog(
        user_id=u.id, ticker_id=t.id,
        event_type=NotificationEventType.PRICE_CHANGE_RANGE,
        sent_to=u.notify_email, summary="down 2%",
        sent_at=datetime(2026, 11, 2, 18, 0),
    ))
    db_session.commit()
    rep = diagnostics.delivery_report(DAY, db_session)
    row = next(r for r in rep["alerts"] if r["user"] == "alerted@example.com")
    assert row["story_status"] == "ready"
    assert row["reason"] == "ready"
    assert row["inferred"] is True  # honest about being an inference


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------


def test_missing_alert_receiver_is_a_high_finding():
    found = diagnostics.findings({
        "jobs": {"trading_day": False, "slots": []},
        "config": {"alert_receiver_set": False, "ai_articles_enabled": False},
        "prices": {"closes_saved": 0},
        "stories": {"count": 0, "by_status": {}},
        "followups": {"abandoned": 0},
        "delivery": {"recap_unexplained": 0, "recap_missed": []},
        "errors": {"errors": 0, "by_logger": {}},
    })
    f = next(f for f in found if f["code"] == "no_alert_receiver")
    assert f["severity"] == "high"


def test_mostly_no_cause_is_flagged():
    found = diagnostics.findings({
        "jobs": {"trading_day": True, "slots": []},
        "config": {"alert_receiver_set": True, "ai_articles_enabled": True},
        "prices": {"closes_saved": 5},
        "stories": {"count": 4, "by_status": {"no_clear_cause": 3, "ready": 1}},
        "followups": {"abandoned": 0},
        "delivery": {"recap_unexplained": 0, "recap_missed": []},
        "errors": {"errors": 0, "by_logger": {}},
    })
    assert any(f["code"] == "mostly_no_cause" for f in found)


def test_findings_sorted_high_first():
    found = diagnostics.findings({
        "jobs": {"trading_day": False, "slots": []},
        "config": {"alert_receiver_set": False, "ai_articles_enabled": False},
        "prices": {"closes_saved": 1},
        "stories": {"count": 0, "by_status": {}},
        "followups": {"abandoned": 2},
        "delivery": {"recap_unexplained": 0, "recap_missed": []},
        "errors": {"errors": 0, "by_logger": {}},
    })
    sev = [f["severity"] for f in found]
    assert sev == sorted(sev, key=lambda s: {"high": 0, "medium": 1, "low": 2}[s])


def test_clean_day_produces_no_findings():
    found = diagnostics.findings({
        "jobs": {"trading_day": True, "slots": [
            {"job_id": "pipeline_0935", "status": "SUCCESS", "drift_minutes": 0,
             "expected_et": "09:35", "error": None},
        ]},
        "config": {"alert_receiver_set": True, "ai_articles_enabled": True},
        "prices": {"closes_saved": 10},
        "stories": {"count": 2, "by_status": {"ready": 2}},
        "followups": {"abandoned": 0},
        "delivery": {"recap_unexplained": 0, "recap_missed": []},
        "errors": {"errors": 0, "by_logger": {}},
    })
    assert found == []


# ---------------------------------------------------------------------------
# Endpoint shape
# ---------------------------------------------------------------------------


def test_diagnostics_has_every_section(client, admin_session):
    body = client.get("/admin/diagnostics", headers=admin_session).json()
    for key in ("date", "config", "jobs", "prices", "stories", "delivery",
                "followups", "errors", "findings"):
        assert key in body, key


def test_diagnostics_accepts_a_date(client, admin_session):
    body = client.get("/admin/diagnostics?date=2026-11-02", headers=admin_session).json()
    assert body["date"] == "2026-11-02"


def test_diagnostics_rejects_a_bad_date(client, admin_session):
    assert client.get(
        "/admin/diagnostics?date=nope", headers=admin_session).status_code == 422


def test_findings_endpoint_is_ranked_and_counted(client, admin_on):
    body = client.get("/admin/findings?days=3", headers=admin_on).json()
    assert {"from", "to", "count", "high", "findings"} <= set(body)
    assert body["count"] == len(body["findings"])


def test_findings_window_is_capped(client, admin_on):
    assert client.get("/admin/findings?days=999", headers=admin_on).status_code == 422


def test_reason_catalogue_is_served(client, admin_on):
    body = client.get("/admin/reason-catalogue", headers=admin_on).json()
    assert "ready" in body["help"]
    assert "no_clear_cause" in body["remediation"]


# ---------------------------------------------------------------------------
# Storage, retention and log noise (cost control)
# ---------------------------------------------------------------------------


def test_storage_report_lists_growth_tables(db_session):
    rep = diagnostics.storage_report(db_session)
    names = {t["table"] for t in rep["tables"]}
    assert {"info_logs", "log_entries", "job_runs", "intraday_prices"} <= names
    assert "total_mb" in rep and "log_quality" in rep


def test_retention_setting_is_resolved(db_session):
    rep = diagnostics.storage_report(db_session)
    info = next(t for t in rep["tables"] if t["table"] == "info_logs")
    assert info["retention_setting"] == "info_log_lifetime"
    assert info["retention_days"] == 7.0  # INFO_LOG_LIFETIME default "7d"


def test_tables_without_retention_are_flagged_unbounded(db_session):
    rep = diagnostics.storage_report(db_session)
    nl = next(t for t in rep["tables"] if t["table"] == "notification_logs")
    assert nl["unbounded"] is True
    assert nl["projected_steady_rows"] is None


def test_stale_rows_mean_retention_not_enforced(db_session):
    """A row older than its retention window means cleanup has stopped."""
    from app.models import InfoLog
    db_session.add(InfoLog(
        created_at=datetime.utcnow() - timedelta(days=90),
        level="INFO", logger="app.jobs.ingest", message="ancient",
    ))
    db_session.commit()
    rep = diagnostics.storage_report(db_session)
    info = next(t for t in rep["tables"] if t["table"] == "info_logs")
    assert info["retention_enforced"] is False
    found = diagnostics.findings({
        "jobs": {"trading_day": False, "slots": []},
        "config": {"alert_receiver_set": True, "ai_articles_enabled": False},
        "prices": {"closes_saved": 1}, "stories": {"count": 0, "by_status": {}},
        "followups": {"abandoned": 0},
        "delivery": {"recap_unexplained": 0, "recap_missed": []},
        "errors": {"errors": 0, "by_logger": {}},
        "storage": rep,
    })
    assert any(f["code"] == "retention_not_enforced" for f in found)


def test_repeated_messages_collapse_to_one_shape(db_session):
    """Digits are normalised, so the same sentence with different counters
    is recognised as one repeated line rather than many unique ones."""
    from app.models import InfoLog
    for i in range(300):
        db_session.add(InfoLog(
            level="INFO", logger="app.jobs.ingest",
            message=f"Ingested {i} tickers in {i * 2}ms",
        ))
    db_session.commit()
    q = diagnostics.log_quality(db_session)["info_logs"]
    assert q["distinct_shapes"] == 1
    assert q["duplicate_ratio"] > 0.99
    assert q["top_messages"][0]["pct_of_sample"] == 100.0


def test_noisy_log_is_flagged_with_the_offending_line(db_session):
    from app.models import InfoLog
    for i in range(250):
        db_session.add(InfoLog(level="INFO", logger="app.jobs.intraday",
                               message=f"Keepalive self-ping ok {i}"))
    db_session.commit()
    rep = diagnostics.storage_report(db_session)
    found = diagnostics.findings({
        "jobs": {"trading_day": False, "slots": []},
        "config": {"alert_receiver_set": True, "ai_articles_enabled": False},
        "prices": {"closes_saved": 1}, "stories": {"count": 0, "by_status": {}},
        "followups": {"abandoned": 0},
        "delivery": {"recap_unexplained": 0, "recap_missed": []},
        "errors": {"errors": 0, "by_logger": {}},
        "storage": rep,
    })
    noise = next(f for f in found if f["code"] == "log_mostly_noise")
    assert "Keepalive" in noise["detail"]
    assert "DEBUG" in noise["remediation"]


def test_varied_messages_are_not_flagged_as_noise(db_session):
    from app.models import InfoLog
    for i in range(250):
        db_session.add(InfoLog(level="INFO", logger="app.jobs.x",
                               message=f"distinct event kind {chr(65 + i % 26)}{i} happened here"))
    db_session.commit()
    q = diagnostics.log_quality(db_session)["info_logs"]
    assert q["duplicate_ratio"] < 0.9


# ---------------------------------------------------------------------------
# /admin/watch — the agent's entry point
# ---------------------------------------------------------------------------


def test_watch_splits_today_and_yesterday(client, admin_on):
    body = client.get("/admin/watch", headers=admin_on).json()
    assert body["today"]["urgency"] in ("act_now", "ok")
    assert body["yesterday"]["urgency"] == "review"
    assert body["today"]["date"] > body["yesterday"]["date"]
    assert "storage" in body


def test_watch_act_now_holds_only_high_severity(client, admin_on):
    body = client.get("/admin/watch", headers=admin_on).json()
    assert all(f["severity"] == "high" for f in body["today"]["act_now"])
    if body["today"]["act_now"]:
        assert body["today"]["urgency"] == "act_now"


def test_watch_does_not_flag_jobs_whose_slot_has_not_arrived(client, admin_on):
    """A morning check must not report the 16:05 batch as missing."""
    body = client.get("/admin/watch", headers=admin_on).json()
    missing = {f["detail"].split()[0] for f in body["today"]["findings"]
               if f["code"] == "job_missing"}
    assert not (missing & set(body["today"]["not_due_yet"]))


def test_storage_endpoint(client, admin_on):
    body = client.get("/admin/storage", headers=admin_on).json()
    assert "tables" in body and "log_quality" in body


# ---------------------------------------------------------------------------
# ML corpus preservation — stories are labels, prices are features
# ---------------------------------------------------------------------------


def test_price_retention_outlives_stories_by_default():
    """The default must not prune features out from under the labels."""
    from app.services.alerts import parse_lifetime
    get_settings.cache_clear()
    days = parse_lifetime(get_settings().price_history_lifetime).days
    assert days >= 3650, "price history must be kept for years for training"


def test_feedback_labels_are_counted(db_session):
    from app.models import ContentFeedback
    u = _user(db_session, "rater@example.com")
    for i, rating in enumerate(("like", "like", "dislike")):
        db_session.add(ContentFeedback(
            user_id=u.id, content_type="stock_article",
            content_id=f"AAPL:2026-11-0{i + 1}", rating=rating,
        ))
    db_session.commit()
    rep = diagnostics.ml_corpus_report(db_session)
    assert rep["feedback_total"] == 3
    assert sum(rep["feedback_labels"].values()) == 3


def test_story_older_than_price_history_is_reported_as_lost(db_session):
    """The silent, irreversible failure: label kept, features pruned."""
    from app.models import DailyClose
    t = _ticker(db_session)
    db_session.add(StockArticle(
        ticker_id=t.id, trading_date=date(2024, 1, 10), direction="up",
        status="ready", move_pct=3.0, price=100.0, prev_close=97.0,
        content='{"story":{},"facts":[]}',
    ))
    db_session.add(DailyClose(
        ticker_id=t.id, date=date(2026, 1, 10), close=120.0,
    ))
    db_session.commit()
    rep = diagnostics.ml_corpus_report(db_session)
    assert rep["stories_without_price_context"] == 1
    found = diagnostics.findings({
        "jobs": {"trading_day": False, "slots": []},
        "config": {"alert_receiver_set": True, "ai_articles_enabled": False},
        "prices": {"closes_saved": 1}, "stories": {"count": 0, "by_status": {}},
        "followups": {"abandoned": 0},
        "delivery": {"recap_unexplained": 0, "recap_missed": []},
        "errors": {"errors": 0, "by_logger": {}},
        "ml_corpus": rep,
    })
    loss = next(f for f in found if f["code"] == "ml_data_loss")
    assert loss["severity"] == "high"
    assert "irreversible" in loss["remediation"]


def test_aligned_history_reports_no_loss(db_session):
    from app.models import DailyClose
    t = _ticker(db_session)
    db_session.add(StockArticle(
        ticker_id=t.id, trading_date=date(2026, 6, 1), direction="up",
        status="ready", move_pct=3.0, price=100.0, prev_close=97.0,
        content='{"story":{},"facts":[]}',
    ))
    db_session.add(DailyClose(ticker_id=t.id, date=date(2026, 1, 1), close=90.0))
    db_session.commit()
    rep = diagnostics.ml_corpus_report(db_session)
    assert rep["stories_without_price_context"] == 0


def test_ml_corpus_endpoint(client, admin_on):
    body = client.get("/admin/ml-corpus", headers=admin_on).json()
    assert {"labelled_stories", "feedback_total", "price_retention"} <= set(body)


# ---------------------------------------------------------------------------
# Agent data access: pseudonyms, and the PII regression guard
# ---------------------------------------------------------------------------

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _seed_identifiable(db):
    """A user with an email, a watchlist, an alert and a missed recap."""
    u = _user(db, "identifiable@example.com")
    t = _ticker(db)
    db.add(WatchlistItem(user_id=u.id, ticker_id=t.id))
    db.add(StockArticle(
        ticker_id=t.id, trading_date=DAY, direction="down", status="ready",
        move_pct=-2.0, price=100.0, prev_close=102.0, content='{"facts":[{}]}',
    ))
    db.add(NotificationLog(
        user_id=u.id, ticker_id=t.id,
        event_type=NotificationEventType.PRICE_CHANGE_RANGE,
        sent_to=u.notify_email, summary="down 2%",
        sent_at=datetime(2026, 11, 2, 18, 0),
    ))
    db.commit()
    return u


def test_admin_scope_shows_real_emails(db_session):
    u = _seed_identifiable(db_session)
    rep = diagnostics.delivery_report(DAY, db_session, scope=diagnostics.SCOPE_ADMIN)
    assert any(r["user"] == u.email for r in rep["alerts"])


def test_service_scope_replaces_emails_with_a_pseudonym(db_session):
    u = _seed_identifiable(db_session)
    rep = diagnostics.delivery_report(DAY, db_session, scope=diagnostics.SCOPE_SERVICE)
    assert any(r["user"] == f"u{u.id}" for r in rep["alerts"])
    assert not EMAIL_RE.search(json.dumps(rep)), "an address survived service scope"


def test_pseudonym_is_stable_across_days(db_session):
    """Stability is the point — it lets the agent correlate the same user
    over time without ever knowing who they are."""
    u = _seed_identifiable(db_session)
    a = diagnostics.delivery_report(DAY, db_session, scope=diagnostics.SCOPE_SERVICE)
    b = diagnostics.delivery_report(
        DAY - timedelta(days=1), db_session, scope=diagnostics.SCOPE_SERVICE)
    assert diagnostics.user_ref(u, diagnostics.SCOPE_SERVICE) == f"u{u.id}"
    refs = {r["user"] for r in a["alerts"]} | {m["user"] for m in b["recap_missed"]}
    assert f"u{u.id}" in refs


def test_missed_recap_rows_carry_streak_and_watch_count(db_session):
    u = _user(db_session, "quiet@example.com")
    t = _ticker(db_session)
    db_session.add(WatchlistItem(user_id=u.id, ticker_id=t.id))
    db_session.add(RecapSend(user_id=u.id, trading_date=DAY - timedelta(days=3)))
    db_session.commit()
    rep = diagnostics.delivery_report(DAY, db_session, scope=diagnostics.SCOPE_SERVICE)
    row = next(m for m in rep["recap_missed"] if m["user"] == f"u{u.id}")
    assert row["watch_count"] == 1
    assert row["missed_days_running"] == 3  # day, -1, -2 all empty


def test_followups_honour_service_scope(db_session):
    u = _user(db_session, "waiting@example.com")
    t = _ticker(db_session)
    a = StockArticle(
        ticker_id=t.id, trading_date=DAY, direction="up", status="failed",
        move_pct=2.0, price=100.0, prev_close=98.0,
    )
    db_session.add(a)
    db_session.commit()
    db_session.add(ArticleFollowup(
        article_id=a.id, user_id=u.id, in_reply_to="<x@y>", subject="s",
        status="abandoned",
    ))
    db_session.commit()
    rep = diagnostics.followups_report(DAY, db_session, scope=diagnostics.SCOPE_SERVICE)
    assert rep["items"][0]["user"] == f"u{u.id}"
    assert not EMAIL_RE.search(json.dumps(rep))


def test_no_service_path_leaks_personal_data(client, admin_on, db_session):
    """The guard that has to survive future changes: every allow-listed path
    is scanned for anything email-shaped. Adding a PII field to a response
    turns this red rather than quietly shipping."""
    from app.api.admin_routes import SERVICE_SAFE_PATHS

    _seed_identifiable(db_session)
    for path in sorted(SERVICE_SAFE_PATHS):
        r = client.get(path, headers=admin_on)
        assert r.status_code == 200, f"{path} → {r.status_code}"
        found = EMAIL_RE.findall(r.text)
        assert not found, f"{path} leaked {found[:3]}"


def test_allow_list_excludes_the_unfiltered_endpoints():
    """/admin/diagnostics and /admin/health-summary must stay human-only."""
    from app.api.admin_routes import SERVICE_SAFE_PATHS

    assert "/admin/diagnostics" not in SERVICE_SAFE_PATHS
    assert "/admin/health-summary" not in SERVICE_SAFE_PATHS


def test_every_allow_listed_path_is_a_real_route(client, admin_on):
    """A typo in the allow-list would silently grant nothing (or worse,
    be assumed to cover an endpoint it doesn't name)."""
    from app.api.admin_routes import SERVICE_SAFE_PATHS
    from app.main import create_app

    routes = {getattr(r, "path", "") for r in create_app().routes}
    assert SERVICE_SAFE_PATHS <= routes, SERVICE_SAFE_PATHS - routes


def test_stock_articles_are_fully_visible_to_the_agent(db_session):
    """Stories are the highest-value, lowest-risk table — the agent needs
    content, status, error and token counts, and none of it is personal."""
    t = _ticker(db_session)
    db_session.add(StockArticle(
        ticker_id=t.id, trading_date=DAY, direction="up", status="failed",
        move_pct=4.0, price=100.0, prev_close=96.0, model="claude-haiku-5-5",
        input_tokens=30000, output_tokens=6000, error="reviewer rejected",
    ))
    db_session.commit()
    rep = diagnostics.stories_report(DAY, db_session)
    item = rep["items"][0]
    assert item["error"] == "reviewer rejected"
    assert item["input_tokens"] == 30000
    assert item["est_cost_usd"] is not None
    assert not EMAIL_RE.search(json.dumps(rep))
