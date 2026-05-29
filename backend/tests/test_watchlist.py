from app.db.seed import seed


def test_add_remove_watch_by_symbol(signed_in):
    seed()
    c = signed_in["client"]
    h = signed_in["auth"]

    r = c.post("/watchlist", json={"symbol": "aapl"}, headers=h)
    assert r.status_code == 201
    item = r.json()
    assert item["ticker"]["symbol"] == "AAPL"

    # Idempotent: adding again returns the same row (or a row with same ticker).
    r2 = c.post("/watchlist", json={"symbol": "AAPL"}, headers=h)
    assert r2.status_code in (200, 201)
    assert r2.json()["ticker"]["symbol"] == "AAPL"

    # List
    rl = c.get("/watchlist", headers=h)
    assert rl.status_code == 200
    assert any(w["ticker"]["symbol"] == "AAPL" for w in rl.json())

    # Delete
    rd = c.delete(f"/watchlist/{item['id']}", headers=h)
    assert rd.status_code == 204


def test_add_unknown_symbol_creates_unseeded_ticker(signed_in):
    seed()
    c = signed_in["client"]
    r = c.post("/watchlist", json={"symbol": "ZZZZ"}, headers=signed_in["auth"])
    assert r.status_code == 201
    assert r.json()["ticker"]["symbol"] == "ZZZZ"
