"""Edge cases for the alerting + logging plumbing.

Concurrent dedup, circuit-breaker recovery, sqlalchemy log skip, settings
fallback under empty SMTP_USERNAME, and daily-summary tolerance of NULL
last_login.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.settings import get_settings
from app.db.database import get_session_factory
from app.jobs.daily_summary import collect_summary
from app.models import LogEntry, User
from app.services import alerts as alerts_mod
from app.services.alerts import (
    DBLogHandler,
    EmailAlertHandler,
    _dedup_state,
    reset_for_tests,
)


# -----------------------------------------------------------------------------
# Dedup is thread-safe
# -----------------------------------------------------------------------------


def test_dedup_under_concurrent_emit(monkeypatch):
    """Hammering the same fingerprint from many threads must produce one alert."""
    monkeypatch.setenv("ALERTS_ENABLED", "true")
    monkeypatch.setenv("ALERT_RECEIVER", "admin@example.com")
    get_settings.cache_clear()

    sent: list[tuple] = []
    sent_lock = threading.Lock()

    def fake_send(to, subject, body, body_html=None):
        with sent_lock:
            sent.append((to, subject))

    from app.services import emailer
    monkeypatch.setattr(emailer, "send_email", fake_send)

    reset_for_tests()
    handler = EmailAlertHandler()
    logger = logging.getLogger("test.concurrent_dedup")
    logger.addHandler(handler)

    def worker():
        for _ in range(50):
            logger.error("identical error from many threads")

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    logger.removeHandler(handler)

    # 400 records → exactly one alert.
    assert len(sent) == 1


def test_dedup_distinguishes_different_messages(monkeypatch):
    monkeypatch.setenv("ALERTS_ENABLED", "true")
    monkeypatch.setenv("ALERT_RECEIVER", "admin@example.com")
    get_settings.cache_clear()

    sent: list[tuple] = []
    from app.services import emailer
    monkeypatch.setattr(emailer, "send_email", lambda *a, **k: sent.append(a))

    reset_for_tests()
    handler = EmailAlertHandler()
    logger = logging.getLogger("test.distinct_msgs")
    logger.addHandler(handler)
    try:
        logger.error("msg one")
        logger.error("msg two")
        logger.error("msg three")
    finally:
        logger.removeHandler(handler)

    assert len(sent) == 3


# -----------------------------------------------------------------------------
# Circuit breaker: trips after N consecutive failures, then suppresses
# -----------------------------------------------------------------------------


def test_db_handler_circuit_breaker_trips_after_failures(monkeypatch):
    """After 5 consecutive DB failures, the handler stops attempting writes."""
    monkeypatch.setenv("LOG_DB_PERSISTENCE", "true")
    get_settings.cache_clear()

    call_count = {"n": 0}

    def boom():
        call_count["n"] += 1
        raise RuntimeError("DB unavailable")

    from app.db import database as db_mod
    monkeypatch.setattr(db_mod, "get_session_factory", boom)

    handler = DBLogHandler()
    logger = logging.getLogger("test.cb")
    logger.addHandler(handler)
    try:
        # Fire 20 records; only the first 5 should hit get_session_factory
        # (which raises). After that the breaker should suppress.
        for _ in range(20):
            logger.error("repeated failure")
    finally:
        logger.removeHandler(handler)

    # Exactly _MAX_CONSEC_FAILURES (5) before the circuit opens.
    assert call_count["n"] == 5


def test_db_handler_circuit_breaker_recovers_after_cooldown(monkeypatch):
    monkeypatch.setenv("LOG_DB_PERSISTENCE", "true")
    get_settings.cache_clear()

    call_count = {"n": 0}

    def boom():
        call_count["n"] += 1
        raise RuntimeError("DB unavailable")

    from app.db import database as db_mod
    monkeypatch.setattr(db_mod, "get_session_factory", boom)

    handler = DBLogHandler()
    # Speed up cooldown for the test.
    monkeypatch.setattr(handler, "_COOLDOWN_SECS", 0.05)
    DBLogHandler._COOLDOWN_SECS = 0.05  # the instance setattr above is the safer one

    logger = logging.getLogger("test.cb_recover")
    logger.addHandler(handler)
    try:
        for _ in range(10):
            logger.error("trip the breaker")
        assert call_count["n"] == 5
        time.sleep(0.06)
        # After cooldown elapses, breaker re-enables and the next emit retries.
        logger.error("after cooldown")
        assert call_count["n"] == 6
    finally:
        logger.removeHandler(handler)
        DBLogHandler._COOLDOWN_SECS = 60  # restore


# -----------------------------------------------------------------------------
# DBLogHandler must skip sqlalchemy.* loggers to avoid recursion
# -----------------------------------------------------------------------------


def test_db_handler_skips_sqlalchemy_logger(client, monkeypatch):
    monkeypatch.setenv("LOG_DB_PERSISTENCE", "true")
    get_settings.cache_clear()

    reset_for_tests()
    handler = DBLogHandler()
    sa_logger = logging.getLogger("sqlalchemy.engine")
    sa_logger.addHandler(handler)
    try:
        sa_logger.warning("would recurse forever if we persisted this")
    finally:
        sa_logger.removeHandler(handler)

    db = get_session_factory()()
    rows = db.execute(
        select(LogEntry).where(LogEntry.logger.like("sqlalchemy%"))
    ).scalars().all()
    db.close()
    assert rows == []


# -----------------------------------------------------------------------------
# Empty SMTP_USERNAME AND empty ALERT_RECEIVER → silent (no crash)
# -----------------------------------------------------------------------------


def test_empty_receiver_no_crash(monkeypatch):
    monkeypatch.setenv("ALERTS_ENABLED", "true")
    monkeypatch.setenv("ALERT_RECEIVER", "")
    monkeypatch.setenv("SMTP_USERNAME", "")
    get_settings.cache_clear()

    from app.services import emailer
    monkeypatch.setattr(
        emailer,
        "send_email",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not be called")),
    )

    reset_for_tests()
    handler = EmailAlertHandler()
    logger = logging.getLogger("test.no_receiver")
    logger.addHandler(handler)
    try:
        logger.error("nobody home")  # must not crash
    finally:
        logger.removeHandler(handler)


# -----------------------------------------------------------------------------
# Daily summary tolerates users that never logged in (NULL last_login_at)
# -----------------------------------------------------------------------------


def test_collect_summary_handles_users_with_null_last_login(client):
    db = get_session_factory()()
    db.add_all([
        User(email="never-logged@example.com", notify_email="never-logged@example.com"),
        User(email="logged@example.com", notify_email="logged@example.com",
             last_login_at=datetime.utcnow() - timedelta(hours=2)),
    ])
    db.commit()
    db.close()

    data = collect_summary()
    # Both users counted in totals; only the one with last_login_at within 24h
    # counts as a login.
    assert data["db_rows"]["users"] >= 2
    assert data["auth"]["logins_24h"] == 1


# -----------------------------------------------------------------------------
# Notify_admin builds correct subject prefix per channel
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "channel,prefix",
    [
        (alerts_mod.AlertChannel.ERROR, "[AISW ALERT]"),
        (alerts_mod.AlertChannel.EVENT, "[AISW EVENT]"),
        (alerts_mod.AlertChannel.SUMMARY, "[AISW DAILY]"),
    ],
)
def test_notify_admin_subject_prefixes(monkeypatch, channel, prefix):
    monkeypatch.setenv("ALERTS_ENABLED", "true")
    monkeypatch.setenv("ALERT_RECEIVER", "x@example.com")
    get_settings.cache_clear()

    sent: list[str] = []
    from app.services import emailer
    monkeypatch.setattr(emailer, "send_email", lambda to, subject, body, body_html=None: sent.append(subject))
    alerts_mod.notify_admin("evt", "subj", "body", channel=channel)
    assert sent[0].startswith(prefix)


# -----------------------------------------------------------------------------
# Dedup GC trims stale entries
# -----------------------------------------------------------------------------


def test_dedup_gc_trims_stale_entries(monkeypatch):
    """After many fingerprints + time, the dedup dict shouldn't grow forever."""
    reset_for_tests()
    # Force the GC threshold by manually inserting old entries.
    far_past = datetime.utcnow() - timedelta(days=1)
    for i in range(100):
        _dedup_state[f"old_{i}"] = far_past
    pre = len(_dedup_state)
    assert pre == 100

    # Triggering one new fingerprint runs the cheap GC pass.
    from app.services.alerts import _recently_alerted
    _recently_alerted("brand_new")
    # The far_past entries are older than 10x the 15-min window, so they should
    # be evicted.
    remaining_old = sum(1 for k in _dedup_state if k.startswith("old_"))
    assert remaining_old == 0
