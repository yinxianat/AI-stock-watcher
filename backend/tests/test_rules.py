from app.db.seed import seed


def _add_watch(client, headers, symbol):
    r = client.post("/watchlist", json={"symbol": symbol}, headers=headers)
    assert r.status_code in (200, 201), r.text
    return r.json()["ticker"]["id"]


def test_rule_upsert_validates_band(signed_in):
    seed()
    c, h = signed_in["client"], signed_in["auth"]
    tid = _add_watch(c, h, "AAPL")

    # PRICE_CHANGE_RANGE requires both bounds; low<high.
    r = c.post(
        "/rules",
        json={"ticker_id": tid, "event_type": "price_change_range"},
        headers=h,
    )
    assert r.status_code == 422

    r2 = c.post(
        "/rules",
        json={
            "ticker_id": tid,
            "event_type": "price_change_range",
            "pct_low": 5,
            "pct_high": -5,
        },
        headers=h,
    )
    assert r2.status_code == 422


def test_rule_upsert_and_delete(signed_in):
    seed()
    c, h = signed_in["client"], signed_in["auth"]
    tid = _add_watch(c, h, "MSFT")

    r = c.post(
        "/rules",
        json={
            "ticker_id": tid,
            "event_type": "price_change_range",
            "pct_low": -3,
            "pct_high": 3,
        },
        headers=h,
    )
    assert r.status_code == 201
    rule_id = r.json()["id"]

    # Upserting same (ticker, event) updates in place.
    r2 = c.post(
        "/rules",
        json={
            "ticker_id": tid,
            "event_type": "price_change_range",
            "pct_low": -2,
            "pct_high": 2,
        },
        headers=h,
    )
    assert r2.json()["id"] == rule_id
    assert r2.json()["pct_low"] == -2

    rd = c.delete(f"/rules/{rule_id}", headers=h)
    assert rd.status_code == 204
