"""The monitoring agent's credential: exchange, expiry, and refusals.

The agent holds one long-lived secret and swaps it for a short-lived token
once per run. The security properties worth pinning down here are that the
secret is *only* good for the exchange (a static bearer would defeat the
point), that issued tokens are stored hashed, that they expire, and that a
weak or missing secret fails closed rather than open.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.security import hash_session_token
from app.core.settings import get_settings
from app.db.database import get_session_factory
from app.models import ServiceSession, utcnow
from app.services import service_auth

SECRET = "a-sufficiently-long-service-secret-value"


@pytest.fixture
def service_on(client, monkeypatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", SECRET)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _login(client, secret: str = SECRET):
    return client.post("/admin/service-login", json={"secret": secret})


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# The exchange
# ---------------------------------------------------------------------------


def test_login_returns_a_short_lived_token(client, service_on):
    r = _login(client)
    assert r.status_code == 200
    body = r.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 15 * 60
    assert len(body["token"]) >= 32


def test_issued_token_works_on_allow_listed_paths(client, service_on):
    token = _login(client).json()["token"]
    from app.api.admin_routes import SERVICE_SAFE_PATHS

    for path in sorted(SERVICE_SAFE_PATHS):
        assert client.get(path, headers=_bearer(token)).status_code == 200, path


def test_issued_token_still_cannot_reach_human_only_paths(client, service_on):
    """Exchanging the secret doesn't widen what the agent may read."""
    token = _login(client).json()["token"]
    for path in ("/admin/diagnostics", "/admin/health-summary"):
        assert client.get(path, headers=_bearer(token)).status_code in (401, 403), path


def test_the_secret_itself_is_not_accepted_as_a_bearer(client, service_on):
    """The whole point of the exchange: a static bearer would travel on
    every request and never expire, so the secret must only work for login."""
    assert client.get("/admin/watch", headers=_bearer(SECRET)).status_code == 403


# ---------------------------------------------------------------------------
# Storage and expiry
# ---------------------------------------------------------------------------


def test_only_the_hash_is_stored(client, service_on):
    """A database dump must not be replayable."""
    token = _login(client).json()["token"]
    db = get_session_factory()()
    rows = db.execute(select(ServiceSession)).scalars().all()
    db.close()
    assert len(rows) == 1
    assert rows[0].token_hash == hash_session_token(token)
    assert token not in rows[0].token_hash


def test_expired_token_is_refused(client, service_on):
    token = _login(client).json()["token"]
    db = get_session_factory()()
    row = db.execute(select(ServiceSession)).scalar_one()
    row.expires_at = utcnow() - timedelta(seconds=1)
    db.commit()
    db.close()
    assert client.get("/admin/watch", headers=_bearer(token)).status_code == 403


def test_revoked_token_is_refused(client, service_on):
    token = _login(client).json()["token"]
    db = get_session_factory()()
    assert service_auth.revoke_all(db) == 1
    db.close()
    assert client.get("/admin/watch", headers=_bearer(token)).status_code == 403


def test_revoke_all_lets_a_leaked_secret_be_contained(client, service_on):
    """Rotating the secret should not leave old tokens alive."""
    first = _login(client).json()["token"]
    second = _login(client).json()["token"]
    db = get_session_factory()()
    service_auth.revoke_all(db)
    db.close()
    for t in (first, second):
        assert client.get("/admin/watch", headers=_bearer(t)).status_code == 403


def test_turning_service_access_off_kills_existing_tokens(client, service_on, monkeypatch):
    """Clearing the secret must stop tokens already issued, not just block
    new logins."""
    token = _login(client).json()["token"]
    assert client.get("/admin/watch", headers=_bearer(token)).status_code == 200
    monkeypatch.setenv("ADMIN_API_TOKEN", "")
    get_settings.cache_clear()
    assert client.get("/admin/watch", headers=_bearer(token)).status_code == 503


# ---------------------------------------------------------------------------
# Refusals — all fail closed
# ---------------------------------------------------------------------------


def test_wrong_secret_is_refused(client, service_on):
    assert _login(client, "wrong-but-also-quite-long-enough-value").status_code == 403


def test_missing_secret_disables_login(client, monkeypatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "")
    get_settings.cache_clear()
    assert _login(client).status_code == 503


def test_short_secret_is_refused_as_misconfiguration(client, monkeypatch):
    """Nobody should be able to quietly set ADMIN_API_TOKEN=test."""
    monkeypatch.setenv("ADMIN_API_TOKEN", "test")
    get_settings.cache_clear()
    r = _login(client, "test")
    assert r.status_code == 503
    assert "short" in r.json()["detail"].lower()


def test_empty_body_is_rejected(client, service_on):
    assert client.post("/admin/service-login", json={}).status_code == 422


def test_failed_login_is_logged_without_the_attempted_value(client, service_on, caplog):
    """Repeated failures should be reviewable, but the log must not become a
    record of guessed secrets."""
    import logging

    with caplog.at_level(logging.WARNING):
        _login(client, "wrong-but-also-quite-long-enough-value")
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "Service login failed" in logged
    assert "wrong-but-also" not in logged


# ---------------------------------------------------------------------------
# Notification on a new network, and silence otherwise
# ---------------------------------------------------------------------------


@pytest.fixture
def alerts_on(monkeypatch):
    """Alerts are off suite-wide; these two tests are about the email."""
    monkeypatch.setenv("ALERTS_ENABLED", "true")
    monkeypatch.setenv("ALERT_RECEIVER", "admin@example.com")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_first_login_from_a_new_network_notifies_the_admin(client, service_on, alerts_on):
    _login(client)
    subjects = [m["subject"] for m in client.sent_emails]
    assert any("new network" in s.lower() for s in subjects), subjects


def test_repeat_logins_from_a_known_network_stay_silent(client, service_on, alerts_on):
    """Three runs a day would be ~90 emails a month; that gets filtered and
    then the signal is lost. Only something new is worth telling."""
    _login(client)
    before = len(client.sent_emails)
    _login(client)
    _login(client)
    assert len(client.sent_emails) == before


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


def test_https_is_required_in_production(client, service_on, monkeypatch):
    """The secret must never cross in cleartext."""
    monkeypatch.setenv("APP_ENV", "production")
    get_settings.cache_clear()
    r = client.post(
        "/admin/service-login", json={"secret": SECRET},
        headers={"X-Forwarded-Proto": "http"},
    )
    assert r.status_code == 400
    assert "https" in r.json()["detail"].lower()


def test_forwarded_https_is_accepted_in_production(client, service_on, monkeypatch):
    """Railway terminates TLS, so the real scheme is in X-Forwarded-Proto."""
    monkeypatch.setenv("APP_ENV", "production")
    get_settings.cache_clear()
    r = client.post(
        "/admin/service-login", json={"secret": SECRET},
        headers={"X-Forwarded-Proto": "https"},
    )
    assert r.status_code == 200


def test_plain_http_is_fine_outside_production(client, service_on):
    assert _login(client).status_code == 200
