"""Default rule on add, rule cleanup on remove, and alert-email unsubscribe."""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select

from app.core.settings import get_settings
from app.db.database import get_session_factory
from app.db.seed import seed
from app.jobs.intraday import run_intraday_capture
from app.models import (
    DailyClose,
    EmailOptOut,
    NotificationEventType,
    NotificationRule,
    Ticker,
    User,
    WatchlistItem,
)
from app.services.email_prefs import set_alerts_enabled, unsubscribe_url


def _rules(client, auth):
    r = client.get("/rules", headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


def _rules_for(client, auth, ticker_id):
    """Rules for one ticker. New users start with a rule on the default
    index fund (SPY), so these tests scope to the stock under test."""
    return [r for r in _rules(client, auth) if r["ticker_id"] == ticker_id]


def _ticker_id(symbol):
    db = get_session_factory()()
    try:
        return db.execute(select(Ticker.id).where(Ticker.symbol == symbol)).scalar_one()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Default rule
# ---------------------------------------------------------------------------


def test_adding_stock_creates_default_band_rule(signed_in):
    seed()
    c, auth = signed_in["client"], signed_in["auth"]
    item = c.post("/watchlist", json={"symbol": "AAPL"}, headers=auth).json()

    rules = _rules_for(c, auth, item["ticker"]["id"])
    assert len(rules) == 1
    rule = rules[0]
    assert rule["ticker_id"] == item["ticker"]["id"]
    assert rule["event_type"] == "price_change_range"
    assert (rule["pct_low"], rule["pct_high"]) == (-1.5, 1.5)
    assert rule["enabled"] is True


def test_default_rule_skipped_when_user_already_has_a_rule(signed_in):
    seed()
    c, auth = signed_in["client"], signed_in["auth"]
    ticker_id = _ticker_id("AAPL")
    c.post(
        "/rules",
        json={"ticker_id": ticker_id, "event_type": "month_high", "enabled": True},
        headers=auth,
    )

    c.post("/watchlist", json={"symbol": "AAPL"}, headers=auth)

    rules = _rules_for(c, auth, ticker_id)
    assert [r["event_type"] for r in rules] == ["month_high"]


def test_default_rule_can_be_disabled_by_setting(signed_in, monkeypatch):
    seed()
    c, auth = signed_in["client"], signed_in["auth"]
    monkeypatch.setenv("DEFAULT_RULE_BAND_PCT", "0")
    get_settings.cache_clear()

    c.post("/watchlist", json={"symbol": "AAPL"}, headers=auth)

    assert _rules_for(c, auth, _ticker_id("AAPL")) == []


def test_adding_same_stock_twice_keeps_one_rule(signed_in):
    seed()
    c, auth = signed_in["client"], signed_in["auth"]
    c.post("/watchlist", json={"symbol": "AAPL"}, headers=auth)
    c.post("/watchlist", json={"symbol": "AAPL"}, headers=auth)

    assert len(_rules_for(c, auth, _ticker_id("AAPL"))) == 1


def test_removing_stock_deletes_its_rules_only(signed_in):
    seed()
    c, auth = signed_in["client"], signed_in["auth"]
    aapl = c.post("/watchlist", json={"symbol": "AAPL"}, headers=auth).json()
    msft = c.post("/watchlist", json={"symbol": "MSFT"}, headers=auth).json()

    r = c.delete(f"/watchlist/{aapl['id']}", headers=auth)
    assert r.status_code == 204

    assert _rules_for(c, auth, aapl["ticker"]["id"]) == []
    assert len(_rules_for(c, auth, msft["ticker"]["id"])) == 1


# ---------------------------------------------------------------------------
# Preferences API
# ---------------------------------------------------------------------------


def test_alerts_enabled_by_default_and_can_be_toggled(signed_in):
    c, auth = signed_in["client"], signed_in["auth"]
    assert c.get("/auth/email-preferences", headers=auth).json()["alerts_enabled"] is True

    r = c.put("/auth/email-preferences", json={"alerts_enabled": False}, headers=auth)
    assert r.json()["alerts_enabled"] is False
    assert c.get("/auth/email-preferences", headers=auth).json()["alerts_enabled"] is False

    # Turning off twice is fine (idempotent).
    c.put("/auth/email-preferences", json={"alerts_enabled": False}, headers=auth)

    c.put("/auth/email-preferences", json={"alerts_enabled": True}, headers=auth)
    assert c.get("/auth/email-preferences", headers=auth).json()["alerts_enabled"] is True


def test_email_preferences_require_sign_in(client):
    assert client.get("/auth/email-preferences").status_code == 401


# ---------------------------------------------------------------------------
# Unsubscribe link
# ---------------------------------------------------------------------------


def _token_for(user_id: int) -> str:
    db = get_session_factory()()
    user = db.get(User, user_id)
    url = unsubscribe_url(user)
    db.close()
    return url.split("token=")[1]


def test_unsubscribe_link_turns_alerts_off_without_sign_in(signed_in):
    c, auth = signed_in["client"], signed_in["auth"]
    token = _token_for(signed_in["user"]["id"])

    r = c.post("/auth/unsubscribe", json={"token": token})
    assert r.status_code == 204
    assert c.get("/auth/email-preferences", headers=auth).json()["alerts_enabled"] is False

    # Clicking the same link again still succeeds.
    assert c.post("/auth/unsubscribe", json={"token": token}).status_code == 204


def test_unsubscribe_rejects_bad_token(client):
    r = client.post("/auth/unsubscribe", json={"token": "x" * 60})
    assert r.status_code == 400


def test_magic_link_token_cannot_be_used_to_unsubscribe(client):
    """Tokens are purpose-scoped: a sign-in token must not unsubscribe."""
    client.post("/auth/request-link", json={"email": "bob@example.com"})
    body = client.sent_emails[-1]["body_text"]
    magic = body.split("token=")[1].split("\n")[0].strip()
    assert client.post("/auth/unsubscribe", json={"token": magic}).status_code == 400


# ---------------------------------------------------------------------------
# Alert emails respect the opt-out
# ---------------------------------------------------------------------------


def _user_with_band_rule() -> int:
    seed()
    db = get_session_factory()()
    user = User(email="a@example.com", notify_email="a@example.com", notify_email_confirmed=True)
    db.add(user)
    db.flush()
    t = db.execute(select(Ticker).where(Ticker.symbol == "AAPL")).scalar_one()
    db.add(WatchlistItem(user_id=user.id, ticker_id=t.id))
    db.add(NotificationRule(
        user_id=user.id, ticker_id=t.id,
        event_type=NotificationEventType.PRICE_CHANGE_RANGE,
        pct_low=-1.0, pct_high=1.0, enabled=True,
    ))
    db.add(DailyClose(ticker_id=t.id, date=date.today() - timedelta(days=1), close=100.0))
    db.commit()
    uid = user.id
    db.close()
    return uid


def _prices(values):
    it = iter(values)
    return lambda _symbol: next(it)


def test_alert_email_has_unsubscribe_link_and_header(client):
    _user_with_band_rule()
    run_intraday_capture(fetcher=_prices([100.0]), force=True)
    assert run_intraday_capture(fetcher=_prices([105.0]), force=True) == 1

    email = client.sent_emails[-1]
    assert "/unsubscribe?token=" in email["body_text"]
    assert email["headers"]["List-Unsubscribe"].startswith("<http")


def test_unsubscribed_user_gets_no_alerts(client):
    uid = _user_with_band_rule()
    db = get_session_factory()()
    set_alerts_enabled(db, db.get(User, uid), False)
    assert db.get(EmailOptOut, uid) is not None
    db.close()

    run_intraday_capture(fetcher=_prices([100.0]), force=True)
    assert run_intraday_capture(fetcher=_prices([105.0]), force=True) == 0
    assert client.sent_emails == []


def test_unsubscribed_user_still_gets_sign_in_links(client):
    uid = _user_with_band_rule()
    db = get_session_factory()()
    set_alerts_enabled(db, db.get(User, uid), False)
    db.close()

    client.post("/auth/request-link", json={"email": "a@example.com"})
    assert len(client.sent_emails) == 1
    assert "token=" in client.sent_emails[0]["body_text"]


def test_default_band_is_one_and_a_half_percent():
    from app.core.settings import Settings
    assert Settings.model_fields["default_rule_band_pct"].default == 1.5
