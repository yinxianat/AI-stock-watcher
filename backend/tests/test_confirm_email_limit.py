"""Repeated "Send confirmation" clicks must not flood the user's inbox."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app.db.database import get_session_factory
from app.models import ConfirmationToken


def _request(c, auth, email="alerts@example.com"):
    return c.post("/auth/notify-email", json={"new_email": email}, headers=auth)


def _confirmation_emails(c):
    return [m for m in c.sent_emails if m["subject"] == "Confirm your notification email"]


def _age_all_tokens(by: timedelta):
    """Pretend time passed. Done in Python: SQLite can't subtract an
    interval from a datetime inside an UPDATE."""
    db = get_session_factory()()
    for row in db.execute(select(ConfirmationToken)).scalars():
        row.created_at -= by
    db.commit()
    db.close()


def test_rapid_clicks_send_only_one_email(signed_in):
    c, auth = signed_in["client"], signed_in["auth"]

    assert _request(c, auth).status_code == 204
    for _ in range(5):
        r = _request(c, auth)
        assert r.status_code == 429
        assert 1 <= int(r.headers["Retry-After"]) <= 30

    assert len(_confirmation_emails(c)) == 1


def test_can_resend_after_cooldown(signed_in):
    c, auth = signed_in["client"], signed_in["auth"]
    assert _request(c, auth).status_code == 204

    _age_all_tokens(timedelta(seconds=61))

    assert _request(c, auth).status_code == 204
    assert len(_confirmation_emails(c)) == 2


def test_burst_cap(signed_in):
    c, auth = signed_in["client"], signed_in["auth"]
    for _ in range(5):
        assert _request(c, auth).status_code == 204
        _age_all_tokens(timedelta(seconds=61))

    r = _request(c, auth)
    assert r.status_code == 429
    # Capped by the 10-minute burst window, not the 30s cooldown.
    assert 30 < int(r.headers["Retry-After"]) <= 600
    assert len(_confirmation_emails(c)) == 5


def test_refused_request_leaves_pending_address_unchanged(signed_in):
    """A rate-limited request must not switch the address the user is
    waiting to confirm."""
    c, auth = signed_in["client"], signed_in["auth"]
    _request(c, auth, "first@example.com")
    assert _request(c, auth, "second@example.com").status_code == 429

    assert c.get("/auth/me", headers=auth).json()["notify_email"] == "first@example.com"


def test_limit_is_per_user(client):
    def sign_in(email):
        client.post("/auth/request-link", json={"email": email})
        body = client.sent_emails[-1]["body_text"]
        token = body.split("token=")[1].split("\n")[0].strip()
        session = client.post("/auth/verify", json={"token": token}).json()["session_token"]
        return {"Authorization": f"Bearer {session}"}

    alice, bob = sign_in("alice@example.com"), sign_in("bob@example.com")
    assert _request(client, alice, "a2@example.com").status_code == 204
    assert _request(client, bob, "b2@example.com").status_code == 204
