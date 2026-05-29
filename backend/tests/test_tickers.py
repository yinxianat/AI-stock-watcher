from app.db.seed import seed
from app.db.database import reset_engine


def _seed(client):
    # `client` fixture has already patched the engine; just run seed.
    seed()


def test_list_seeded_tickers(client):
    _seed(client)
    r = client.get("/tickers")
    assert r.status_code == 200
    symbols = [t["symbol"] for t in r.json()]
    assert "SPY" in symbols and "AAPL" in symbols
    assert len(symbols) >= 40


def test_search_autocomplete_prefers_symbol_prefix(client):
    _seed(client)
    r = client.get("/tickers/search", params={"q": "AA"})
    assert r.status_code == 200
    syms = [t["symbol"] for t in r.json()]
    # AAPL should appear because it starts with AA
    assert "AAPL" in syms


def test_search_finds_by_name_substring(client):
    _seed(client)
    r = client.get("/tickers/search", params={"q": "vanguard"})
    syms = [t["symbol"] for t in r.json()]
    assert any(s in syms for s in ("VOO", "VTI", "VEA", "VWO", "BND", "VNQ"))
