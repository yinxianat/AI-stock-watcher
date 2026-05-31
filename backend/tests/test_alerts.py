"""Tests for app.services.alerts and downstream event hooks."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.settings import get_settings
from app.db.database import get_session_factory
from app.models import LogEntry, NotificationEventType, Ticker, User, WatchlistItem, NotificationRule
from app.services import alerts
from app.services.alerts import (
    AlertChannel,
    DBLogHandler,
    EmailAlertHandler,
    notify_admin,
    parse_lifetime,
    reset_for_tests,
)


# -----------------------------------------------------------------------------
# parse_lifetime
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "spec,expected",
    [
        ("30d", timedelta(days=30)),
        ("24h", timedelta(hours=24)),
        ("7d", timedelta(days=7)),
        ("1M", timedelta(days=30)),
        ("1y", timedelta(days=365)),
        ("60m", timedelta(minutes=60)),
        ("30", timedelta(days=30)),  # bare integer = days
        ("2w", timedelta(weeks=2)),
    ],
)
def test_parse_lifetime_valid(spec, expected):
    assert parse_lifetime(spec) == expected


@pytest.mark.parametrize("bad", ["", "abc", "30x", "-1d", "1.5d"])
def test_parse_lifetime_invalid(bad):
    with pytest.raises(ValueError):
        parse_lifetime(bad)


# -----------------------------------------------------------------------------
# effective_alert_receiver fallback
# -----------------------------------------------------------------------------


def test_alert_receiver_falls_back_to_smtp_username(monkeypatch):
    monkeypatch.setenv("ALERT_RECEIVER", "")
    monkeypatch.setenv("SMTP_USERNAME", "ops@example.com")
    get_settings.cache_clear()
    assert get_settings().effective_alert_receiver == "ops@example.com"


def test_alert_receiver_explicit_overrides_smtp(monkeypatch):
    monkeypatch.setenv("ALERT_RECEIVER", "admin@example.com")
    monkeypatch.setenv("SMTP_USERNAME", "noreply@example.com")
    get_settings.cache_clear()
    assert get_settings().effective_alert_receiver == "admin@example.com"


# -----------------------------------------------------------------------------
# notify_admin
# -----------------------------------------------------------------------------


def test_notify_admin_sends_when_alerts_enabled(monkeypatch):
    monkeypatch.setenv("ALERTS_ENABLED", "true")
    monkeypatch.setenv("ALERT_RECEIVER", "admin@example.com")
    get_settings.cache_clear()

    captured: list[tuple] = []

    def fake_send(to, subject, body, body_html=None):
        captured.append((to, subject, body, body_html))

    from app.services import emailer
    monkeypatch.setattr(emailer, "send_email", fake_send)

    notify_admin("test_event", "hello", "world", channel=AlertChannel.EVENT)
    assert len(captured) == 1
    to, subj, body, _ = captured[0]
    assert to == "admin@example.com"
    assert subj.startswith("[AISW EVENT]")
    assert "world" in body


def test_notify_admin_silent_when_disabled(monkeypatch):
    monkeypatch.setenv("ALERTS_ENABLED", "false")
    monkeypatch.setenv("ALERT_RECEIVER", "admin@example.com")
    get_settings.cache_clear()

    from app.services import emailer

    monkeypatch.setattr(
        emailer,
        "send_email",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not be called")),
    )
    notify_admin("x", "y", "z")


def test_notify_admin_swallows_send_failures(monkeypatch):
    monkeypatch.setenv("ALERTS_ENABLED", "true")
    monkeypatch.setenv("ALERT_RECEIVER", "admin@example.com")
    get_settings.cache_clear()

    from app.services import emailer

    def boom(*a, **k):
        raise RuntimeError("smtp down")

    monkeypatch.setattr(emailer, "send_email", boom)
    # Must not raise — alerting failures must never crash callers.
    notify_admin("x", "y", "z")


def test_notify_admin_no_recipient_is_silent(monkeypatch):
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
    notify_admin("x", "y", "z")


# -----------------------------------------------------------------------------
# EmailAlertHandler
# -----------------------------------------------------------------------------


def test_email_alert_handler_only_fires_on_error(monkeypatch):
    monkeypatch.setenv("ALERTS_ENABLED", "true")
    monkeypatch.setenv("ALERT_RECEIVER", "admin@example.com")
    get_settings.cache_clear()

    captured: list[tuple] = []
    from app.services import emailer
    monkeypatch.setattr(emailer, "send_email", lambda *a, **k: captured.append(a))

    reset_for_tests()
    handler = EmailAlertHandler()
    logger = logging.getLogger("test.email_handler")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        logger.warning("warn msg")
        logger.error("err msg unique 1")
        logger.critical("crit msg unique 2")
    finally:
        logger.removeHandler(handler)

    # WARNING ignored; ERROR + CRITICAL each send once.
    assert len(captured) == 2


def test_email_alert_handler_dedups_within_window(monkeypatch):
    monkeypatch.setenv("ALERTS_ENABLED", "true")
    monkeypatch.setenv("ALERT_RECEIVER", "admin@example.com")
    get_settings.cache_clear()

    captured: list[tuple] = []
    from app.services import emailer
    monkeypatch.setattr(emailer, "send_email", lambda *a, **k: captured.append(a))

    reset_for_tests()
    handler = EmailAlertHandler()
    logger = logging.getLogger("test.dedup")
    logger.addHandler(handler)
    try:
        for _ in range(5):
            logger.error("same message every time")
    finally:
        logger.removeHandler(handler)

    assert len(captured) == 1


# -----------------------------------------------------------------------------
# DBLogHandler
# -----------------------------------------------------------------------------


def test_db_log_handler_persists_warning_and_above(client, monkeypatch):
    # `client` fixture sets up the engine + session factory swap.
    monkeypatch.setenv("LOG_DB_PERSISTENCE", "true")
    get_settings.cache_clear()

    reset_for_tests()
    handler = DBLogHandler()
    logger = logging.getLogger("test.db_handler")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        logger.info("info — should NOT persist")
        logger.warning("warn — should persist")
        logger.error("err — should persist")
    finally:
        logger.removeHandler(handler)

    db = get_session_factory()()
    rows = db.execute(
        select(LogEntry).where(LogEntry.logger == "test.db_handler")
    ).scalars().all()
    db.close()
    levels = sorted(r.level for r in rows)
    assert levels == ["ERROR", "WARNING"]


def test_db_log_handler_swallows_db_errors(monkeypatch):
    """If the DB session factory raises, the handler must not propagate."""
    monkeypatch.setenv("LOG_DB_PERSISTENCE", "true")
    get_settings.cache_clear()

    from app.db import database as db_mod

    def boom():
        raise RuntimeError("DB unavailable")

    monkeypatch.setattr(db_mod, "get_session_factory", boom)
    handler = DBLogHandler()
    logger = logging.getLogger("test.db_handler_fail")
    logger.addHandler(handler)
    try:
        # Should not raise.
        logger.error("err while DB is down")
    finally:
        logger.removeHandler(handler)


# -----------------------------------------------------------------------------
# Event hooks
# -----------------------------------------------------------------------------


def test_signup_fires_event_alert(client, monkeypatch):
    """Magic-link verify for a brand-new email should fire signup event."""
    monkeypatch.setenv("ALERTS_ENABLED", "true")
    monkeypatch.setenv("ALERT_RECEIVER", "admin@example.com")
    get_settings.cache_clear()

    captured: list[tuple] = []
    from app.services import alerts as alerts_mod

    def fake(event, subject, body, body_html=None, channel=None):
        captured.append((event, subject, body))

    monkeypatch.setattr(alerts_mod, "notify_admin", fake)
    # auth.py imported alert_event_signup, which references notify_admin via
    # the alerts module — rebind the reference there too.
    from app.services import auth as auth_mod

    def fake_signup(email):
        alerts_mod.notify_admin("user_signup", f"signup {email}", email)

    monkeypatch.setattr(auth_mod, "alert_event_signup", fake_signup)

    r = client.post("/auth/request-link", json={"email": "newbie@example.com"})
    assert r.status_code == 204
    body = client.sent_emails[-1]["body_text"]
    token = body.split("token=")[1].split("\n")[0].strip()
    r = client.post("/auth/verify", json={"token": token})
    assert r.status_code == 200

    assert any(e[0] == "user_signup" for e in captured), captured


def test_signup_only_fires_once_per_user(client, monkeypatch):
    """Second sign-in for the same email should NOT fire signup again."""
    captured: list[tuple] = []
    from app.services import alerts as alerts_mod
    from app.services import auth as auth_mod

    def fake_signup(email):
        captured.append(("user_signup", email))

    monkeypatch.setattr(auth_mod, "alert_event_signup", fake_signup)

    # First sign-in.
    client.post("/auth/request-link", json={"email": "repeat@example.com"})
    body = client.sent_emails[-1]["body_text"]
    token = body.split("token=")[1].split("\n")[0].strip()
    client.post("/auth/verify", json={"token": token})

    # Second sign-in.
    client.post("/auth/request-link", json={"email": "repeat@example.com"})
    body = client.sent_emails[-1]["body_text"]
    token = body.split("token=")[1].split("\n")[0].strip()
    client.post("/auth/verify", json={"token": token})

    assert len(captured) == 1
