"""Coverage for the stock-data provider abstraction.

We monkey-patch the HTTP layer (httpx for finnhub, yfinance for yfinance)
so these tests never make real network calls.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.core.settings import get_settings


# ---------------------------------------------------------------------------
# Provider selection
# ---------------------------------------------------------------------------


def test_default_provider_is_yfinance(client):
    assert get_settings().stock_data_provider == "yfinance"


# ---------------------------------------------------------------------------
# Finnhub backend
# ---------------------------------------------------------------------------


def _patch_finnhub(monkeypatch, response_data: dict | None, status_code: int = 200):
    """Replace httpx.get inside the stock_data module with a fake."""
    from app.services import stock_data as sd

    class FakeResponse:
        def __init__(self, data, status):
            self._data = data
            self.status_code = status
            self.text = "" if data is None else str(data)[:200]

        def json(self):
            if self._data is None:
                raise ValueError("no body")
            return self._data

    def fake_get(url, params=None, timeout=None):
        return FakeResponse(response_data, status_code)

    monkeypatch.setattr(sd.httpx, "get", fake_get)


def test_finnhub_quote_returns_current_price(client, monkeypatch):
    monkeypatch.setenv("STOCK_DATA_PROVIDER", "finnhub")
    monkeypatch.setenv("FINNHUB_API_KEY", "test-key")
    get_settings.cache_clear()

    _patch_finnhub(monkeypatch, {"c": 165.10, "pc": 164.50, "h": 166, "l": 164, "o": 165, "t": 1700000000})

    from app.services.stock_data import get_current_price
    assert get_current_price("AAPL") == pytest.approx(165.10)


def test_finnhub_previous_close(client, monkeypatch):
    monkeypatch.setenv("STOCK_DATA_PROVIDER", "finnhub")
    monkeypatch.setenv("FINNHUB_API_KEY", "test-key")
    get_settings.cache_clear()

    _patch_finnhub(monkeypatch, {"c": 165.10, "pc": 164.50})

    from app.services.stock_data import get_previous_close
    assert get_previous_close("AAPL") == pytest.approx(164.50)


def test_finnhub_history_parses_candles(client, monkeypatch):
    monkeypatch.setenv("STOCK_DATA_PROVIDER", "finnhub")
    monkeypatch.setenv("FINNHUB_API_KEY", "test-key")
    get_settings.cache_clear()

    # Two-day window: two closes, two timestamps (unix UTC).
    t1 = int(datetime(2026, 5, 30, tzinfo=timezone.utc).timestamp())
    t2 = int(datetime(2026, 6, 2, tzinfo=timezone.utc).timestamp())
    _patch_finnhub(monkeypatch, {"s": "ok", "c": [100.0, 101.0], "t": [t1, t2]})

    from app.services.stock_data import get_daily_history
    hist = get_daily_history("AAPL")
    assert hist == [(date(2026, 5, 30), 100.0), (date(2026, 6, 2), 101.0)]


def test_finnhub_returns_none_on_zero_quote(client, monkeypatch):
    """Finnhub returns c=0 when the symbol is unknown — treat as None."""
    monkeypatch.setenv("STOCK_DATA_PROVIDER", "finnhub")
    monkeypatch.setenv("FINNHUB_API_KEY", "test-key")
    get_settings.cache_clear()
    _patch_finnhub(monkeypatch, {"c": 0, "pc": 0})
    from app.services.stock_data import get_current_price, get_previous_close
    assert get_current_price("ZZZZ") is None
    assert get_previous_close("ZZZZ") is None


def test_finnhub_requires_api_key(client, monkeypatch, caplog):
    """Selecting finnhub without a key produces an error log and returns None."""
    monkeypatch.setenv("STOCK_DATA_PROVIDER", "finnhub")
    monkeypatch.setenv("FINNHUB_API_KEY", "")
    get_settings.cache_clear()

    from app.services.stock_data import get_current_price
    with caplog.at_level("ERROR", logger="app.services.stock_data"):
        assert get_current_price("AAPL") is None
    assert any("FINNHUB_API_KEY" in r.message for r in caplog.records)


def test_finnhub_handles_http_error(client, monkeypatch):
    monkeypatch.setenv("STOCK_DATA_PROVIDER", "finnhub")
    monkeypatch.setenv("FINNHUB_API_KEY", "test-key")
    get_settings.cache_clear()
    _patch_finnhub(monkeypatch, {"error": "rate limit"}, status_code=429)
    from app.services.stock_data import get_current_price
    assert get_current_price("AAPL") is None


def test_finnhub_history_returns_none_on_no_data_status(client, monkeypatch):
    monkeypatch.setenv("STOCK_DATA_PROVIDER", "finnhub")
    monkeypatch.setenv("FINNHUB_API_KEY", "test-key")
    get_settings.cache_clear()
    _patch_finnhub(monkeypatch, {"s": "no_data", "c": [], "t": []})
    from app.services.stock_data import get_daily_history
    assert get_daily_history("ZZZZ") is None
