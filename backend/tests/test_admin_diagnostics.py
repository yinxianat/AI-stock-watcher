"""Admin diagnostics endpoints and the diagnostics module behind them.

The security boundary is the important part: these endpoints expose
cross-user delivery data, so an unset token must disable them rather than
open them, and a wrong token must never get through.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.settings import get_settings
from app.models import (
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
# ---------------------------------------------------------------------------


def test_disabled_when_token_unset(client, monkeypatch):
    """No token configured must mean "off", never "open"."""
    monkeypatch.setenv("ADMIN_API_TOKEN", "")
    get_settings.cache_clear()
    for path in ("/admin/diagnostics", "/admin/findings", "/admin/health-summary"):
        assert client.get(path).status_code == 503, path


def test_no_token_rejected(client, admin_on):
    assert client.get("/admin/diagnostics").status_code == 401


def test_wrong_token_rejected(client, admin_on):
    r = client.get("/admin/diagnostics", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 403


def test_non_bearer_scheme_rejected(client, admin_on):
    r = client.get("/admin/diagnostics", headers={"Authorization": f"Basic {TOKEN}"})
    assert r.status_code == 401


def test_bare_token_without_scheme_rejected(client, admin_on):
    r = client.get("/admin/diagnostics", headers={"Authorization": TOKEN})
    assert r.status_code == 401


def test_valid_token_accepted(client, admin_on):
    assert client.get("/admin/diagnostics", headers=admin_on).status_code == 200


def test_user_session_does_not_grant_admin(client, signed_in, admin_on):
    """A normal logged-in user must not reach cross-user diagnostics."""
    r = client.get("/admin/diagnostics", headers=signed_in["auth"])
    assert r.status_code in (401, 403)


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


def test_diagnostics_has_every_section(client, admin_on):
    body = client.get("/admin/diagnostics", headers=admin_on).json()
    for key in ("date", "config", "jobs", "prices", "stories", "delivery",
                "followups", "errors", "findings"):
        assert key in body, key


def test_diagnostics_accepts_a_date(client, admin_on):
    body = client.get("/admin/diagnostics?date=2026-11-02", headers=admin_on).json()
    assert body["date"] == "2026-11-02"


def test_diagnostics_rejects_a_bad_date(client, admin_on):
    assert client.get("/admin/diagnostics?date=nope", headers=admin_on).status_code == 422


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
