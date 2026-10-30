"""Daily watchlist recap: who gets it, what's in it, stories, no repeats."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.db.database import get_session_factory
from app.db.seed import seed
from app.jobs.recap import run_watchlist_recap
from app.models import (
    DailyClose,
    EmailOptOut,
    NotificationEventType,
    NotificationLog,
    NotificationRule,
    RecapOptOut,
    RecapSend,
    StockArticle,
    Ticker,
    User,
    WatchlistItem,
)
from app.services.articles import service
from app.services.articles.preferences import set_age_groups
from tests.test_articles import stories_on  # noqa: F401  (fixture)


@pytest.fixture(autouse=True)
def _market(monkeypatch):
    monkeypatch.setattr(service, "market_move_pct", lambda: 0.42)


def _user(email: str, symbols: list[str], confirmed: bool = True) -> int:
    seed()
    db = get_session_factory()()
    user = User(email=email, notify_email=email, notify_email_confirmed=confirmed)
    db.add(user)
    db.flush()
    for sym in symbols:
        t = db.execute(select(Ticker).where(Ticker.symbol == sym)).scalar_one()
        db.add(WatchlistItem(user_id=user.id, ticker_id=t.id))
    db.commit()
    uid = user.id
    db.close()
    return uid


def _closes(symbol: str, today: float, prev: float = 100.0, days: int = 2) -> None:
    """`days` daily closes ending today: `prev` on every earlier day."""
    seed()
    db = get_session_factory()()
    t = db.execute(select(Ticker).where(Ticker.symbol == symbol)).scalar_one()
    day = service.trading_date()
    for i in range(days - 1, 0, -1):
        db.add(DailyClose(ticker_id=t.id, date=day - timedelta(days=i), close=prev))
    db.add(DailyClose(ticker_id=t.id, date=day, close=today))
    db.commit()
    db.close()


def _add(row) -> None:
    db = get_session_factory()()
    db.add(row)
    db.commit()
    db.close()


def test_recap_lists_every_stock_biggest_move_first(client):
    _user("a@example.com", ["AAPL", "MSFT"])
    _closes("AAPL", 100.5)
    _closes("MSFT", 97.0)

    assert run_watchlist_recap() == 1

    email = client.sent_emails[0]
    assert email["to"] == "a@example.com"
    assert "Your watchlist today: MSFT -3.0%" in email["subject"]
    body = email["body_text"]
    assert "S&P 500 +0.42% today" in body
    assert "Biggest mover: MSFT -3.00%" in body
    assert body.index("MSFT ") < body.index("AAPL ")
    assert "+0.50%" in body
    assert "/settings" in body and "/unsubscribe?token=" in body
    assert "daily recap" in email["body_html"]
    assert "STOCK STORY" not in body  # stories are off


def test_recap_only_goes_to_users_who_want_it(client):
    _user("on@example.com", ["AAPL"])
    recap_off = _user("recap-off@example.com", ["AAPL"])
    alerts_off = _user("alerts-off@example.com", ["AAPL"])
    _user("unconfirmed@example.com", ["AAPL"], confirmed=False)
    _user("no-stocks@example.com", [])
    _add(RecapOptOut(user_id=recap_off))
    _add(EmailOptOut(user_id=alerts_off))
    _closes("AAPL", 102.0)

    assert run_watchlist_recap() == 1
    assert [e["to"] for e in client.sent_emails] == ["on@example.com"]


def test_no_recap_on_a_market_holiday(client):
    _user("a@example.com", ["AAPL"])
    seed()
    db = get_session_factory()()
    t = db.execute(select(Ticker).where(Ticker.symbol == "AAPL")).scalar_one()
    db.add(DailyClose(ticker_id=t.id, date=service.trading_date() - timedelta(days=1), close=100))
    db.commit()
    db.close()

    assert run_watchlist_recap() == 0
    assert client.sent_emails == []


def test_recap_is_sent_once_per_day(client):
    _user("a@example.com", ["AAPL"])
    _closes("AAPL", 102.0)

    assert run_watchlist_recap() == 1
    assert run_watchlist_recap() == 0
    db = get_session_factory()()
    assert len(db.execute(select(RecapSend)).scalars().all()) == 1
    db.close()


def test_new_highs_and_todays_alerts_are_tagged(client):
    uid = _user("a@example.com", ["AAPL", "MSFT"])
    _closes("AAPL", 110.0, days=40)  # highest close in over a month
    _closes("MSFT", 101.0)  # too little history to claim a record
    db = get_session_factory()()
    aapl = db.execute(select(Ticker).where(Ticker.symbol == "AAPL")).scalar_one()
    db.add(NotificationLog(user_id=uid, ticker_id=aapl.id, sent_to="a@example.com",
                           event_type=NotificationEventType.PRICE_CHANGE_RANGE, summary="x"))
    db.commit()
    db.close()

    run_watchlist_recap()

    body = client.sent_emails[0]["body_text"]
    aapl_line = next(line for line in body.splitlines() if line.startswith("  AAPL"))
    msft_line = next(line for line in body.splitlines() if line.startswith("  MSFT"))
    assert "new 1-month high" in aapl_line and "alert sent today" in aapl_line
    assert "new" not in msft_line and "alert" not in msft_line


def test_biggest_mover_gets_an_ai_story_shared_by_everyone(client, stories_on):  # noqa: F811
    _user("a@example.com", ["AAPL", "MSFT"])
    _user("b@example.com", ["AAPL"])
    _closes("AAPL", 104.0)
    _closes("MSFT", 100.2)

    assert run_watchlist_recap() == 2

    assert stories_on == ["AAPL"]  # one story, shared; MSFT isn't anyone's top
    for email in client.sent_emails:
        assert "Talk about it together: AAPL" in email["body_text"]
        assert "STOCK STORY" in email["body_text"]
        assert "Ask your kid" in email["body_html"]


def test_story_written_for_an_alert_today_is_reused(client, stories_on):  # noqa: F811
    _user("a@example.com", ["AAPL"])
    _closes("AAPL", 104.0)
    db = get_session_factory()()
    aapl = db.execute(select(Ticker).where(Ticker.symbol == "AAPL")).scalar_one()
    service.ensure_articles(db, [service.Candidate(aapl, 3.5, 103.5, 100.0)])
    db.close()
    assert stories_on == ["AAPL"]

    run_watchlist_recap()

    assert stories_on == ["AAPL"]  # not written again
    assert "STOCK STORY" in client.sent_emails[0]["body_text"]
    db = get_session_factory()()
    assert len(db.execute(select(StockArticle)).scalars().all()) == 1
    db.close()


def test_small_biggest_move_gets_the_fixed_note_in_the_parents_ages(client, stories_on):  # noqa: F811
    uid = _user("a@example.com", ["AAPL"])
    db = get_session_factory()()
    set_age_groups(db, db.get(User, uid), ["3-5"])
    db.close()
    _closes("AAPL", 100.4)

    run_watchlist_recap()

    body = client.sent_emails[0]["body_text"]
    assert stories_on == []
    assert "SMALL MOVE" in body
    assert "ages 3–5" in body and "ages 6–9" not in body


def test_recap_switch_in_settings(signed_in):
    c, auth = signed_in["client"], signed_in["auth"]
    prefs = c.get("/auth/email-preferences", headers=auth).json()
    assert prefs == {"alerts_enabled": True, "recap_enabled": True}

    r = c.put("/auth/email-preferences", json={"recap_enabled": False}, headers=auth)
    assert r.json() == {"alerts_enabled": True, "recap_enabled": False}  # alerts untouched
    r = c.put("/auth/email-preferences", json={"alerts_enabled": False}, headers=auth)
    assert r.json() == {"alerts_enabled": False, "recap_enabled": False}  # recap untouched


def _rule(uid: int, symbol: str, low: float, high: float, enabled: bool = True) -> int:
    db = get_session_factory()()
    t = db.execute(select(Ticker).where(Ticker.symbol == symbol)).scalar_one()
    db.add(NotificationRule(user_id=uid, ticker_id=t.id, pct_low=low, pct_high=high,
                            enabled=enabled,
                            event_type=NotificationEventType.PRICE_CHANGE_RANGE))
    db.commit()
    tid = t.id
    db.close()
    return tid


def test_quiet_price_alerts_get_a_how_and_why_to_adjust(client):
    uid = _user("a@example.com", ["AAPL", "MSFT"])
    aapl = _rule(uid, "AAPL", -5.0, 5.0)  # +0.8% today: stayed quiet
    _rule(uid, "MSFT", -2.0, 2.0)          # -3% today: outside, so not "quiet"
    _closes("AAPL", 100.8)
    _closes("MSFT", 97.0)

    run_watchlist_recap()

    email = client.sent_emails[0]
    body = email["body_text"]
    assert "Your price alerts that didn't go off today" in body
    assert "AAPL: your range is -5% to +5%; today it moved +0.80%." in body
    assert "MSFT: your range" not in body
    assert "Why change it" in body and "-1.5% to +1.5%" in body
    assert 'choose "Price change outside band (%)"' in body and "Save rule" in body
    assert f"/rules/{aapl}" in body
    assert f"/rules/{aapl}" in email["body_html"]


def test_no_quiet_section_when_alert_fired_paused_or_no_rule(client):
    uid = _user("a@example.com", ["AAPL", "MSFT", "SPY", "QQQ"])
    aapl = _rule(uid, "AAPL", -5.0, 5.0)
    _rule(uid, "MSFT", -5.0, 5.0, enabled=False)  # paused: the user chose quiet
    _rule(uid, "QQQ", -1.5, 1.5)  # already as small as we'd suggest
    # SPY has no rule at all.
    _closes("AAPL", 100.8)
    _closes("MSFT", 100.5)
    _closes("SPY", 100.3)
    _closes("QQQ", 100.6)
    _add(NotificationLog(user_id=uid, ticker_id=aapl, sent_to="a@example.com",
                         event_type=NotificationEventType.PRICE_CHANGE_RANGE, summary="x"))

    run_watchlist_recap()

    assert "didn't go off" not in client.sent_emails[0]["body_text"]
