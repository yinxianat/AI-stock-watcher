"""Signed deep links in emails.

Email footers link to Settings with a purpose-scoped token so a parent
clicking from their inbox isn't dropped on a login form. The token must
open the preference endpoints and nothing else — in particular it must not
be usable to change the notification email, or a forwarded email would let
someone redirect another person's alerts.
"""

from __future__ import annotations

import pytest

from app.core.security import issue_token
from app.db.database import get_session_factory
from app.services.email_links import (
    MONEY_IDEAS_PURPOSE,
    SETTINGS_PURPOSE,
    money_ideas_url,
    settings_url,
)


def _token(url: str) -> str:
    return url.split("token=")[1]


# ---------------------------------------------------------------------------
# URL shape
# ---------------------------------------------------------------------------

class TestLinkUrls:
    def test_settings_url_shape(self):
        assert "/settings?token=" in settings_url(1)

    def test_money_ideas_url_shape(self):
        assert "/money-ideas?token=" in money_ideas_url(1)

    def test_purposes_produce_different_tokens(self):
        assert _token(settings_url(1)) != _token(money_ideas_url(1))


# ---------------------------------------------------------------------------
# Preferences via an email token (no session)
# ---------------------------------------------------------------------------

class TestPreferencesViaToken:
    def test_get_email_preferences(self, client, signed_in):
        token = _token(settings_url(signed_in["user"]["id"]))
        r = client.get(f"/auth/email-preferences?token={token}")
        assert r.status_code == 200
        assert r.json()["alerts_enabled"] is True

    def test_put_email_preferences(self, client, signed_in):
        token = _token(settings_url(signed_in["user"]["id"]))
        r = client.put(
            f"/auth/email-preferences?token={token}", json={"recap_enabled": False},
        )
        assert r.status_code == 200
        assert r.json()["recap_enabled"] is False

    def test_get_story_preferences(self, client, signed_in):
        token = _token(settings_url(signed_in["user"]["id"]))
        r = client.get(f"/auth/story-preferences?token={token}")
        assert r.status_code == 200
        assert "age_groups" in r.json()

    def test_put_story_preferences(self, client, signed_in):
        token = _token(settings_url(signed_in["user"]["id"]))
        r = client.put(
            f"/auth/story-preferences?token={token}", json={"age_groups": ["10-12"]},
        )
        assert r.status_code == 200
        assert r.json()["age_groups"] == ["10-12"]

    def test_token_change_is_visible_to_the_session(self, client, signed_in):
        """A toggle flipped from the email shows up when they next sign in."""
        token = _token(settings_url(signed_in["user"]["id"]))
        client.put(f"/auth/email-preferences?token={token}", json={"recap_enabled": False})
        r = client.get("/auth/email-preferences", headers=signed_in["auth"])
        assert r.json()["recap_enabled"] is False


# ---------------------------------------------------------------------------
# Rejections
# ---------------------------------------------------------------------------

class TestTokenRejections:
    def test_garbage_token(self, client):
        assert client.get("/auth/email-preferences?token=nonsense").status_code == 400

    def test_wrong_purpose_token_rejected(self, client, signed_in):
        """A money-ideas link must not open the preference endpoints."""
        token = _token(money_ideas_url(signed_in["user"]["id"]))
        assert client.get(f"/auth/email-preferences?token={token}").status_code == 400

    def test_feedback_token_rejected(self, client, signed_in):
        from app.api.feedback_routes import FEEDBACK_PURPOSE

        token = issue_token(FEEDBACK_PURPOSE, {"uid": signed_in["user"]["id"]})
        assert client.get(f"/auth/email-preferences?token={token}").status_code == 400

    def test_settings_token_cannot_open_money_ideas(self, client, signed_in):
        token = _token(settings_url(signed_in["user"]["id"]))
        assert client.get(f"/money-ideas?token={token}").status_code == 400

    def test_unknown_user_rejected(self, client):
        token = issue_token(SETTINGS_PURPOSE, {"uid": 999999})
        assert client.get(f"/auth/email-preferences?token={token}").status_code == 400

    def test_no_token_and_no_session_is_401(self, client):
        assert client.get("/auth/email-preferences").status_code == 401


# ---------------------------------------------------------------------------
# The security boundary this design exists to hold
# ---------------------------------------------------------------------------

class TestTokenScope:
    def test_token_cannot_change_notification_email(self, client, signed_in):
        """The whole point of a scoped token: a forwarded email must not let
        anyone redirect this user's alerts to their own address."""
        token = _token(settings_url(signed_in["user"]["id"]))
        r = client.post(
            f"/auth/notify-email?token={token}", json={"new_email": "attacker@evil.com"},
        )
        assert r.status_code == 401

    def test_token_cannot_read_account(self, client, signed_in):
        token = _token(settings_url(signed_in["user"]["id"]))
        assert client.get(f"/auth/me?token={token}").status_code == 401

    def test_token_cannot_list_watchlist(self, client, signed_in):
        token = _token(settings_url(signed_in["user"]["id"]))
        assert client.get(f"/watchlist?token={token}").status_code == 401

    def test_token_is_not_a_session(self, client, signed_in):
        """It must not work as a bearer token either."""
        token = _token(settings_url(signed_in["user"]["id"]))
        r = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# Footers actually carry the links
# ---------------------------------------------------------------------------

class TestFootersUseTokenLinks:
    def test_alert_footer_has_tokenised_settings_link(self):
        from app.models import User
        from app.services.email_prefs import alert_email_footer

        # Both links are built from the id alone, so no DB round-trip here.
        u = User(id=7, email="a@b.com", notify_email="a@b.com")
        footer = alert_email_footer(u)
        assert "/settings?token=" in footer
        assert "/unsubscribe?token=" in footer

    # The recap email's own footer is asserted in test_recap.py, where the
    # job's fixtures already live.
