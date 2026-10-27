"""Sign-in link throttling and notification-email change validation."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app.db.database import get_session_factory
from app.models import LoginToken, User


def _age_login_tokens(by: timedelta):
    db = get_session_factory()()
    for row in db.execute(select(LoginToken)).scalars():
        row.created_at -= by
    db.commit()
    db.close()


def _sign_in(client, email):
    client.post("/auth/request-link", json={"email": email})
    body = client.sent_emails[-1]["body_text"]
    token = body.split("token=")[1].split("\n")[0].strip()
    session = client.post("/auth/verify", json={"token": token}).json()["session_token"]
    return {"Authorization": f"Bearer {session}"}


def _change(client, auth, email):
    return client.post("/auth/notify-email", json={"new_email": email}, headers=auth)


# ---------------------------------------------------------------------------
# Sign-in link ("Send link") throttling
# ---------------------------------------------------------------------------


def test_rapid_send_link_clicks_send_one_email(client):
    assert client.post("/auth/request-link", json={"email": "bob@example.com"}).status_code == 204
    for _ in range(5):
        r = client.post("/auth/request-link", json={"email": "bob@example.com"})
        assert r.status_code == 429
        assert 1 <= int(r.headers["Retry-After"]) <= 30
    assert len(client.sent_emails) == 1


def test_send_link_limit_ignores_email_case(client):
    client.post("/auth/request-link", json={"email": "bob@example.com"})
    r = client.post("/auth/request-link", json={"email": "BOB@Example.com"})
    assert r.status_code == 429


def test_send_link_limit_is_per_address(client):
    assert client.post("/auth/request-link", json={"email": "a@example.com"}).status_code == 204
    assert client.post("/auth/request-link", json={"email": "b@example.com"}).status_code == 204


def test_can_request_new_link_after_cooldown(client):
    client.post("/auth/request-link", json={"email": "bob@example.com"})
    _age_login_tokens(timedelta(seconds=61))
    assert client.post("/auth/request-link", json={"email": "bob@example.com"}).status_code == 204
    assert len(client.sent_emails) == 2


def test_send_link_burst_cap(client):
    for _ in range(5):
        assert client.post("/auth/request-link", json={"email": "bob@example.com"}).status_code == 204
        _age_login_tokens(timedelta(seconds=61))
    r = client.post("/auth/request-link", json={"email": "bob@example.com"})
    assert r.status_code == 429
    # Capped by the 10-minute burst window, not the 30s cooldown.
    assert 30 < int(r.headers["Retry-After"]) <= 600


def test_limit_does_not_reveal_whether_account_exists(client):
    """Registered and unknown addresses get identical responses."""
    _sign_in(client, "known@example.com")
    _age_login_tokens(timedelta(seconds=61))

    for email in ("known@example.com", "unknown@example.com"):
        assert client.post("/auth/request-link", json={"email": email}).status_code == 204
        assert client.post("/auth/request-link", json={"email": email}).status_code == 429


# ---------------------------------------------------------------------------
# Changing the notification email
# ---------------------------------------------------------------------------


def test_same_email_is_rejected_with_a_clear_message(signed_in):
    c, auth = signed_in["client"], signed_in["auth"]
    current = signed_in["user"]["notify_email"]

    r = _change(c, auth, current.upper())
    assert r.status_code == 400
    assert "already your notification email" in r.json()["detail"]
    assert c.sent_emails[-1]["subject"] != "Confirm your notification email"


def test_pending_address_can_be_requested_again_to_resend(signed_in):
    """Re-entering the address that's waiting for confirmation re-sends the
    link (after the cooldown) instead of being called a duplicate."""
    c, auth = signed_in["client"], signed_in["auth"]
    assert _change(c, auth, "new@example.com").status_code == 204

    db = get_session_factory()()
    from app.models import ConfirmationToken
    for row in db.execute(select(ConfirmationToken)).scalars():
        row.created_at -= timedelta(seconds=61)
    db.commit()
    db.close()

    assert _change(c, auth, "new@example.com").status_code == 204


def test_another_users_login_email_is_rejected(client):
    alice = _sign_in(client, "alice@example.com")
    _sign_in(client, "bob@example.com")

    r = _change(client, alice, "Bob@Example.com")
    assert r.status_code == 409
    assert "another account" in r.json()["detail"]
    me = client.get("/auth/me", headers=alice).json()
    assert me["notify_email"] == "alice@example.com"
    assert me["notify_email_confirmed"] is True


def test_another_users_confirmed_notify_email_is_rejected(client):
    alice = _sign_in(client, "alice@example.com")
    _sign_in(client, "bob@example.com")
    db = get_session_factory()()
    bob = db.execute(select(User).where(User.email == "bob@example.com")).scalar_one()
    bob.notify_email = "bob.alerts@example.com"
    bob.notify_email_confirmed = True
    db.commit()
    db.close()

    assert _change(client, alice, "bob.alerts@example.com").status_code == 409


def test_unconfirmed_address_of_another_user_does_not_block(client):
    """Typing an address without confirming it must not reserve it."""
    alice = _sign_in(client, "alice@example.com")
    bob = _sign_in(client, "bob@example.com")
    assert _change(client, bob, "shared@example.com").status_code == 204  # never confirmed

    assert _change(client, alice, "shared@example.com").status_code == 204


def test_switching_back_to_own_login_email_is_allowed(signed_in):
    c, auth = signed_in["client"], signed_in["auth"]
    db = get_session_factory()()
    user = db.get(User, signed_in["user"]["id"])
    user.notify_email = "work@example.com"
    user.notify_email_confirmed = True
    db.commit()
    db.close()

    assert _change(c, auth, signed_in["user"]["email"]).status_code == 204


def test_confirm_fails_if_address_was_taken_meanwhile(client):
    alice = _sign_in(client, "alice@example.com")
    _change(client, alice, "shared@example.com")
    confirm_token = client.sent_emails[-1]["body_text"].split("token=")[1].split("\n")[0].strip()

    # Someone signs up with that address before Alice clicks her link.
    _sign_in(client, "shared@example.com")

    r = client.post("/auth/notify-email/confirm", json={"token": confirm_token})
    assert r.status_code == 409
