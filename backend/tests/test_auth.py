"""Cover the magic-link sign-in flow end-to-end."""

import pytest


def test_request_link_emits_email_with_link(client):
    r = client.post("/auth/request-link", json={"email": "BOB@Example.com"})
    assert r.status_code == 204
    assert len(client.sent_emails) == 1
    msg = client.sent_emails[0]
    # Email is normalised to the input form (we lowercase in auth service
    # for lookup, but send to the caller-provided address).
    assert msg["to"].lower() == "bob@example.com"
    assert "token=" in msg["body_text"]


def test_verify_creates_user_and_returns_session(client):
    client.post("/auth/request-link", json={"email": "bob@example.com"})
    token = client.sent_emails[-1]["body_text"].split("token=")[1].split("\n")[0].strip()
    r = client.post("/auth/verify", json={"token": token})
    assert r.status_code == 200
    j = r.json()
    assert j["user"]["email"] == "bob@example.com"
    assert j["session_token"] and len(j["session_token"]) > 30


def test_verify_token_is_single_use(client):
    client.post("/auth/request-link", json={"email": "bob@example.com"})
    token = client.sent_emails[-1]["body_text"].split("token=")[1].split("\n")[0].strip()
    assert client.post("/auth/verify", json={"token": token}).status_code == 200
    # second use must fail
    r2 = client.post("/auth/verify", json={"token": token})
    assert r2.status_code == 400


def test_me_requires_bearer_token(client):
    assert client.get("/auth/me").status_code == 401
    assert client.get("/auth/me", headers={"Authorization": "Bearer garbage"}).status_code == 401


def test_me_returns_signed_in_user(signed_in):
    r = signed_in["client"].get("/auth/me", headers=signed_in["auth"])
    assert r.status_code == 200
    assert r.json()["email"] == "alice@example.com"


def test_notify_email_change_flow(signed_in):
    c = signed_in["client"]
    r = c.post(
        "/auth/notify-email", json={"new_email": "alerts@example.com"}, headers=signed_in["auth"]
    )
    assert r.status_code == 204
    # Confirmation email goes to the NEW address.
    last = c.sent_emails[-1]
    assert last["to"] == "alerts@example.com"
    confirm_token = last["body_text"].split("token=")[1].split("\n")[0].strip()

    r2 = c.post("/auth/notify-email/confirm", json={"token": confirm_token})
    assert r2.status_code == 200
    assert r2.json()["notify_email"] == "alerts@example.com"
    assert r2.json()["notify_email_confirmed"] is True


# ---------------------------------------------------------------------------
# Default watchlist for brand-new accounts
# ---------------------------------------------------------------------------


def _sign_up(client, email: str) -> dict:
    client.post("/auth/request-link", json={"email": email})
    token = client.sent_emails[-1]["body_text"].split("token=")[1].split("\n")[0].strip()
    return client.post("/auth/verify", json={"token": token}).json()


class TestDefaultWatchlist:
    """A new account starts watching the broad market, so the dashboard isn't
    empty and the first daily recap has something to teach from."""

    @pytest.fixture(autouse=True)
    def _no_resend_cooldown(self, monkeypatch):
        """These tests sign in twice as the same person; the 30s resend
        cooldown would otherwise swallow the second magic link."""
        monkeypatch.setattr("app.services.auth.enforce_send_limit", lambda _: None)

    def test_new_user_watches_the_default_index_fund(self, client):
        from app.services.auth import DEFAULT_WATCH_SYMBOL

        s = _sign_up(client, "new@example.com")
        auth = {"Authorization": f"Bearer {s['session_token']}"}
        items = client.get("/watchlist", headers=auth).json()
        assert [i["ticker"]["symbol"] for i in items] == [DEFAULT_WATCH_SYMBOL]

    def test_new_user_gets_a_starter_rule_for_it(self, client):
        from app.core.settings import get_settings

        s = _sign_up(client, "new@example.com")
        auth = {"Authorization": f"Bearer {s['session_token']}"}
        rules = client.get("/rules", headers=auth).json()
        assert len(rules) == 1
        band = get_settings().default_rule_band_pct
        assert rules[0]["event_type"] == "price_change_range"
        assert (rules[0]["pct_low"], rules[0]["pct_high"]) == (-band, band)

    def test_signing_in_again_does_not_duplicate(self, client):
        _sign_up(client, "repeat@example.com")
        s = _sign_up(client, "repeat@example.com")
        auth = {"Authorization": f"Bearer {s['session_token']}"}
        assert len(client.get("/watchlist", headers=auth).json()) == 1
        assert len(client.get("/rules", headers=auth).json()) == 1

    def test_removing_the_default_is_not_re_added(self, client):
        """Signing in again must not resurrect a watchlist they cleared."""
        s = _sign_up(client, "clears@example.com")
        auth = {"Authorization": f"Bearer {s['session_token']}"}
        item_id = client.get("/watchlist", headers=auth).json()[0]["id"]
        assert client.delete(f"/watchlist/{item_id}", headers=auth).status_code == 204

        s2 = _sign_up(client, "clears@example.com")
        auth2 = {"Authorization": f"Bearer {s2['session_token']}"}
        assert client.get("/watchlist", headers=auth2).json() == []
