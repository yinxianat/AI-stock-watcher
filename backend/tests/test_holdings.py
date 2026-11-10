"""Purchase-price comparison: what the user paid, and what it is worth now.

Three things decide whether this feature is right, and each has its own
section below.

1. It is OFF until asked for. A family that never opts in is never asked
   for a purchase price, never sees an empty column, and has no row in any
   holdings table.
2. The arithmetic. "How much am I up?" is the question, and a wrong answer
   is worse than no answer — so "we don't know the price yet" must never
   render as "$0.00", and a loss must never render as a gain.
3. It is private. A purchase price is financial data about a real person:
   it must not reach a log line, the AI story pipeline, another user, or an
   admin's SQL console unless that is deliberately switched on.
"""

from __future__ import annotations

import logging

import pytest
from sqlalchemy import select

from app.db.database import get_session_factory
from app.db.seed import seed
from app.models import DailyClose, Holding, HoldingsOptIn, Ticker, TrendAnalysis, User
from app.services import holdings
from app.services.email_links import SETTINGS_PURPOSE
from app.core.security import issue_token

from tests.test_recap import _closes, _market, _user as _recap_user  # noqa: F401


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _watch(signed_in, symbol: str) -> int:
    """Put `symbol` on the watchlist and return its ticker id."""
    r = signed_in["client"].post(
        "/watchlist", json={"symbol": symbol}, headers=signed_in["auth"]
    )
    assert r.status_code in (200, 201), r.text
    return r.json()["ticker"]["id"]


def _price(ticker_id: int, price: float, user_id: int) -> None:
    """Give the ticker a current price, the way a compute run would."""
    db = get_session_factory()()
    db.add(TrendAnalysis(
        user_id=user_id, ticker_id=ticker_id, price=price, pct_change=0.0,
        is_week_low=False, is_week_high=False, is_month_low=False,
        is_month_high=False, is_quarter_low=False, is_quarter_high=False,
        is_year_low=False, is_year_high=False,
    ))
    db.commit()
    db.close()


def _enable(signed_in, enabled: bool = True):
    r = signed_in["client"].put(
        "/auth/holdings-preferences", json={"enabled": enabled},
        headers=signed_in["auth"],
    )
    assert r.status_code == 200, r.text
    return r.json()


def _rows(model):
    db = get_session_factory()()
    rows = db.execute(select(model)).scalars().all()
    db.close()
    return rows


# ---------------------------------------------------------------------------
# 1. Off until asked for
# ---------------------------------------------------------------------------


def test_the_feature_is_off_for_a_new_user(signed_in):
    """The default the user asked for. Nobody is prompted for a purchase
    price until they turn the comparison on themselves."""
    r = signed_in["client"].get("/auth/holdings-preferences", headers=signed_in["auth"])
    assert r.status_code == 200
    assert r.json() == {"enabled": False}
    assert _rows(HoldingsOptIn) == []


def test_turning_it_on_and_off_again(signed_in):
    assert _enable(signed_in, True) == {"enabled": True}
    assert len(_rows(HoldingsOptIn)) == 1
    assert _enable(signed_in, False) == {"enabled": False}
    assert _rows(HoldingsOptIn) == []
    # Idempotent in both directions — a double-click must not 500 or
    # accumulate rows.
    assert _enable(signed_in, False) == {"enabled": False}
    assert _enable(signed_in, True) == {"enabled": True}
    assert _enable(signed_in, True) == {"enabled": True}
    assert len(_rows(HoldingsOptIn)) == 1


def test_turning_it_off_keeps_the_prices(signed_in):
    """A user who changes their mind twice should not have to retype every
    price. The prices are not shown anywhere while the switch is off."""
    seed()
    _enable(signed_in)
    tid = _watch(signed_in, "AAPL")
    c, h = signed_in["client"], signed_in["auth"]
    assert c.put(f"/holdings/{tid}", json={"purchase_price": 98.0}, headers=h).status_code == 200

    _enable(signed_in, False)
    assert len(_rows(Holding)) == 1, "the price was deleted when the feature went off"

    _enable(signed_in, True)
    rows = c.get("/holdings", headers=h).json()
    assert [r["purchase_price"] for r in rows] == [98.0]


def test_a_price_cannot_be_set_while_the_feature_is_off(signed_in):
    """A write with the feature off is a client bug, not a user choice, and
    silently accepting it would store data the user never agreed to give."""
    seed()
    tid = _watch(signed_in, "AAPL")
    r = signed_in["client"].put(
        f"/holdings/{tid}", json={"purchase_price": 98.0}, headers=signed_in["auth"]
    )
    assert r.status_code == 409
    assert "Settings" in r.json()["detail"]
    assert _rows(Holding) == []


def test_listing_holdings_while_off_is_empty_not_an_error(signed_in):
    """The watchlist page asks for these on every load. A 409 here would
    make the page log an error for every user who never turned it on."""
    r = signed_in["client"].get("/holdings", headers=signed_in["auth"])
    assert r.status_code == 200
    assert r.json() == []


def test_the_preference_can_be_changed_from_an_email_link(signed_in):
    """Like the other preferences: a parent clicking Settings from an email
    footer is not sent to a login screen."""
    token = issue_token(SETTINGS_PURPOSE, {"uid": signed_in["user"]["id"]})
    c = signed_in["client"]
    r = c.put(f"/auth/holdings-preferences?token={token}", json={"enabled": True})
    assert r.status_code == 200 and r.json() == {"enabled": True}
    r = c.get(f"/auth/holdings-preferences?token={token}")
    assert r.json() == {"enabled": True}


# ---------------------------------------------------------------------------
# 2. The arithmetic
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("paid,shares,now,per_share,pct,total", [
    (98.0, 12, 106.40, 8.40, 8.5714, 100.80),     # a gain, with shares
    (120.0, 3, 100.00, -20.00, -16.6667, -60.00),  # a loss, with shares
    (100.0, 1, 100.00, 0.0, 0.0, 0.0),             # exactly even
    (590.0, None, 612.10, 22.10, 3.7458, None),    # no share count given
    (0.5, 1000, 0.75, 0.25, 50.0, 250.0),          # a penny stock
    (98.0, 0.5, 108.0, 10.0, 10.2041, 5.0),        # fractional shares
])
def test_gain_loss_arithmetic(paid, shares, now, per_share, pct, total):
    gl = holdings.compute(paid, shares, now)
    assert gl is not None
    assert gl.per_share == pytest.approx(per_share)
    assert gl.pct == pytest.approx(pct, abs=1e-3)
    if total is None:
        assert gl.total is None and gl.cost_basis is None and gl.market_value is None
    else:
        assert gl.total == pytest.approx(total)
        assert gl.cost_basis == pytest.approx(paid * shares)
        assert gl.market_value == pytest.approx(now * shares)


def test_no_current_price_is_not_a_zero_gain():
    """The distinction the whole feature turns on. A stock added today has
    no price yet; reporting "$0.00 (0.00%)" would tell the user they are
    even, which is a different and false statement."""
    assert holdings.compute(98.0, 10, None) is None


def test_an_unusable_purchase_price_reports_nothing():
    """Zero would divide by zero in the percentage; negative is not a price.
    Both are rejected at the API boundary, and defended here because rows
    outlive the code that wrote them."""
    assert holdings.compute(0.0, 10, 100.0) is None
    assert holdings.compute(-5.0, 10, 100.0) is None


def test_break_even_is_not_shown_as_a_loss():
    """Colour follows `is_gain`. Showing an unchanged position in red reads
    as bad news that has not happened."""
    assert holdings.compute(100.0, 1, 100.0).is_gain is True
    assert holdings.compute(100.0, 1, 99.99).is_gain is False


def test_zero_shares_is_treated_as_no_share_count():
    """Not as a real position worth $0.00."""
    gl = holdings.compute(98.0, 0, 106.4)
    assert gl.total is None and gl.per_share == pytest.approx(8.4)


@pytest.mark.parametrize("paid,shares,now,expected", [
    (98.0, 12, 106.40, "+$100.80 (+8.57%)"),
    (120.0, 3, 100.00, "-$60.00 (-16.67%)"),
    (590.0, None, 612.10, "+$22.10/share (+3.75%)"),
    (590.0, None, 500.00, "-$90.00/share (-15.25%)"),
    (100.0, 1, 100.00, "+$0.00 (+0.00%)"),
])
def test_the_summary_phrase(paid, shares, now, expected):
    """One phrase, used by both the page and the email, so the two cannot
    describe the same position differently."""
    assert holdings.summary(holdings.compute(paid, shares, now)) == expected


def test_large_positions_are_grouped_for_reading():
    gl = holdings.compute(1000.0, 2500, 1400.0)
    assert holdings.summary(gl) == "+$1,000,000.00 (+40.00%)"


# ---------------------------------------------------------------------------
# The API: set, change, clear
# ---------------------------------------------------------------------------


def test_set_a_price_and_see_the_gain(signed_in):
    seed()
    _enable(signed_in)
    c, h = signed_in["client"], signed_in["auth"]
    tid = _watch(signed_in, "AAPL")
    _price(tid, 106.40, signed_in["user"]["id"])

    r = c.put(f"/holdings/{tid}", json={"purchase_price": 98.0, "shares": 12}, headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["purchase_price"] == 98.0 and body["shares"] == 12
    gl = body["gain_loss"]
    assert gl["total"] == pytest.approx(100.80)
    assert gl["pct"] == pytest.approx(8.5714, abs=1e-3)
    assert gl["cost_basis"] == pytest.approx(1176.0)
    assert gl["market_value"] == pytest.approx(1276.80)
    assert gl["is_gain"] is True
    assert gl["summary"] == "+$100.80 (+8.57%)"

    assert c.get("/holdings", headers=h).json() == [body]


def test_a_price_is_editable_at_any_time(signed_in):
    """The user asked for this explicitly. A second purchase, a stock split
    or a simple typo all mean the same call."""
    seed()
    _enable(signed_in)
    c, h = signed_in["client"], signed_in["auth"]
    tid = _watch(signed_in, "AAPL")
    _price(tid, 106.40, signed_in["user"]["id"])

    c.put(f"/holdings/{tid}", json={"purchase_price": 98.0, "shares": 12}, headers=h)
    r = c.put(f"/holdings/{tid}", json={"purchase_price": 101.5, "shares": 20}, headers=h)
    assert r.status_code == 200
    assert r.json()["purchase_price"] == 101.5 and r.json()["shares"] == 20
    assert len(_rows(Holding)) == 1, "editing created a second row"


def test_shares_can_be_cleared_without_losing_the_price(signed_in):
    """Going back to percentages only. Omitting `shares` means "I'd rather
    not say", so it must clear a count that was there before."""
    seed()
    _enable(signed_in)
    c, h = signed_in["client"], signed_in["auth"]
    tid = _watch(signed_in, "AAPL")
    _price(tid, 106.40, signed_in["user"]["id"])

    c.put(f"/holdings/{tid}", json={"purchase_price": 98.0, "shares": 12}, headers=h)
    r = c.put(f"/holdings/{tid}", json={"purchase_price": 98.0}, headers=h)
    assert r.json()["shares"] is None
    assert r.json()["gain_loss"]["total"] is None
    assert r.json()["gain_loss"]["summary"].endswith("/share (+8.57%)")


def test_a_priced_stock_with_no_price_data_yet_says_so(signed_in):
    """Added today, or the batch has not run. The row exists, the gain does
    not, and the client is told the difference."""
    seed()
    _enable(signed_in)
    c, h = signed_in["client"], signed_in["auth"]
    tid = _watch(signed_in, "AAPL")  # no TrendAnalysis row

    r = c.put(f"/holdings/{tid}", json={"purchase_price": 98.0, "shares": 12}, headers=h)
    assert r.status_code == 200
    assert r.json()["gain_loss"] is None


def test_removing_a_price_keeps_the_stock(signed_in):
    seed()
    _enable(signed_in)
    c, h = signed_in["client"], signed_in["auth"]
    tid = _watch(signed_in, "AAPL")
    c.put(f"/holdings/{tid}", json={"purchase_price": 98.0}, headers=h)

    assert c.delete(f"/holdings/{tid}", headers=h).status_code == 204
    assert _rows(Holding) == []
    # Deleting again is still 204: the user asked for there to be no price.
    assert c.delete(f"/holdings/{tid}", headers=h).status_code == 204
    assert any(w["ticker"]["id"] == tid for w in c.get("/watchlist", headers=h).json())


def test_removing_the_stock_removes_the_price(signed_in):
    """Holdings are keyed by (user, ticker). A price left behind would come
    back months later against a purchase the user no longer holds."""
    seed()
    _enable(signed_in)
    c, h = signed_in["client"], signed_in["auth"]
    r = c.post("/watchlist", json={"symbol": "AAPL"}, headers=h)
    item_id, tid = r.json()["id"], r.json()["ticker"]["id"]
    c.put(f"/holdings/{tid}", json={"purchase_price": 98.0}, headers=h)

    assert c.delete(f"/watchlist/{item_id}", headers=h).status_code == 204
    assert _rows(Holding) == []

    # Re-adding starts clean rather than resurrecting the old price.
    c.post("/watchlist", json={"symbol": "AAPL"}, headers=h)
    assert c.get("/holdings", headers=h).json() == []


def test_a_price_needs_the_stock_on_the_watchlist(signed_in):
    """Otherwise a caller could store prices against stocks that appear
    nowhere — rows nothing would ever clean up, because the cleanup happens
    when a watchlist row is removed."""
    seed()
    _enable(signed_in)
    db = get_session_factory()()
    other = db.execute(select(Ticker).where(Ticker.symbol == "MSFT")).scalar_one().id
    db.close()

    r = signed_in["client"].put(
        f"/holdings/{other}", json={"purchase_price": 300.0}, headers=signed_in["auth"]
    )
    assert r.status_code == 404
    assert _rows(Holding) == []


@pytest.mark.parametrize("payload", [
    {"purchase_price": 0},            # divides by zero in the percentage
    {"purchase_price": -10},          # not a price
    {"purchase_price": 98.0, "shares": 0},    # a real position is not zero shares
    {"purchase_price": 98.0, "shares": -5},
    {"shares": 10},                   # no price at all
    {"purchase_price": 98.0, "shares": 1e12},   # a typo, not a portfolio
    {"purchase_price": 1e9},
    {"purchase_price": "lots"},
])
def test_nonsense_values_are_rejected(signed_in, payload):
    """The bounds are not opinions about anyone's portfolio: they stop a
    pasted typo from rendering a nonsense total in an email."""
    seed()
    _enable(signed_in)
    tid = _watch(signed_in, "AAPL")
    r = signed_in["client"].put(f"/holdings/{tid}", json=payload, headers=signed_in["auth"])
    assert r.status_code == 422, r.text
    assert _rows(Holding) == []


def test_every_holdings_endpoint_needs_a_session(client):
    """A purchase price is not readable or writable from an unauthenticated
    call, and not from an email-footer token either — only a real sign-in."""
    for method, path in [
        ("GET", "/holdings"), ("PUT", "/holdings/1"), ("DELETE", "/holdings/1"),
    ]:
        r = client.request(method, path, json={"purchase_price": 1.0})
        assert r.status_code in (401, 403), f"{method} {path} returned {r.status_code}"
    # The signed settings token from an email footer authorises the switch,
    # but never the prices themselves.
    token = issue_token(SETTINGS_PURPOSE, {"uid": 1})
    r = client.request("PUT", f"/holdings/1?token={token}", json={"purchase_price": 1.0})
    assert r.status_code in (401, 403)


# ---------------------------------------------------------------------------
# 3. Privacy
# ---------------------------------------------------------------------------


def test_one_user_cannot_see_or_touch_another_users_prices(client):
    """The sharpest version of the privacy requirement."""
    seed()

    def sign_in(email: str) -> dict:
        client.post("/auth/request-link", json={"email": email})
        body = client.sent_emails[-1]["body_text"]
        token = body.split("token=")[1].split("\n")[0].strip()
        data = client.post("/auth/verify", json={"token": token}).json()
        return {"client": client, "user": data["user"],
                "auth": {"Authorization": f"Bearer {data['session_token']}"}}

    alice = sign_in("alice@example.com")
    bob = sign_in("bob@example.com")
    _enable(alice)
    _enable(bob)
    tid = _watch(alice, "AAPL")
    client.put(f"/holdings/{tid}", json={"purchase_price": 98.0, "shares": 12},
               headers=alice["auth"])

    # Bob sees nothing of Alice's, even for the same ticker.
    assert client.get("/holdings", headers=bob["auth"]).json() == []
    # And cannot write to it: it is not on his watchlist.
    assert client.put(f"/holdings/{tid}", json={"purchase_price": 1.0},
                      headers=bob["auth"]).status_code == 404
    # Nor delete it. 204 is honest — *his* price for it is gone (there was
    # none) — but Alice's must survive.
    client.delete(f"/holdings/{tid}", headers=bob["auth"])
    rows = _rows(Holding)
    assert len(rows) == 1 and rows[0].user_id == alice["user"]["id"]


def test_setting_a_price_writes_nothing_to_the_logs(signed_in, caplog):
    """A purchase price in `info_logs` is a family's position on the admin
    dashboard. Checked at DEBUG so nothing anywhere escapes."""
    seed()
    _enable(signed_in)
    tid = _watch(signed_in, "AAPL")
    _price(tid, 106.40, signed_in["user"]["id"])

    with caplog.at_level(logging.DEBUG):
        signed_in["client"].put(
            f"/holdings/{tid}", json={"purchase_price": 1234.56, "shares": 789},
            headers=signed_in["auth"],
        )
    logged = " ".join(r.getMessage() for r in caplog.records)
    for secret in ("1234.56", "1234", "789"):
        assert secret not in logged, f"{secret!r} reached the logs"


def test_the_ai_story_pipeline_is_never_given_a_purchase_price():
    """Stories go to a third-party model. Enforced by reading the source:
    a future edit that passes a holding into a prompt fails here."""
    import inspect
    from app.services.articles import prompts, service as articles_service

    for mod in (articles_service, prompts):
        src = inspect.getsource(mod)
        for word in ("purchase_price", "Holding", "holdings"):
            assert word not in src, (
                f"{mod.__name__} mentions {word!r} — a purchase price must "
                "never reach the AI pipeline"
            )


def test_data_lens_hides_purchase_prices_by_default(monkeypatch):
    """The admin SQL console. Hidden rather than blocked outright, because
    it has real diagnostic use — but not on a screen-share."""
    from app.core.settings import get_settings
    from app.services import data_lens

    get_settings.cache_clear()
    assert "purchase_price" in data_lens.blocked_columns()
    assert "shares" in data_lens.blocked_columns()

    with pytest.raises(data_lens.QueryRejected) as e:
        data_lens.validate("SELECT purchase_price FROM holdings", limit=10)
    assert "private" in str(e.value.message).lower()
    assert "DATA_LENS_SHOW_HOLDINGS" in e.value.hint

    # Not even offered to autocomplete or the schema sidebar.
    cat = data_lens.schema_catalogue()
    assert "purchase_price" in cat["blocked_columns"]
    holdings_tbl = next(t for t in cat["tables"] if t["table"] == "holdings")
    names = [c["name"] for c in holdings_tbl["columns"]]
    assert "purchase_price" not in names and "shares" not in names
    assert "ticker_id" in names, "the table itself is still queryable"


def test_data_lens_shows_purchase_prices_when_deliberately_turned_on(monkeypatch):
    from app.core.settings import get_settings
    from app.services import data_lens

    monkeypatch.setenv("DATA_LENS_SHOW_HOLDINGS", "true")
    get_settings.cache_clear()
    assert "purchase_price" not in data_lens.blocked_columns()
    # Still never the credential column.
    assert "token_hash" in data_lens.blocked_columns()
    plan = data_lens.validate("SELECT purchase_price FROM holdings", limit=10)
    assert "holdings" in plan.tables


# ---------------------------------------------------------------------------
# The daily recap carries it too
# ---------------------------------------------------------------------------


def _recap_for(uid: int, symbol: str, paid: float, shares: float | None) -> None:
    db = get_session_factory()()
    tid = db.execute(select(Ticker).where(Ticker.symbol == symbol)).scalar_one().id
    db.add(HoldingsOptIn(user_id=uid))
    db.add(Holding(user_id=uid, ticker_id=tid, purchase_price=paid, shares=shares))
    db.commit()
    db.close()


def test_the_recap_shows_the_gain_for_a_priced_stock(client):
    """Email-first app: the number has to be in the inbox, not only on the
    page, and it has to be the same number."""
    from app.jobs.recap import run_watchlist_recap

    uid = _recap_user("parent@example.com", ["AAPL"])
    _closes("AAPL", today=106.40, prev=100.0)
    _recap_for(uid, "AAPL", paid=98.0, shares=12)

    run_watchlist_recap()

    email = client.sent_emails[-1]
    assert "you paid $98.00" in email["body_text"]
    assert "+$100.80 (+8.57%)" in email["body_text"]
    assert "+$100.80 (+8.57%)" in email["body_html"]
    # The run total, for someone holding several.
    assert "Total on the stocks you've priced: +$100.80" in email["body_text"]


def test_the_recap_says_nothing_about_prices_when_the_feature_is_off(client):
    """The default. A recap must look exactly as it did before for everyone
    who never asked for this."""
    from app.jobs.recap import run_watchlist_recap

    uid = _recap_user("parent@example.com", ["AAPL"])
    _closes("AAPL", today=106.40, prev=100.0)
    # A price on file, but the switch is off.
    db = get_session_factory()()
    tid = db.execute(select(Ticker).where(Ticker.symbol == "AAPL")).scalar_one().id
    db.add(Holding(user_id=uid, ticker_id=tid, purchase_price=98.0, shares=12))
    db.commit()
    db.close()

    run_watchlist_recap()

    email = client.sent_emails[-1]
    assert "you paid" not in email["body_text"]
    assert "98.00" not in email["body_text"]
    assert "you paid" not in email["body_html"]


def test_the_recap_prices_only_the_stocks_that_have_one(client):
    """A half-filled watchlist shows the comparison for the half that has
    prices, rather than all or nothing."""
    from app.jobs.recap import run_watchlist_recap

    uid = _recap_user("parent@example.com", ["AAPL", "MSFT"])
    _closes("AAPL", today=106.40, prev=100.0)
    _closes("MSFT", today=420.0, prev=400.0)
    _recap_for(uid, "AAPL", paid=98.0, shares=12)

    run_watchlist_recap()

    text = client.sent_emails[-1]["body_text"]
    assert "AAPL" in text and "MSFT" in text
    assert text.count("you paid") == 1


def test_the_recap_omits_a_total_when_no_shares_were_given(client):
    """Percentages only. A total would have to invent a share count."""
    from app.jobs.recap import run_watchlist_recap

    uid = _recap_user("parent@example.com", ["AAPL"])
    _closes("AAPL", today=106.40, prev=100.0)
    _recap_for(uid, "AAPL", paid=98.0, shares=None)

    run_watchlist_recap()

    text = client.sent_emails[-1]["body_text"]
    assert "+$8.40/share (+8.57%)" in text
    assert "Total on the stocks" not in text


def test_a_loss_is_rendered_as_a_loss_in_the_recap(client):
    from app.jobs.recap import run_watchlist_recap

    uid = _recap_user("parent@example.com", ["AAPL"])
    _closes("AAPL", today=90.0, prev=100.0)
    _recap_for(uid, "AAPL", paid=120.0, shares=3)

    run_watchlist_recap()

    email = client.sent_emails[-1]
    assert "-$90.00 (-25.00%)" in email["body_text"]
    # Red, not green, in the HTML version.
    from app.jobs.recap import _DOWN
    assert _DOWN in email["body_html"]


def test_a_recap_send_failure_still_mentions_no_price(client, monkeypatch):
    """Belt and braces on the privacy rule: the one place a recap's contents
    get written to the database is a failed send, and the detail recorded
    there must not carry the position either."""
    from app.jobs import recap as recap_mod
    from app.jobs.recap import run_watchlist_recap
    from app.models import DeliveryOutcome
    from app.services.emailer import EmailSendError

    uid = _recap_user("parent@example.com", ["AAPL"])
    _closes("AAPL", today=106.40, prev=100.0)
    _recap_for(uid, "AAPL", paid=98.0, shares=12)

    def boom(*a, **k):
        raise EmailSendError("SMTPServerDisconnected: timed out")

    monkeypatch.setattr(recap_mod, "send_email", boom)
    run_watchlist_recap()

    for row in _rows(DeliveryOutcome):
        assert "98.0" not in (row.detail or "")
        assert "100.80" not in (row.detail or "")
