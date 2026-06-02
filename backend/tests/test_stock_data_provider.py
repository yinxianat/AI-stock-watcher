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


def test_default_provider_is_yfinance(client, monkeypatch):
    """When STOCK_DATA_PROVIDER is unset (delete from env so .env isn't
    consulted in test runs), the Pydantic default is 'yfinance'."""
    monkeypatch.delenv("STOCK_DATA_PROVIDER", raising=False)
    # Also stop pydantic-settings from reading the dev .env file in tests.
    from app.core import settings as settings_mod
    monkeypatch.setattr(
        settings_mod.Settings, "model_config",
        {**settings_mod.Settings.model_config, "env_file": None},
    )
    get_settings.cache_clear()
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


def _finnhub_history_fixture(monkeypatch, body=None, status=200):
    """Set provider=finnhub and stub httpx.get to return `body` from Twelve Data."""
    monkeypatch.setenv("STOCK_DATA_PROVIDER", "finnhub")
    monkeypatch.setenv("FINNHUB_API_KEY", "test-key")
    monkeypatch.setenv("TWELVEDATA_API_KEY", "td-key")
    get_settings.cache_clear()

    from app.services import stock_data as sd

    captured: list[tuple[str, dict]] = []

    class FakeResponse:
        def __init__(self):
            self.status_code = status
            import json
            self._body = body
            self.text = json.dumps(body) if isinstance(body, dict) else (body or "")

        def json(self):
            if isinstance(self._body, dict):
                return self._body
            import json
            return json.loads(self.text)

    def fake_get(url, params=None, timeout=None, headers=None, follow_redirects=False):
        captured.append((url, dict(params or {})))
        return FakeResponse()

    monkeypatch.setattr(sd.httpx, "get", fake_get)
    return sd, captured


def test_finnhub_history_routes_through_twelvedata(client, monkeypatch):
    """Daily history under STOCK_DATA_PROVIDER=finnhub must NOT call
    Finnhub /stock/candle (paid). It must call Twelve Data /time_series
    and parse the JSON into ascending-date (date, close) tuples."""
    body = {
        "meta": {"symbol": "AAPL", "interval": "1day"},
        "status": "ok",
        # Twelve Data returns newest-first; the parser must sort ascending.
        "values": [
            {"datetime": "2026-06-01", "open": "100.5", "high": "102",
             "low": "100", "close": "101.0", "volume": "1200"},
            {"datetime": "2026-05-30", "open": "100", "high": "101",
             "low": "99", "close": "100.5", "volume": "1000"},
        ],
    }
    sd, captured = _finnhub_history_fixture(monkeypatch, body=body)

    hist = sd.get_daily_history("AAPL")
    assert hist == [(date(2026, 5, 30), 100.5), (date(2026, 6, 1), 101.0)]
    assert any("twelvedata.com" in u for (u, _) in captured)
    assert all("/stock/candle" not in u for (u, _) in captured)
    _, params = captured[-1]
    assert params.get("symbol") == "AAPL"
    assert params.get("apikey") == "td-key"
    assert params.get("interval") == "1day"


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


def test_twelvedata_returns_none_on_error_status(client, monkeypatch):
    """Twelve Data returns {status: 'error', message: ...} for bad symbols."""
    sd, _ = _finnhub_history_fixture(
        monkeypatch,
        body={"code": 400, "status": "error", "message": "symbol not found"},
    )
    assert sd.get_daily_history("ZZZZ") is None


def test_twelvedata_returns_none_on_missing_values_array(client, monkeypatch):
    sd, _ = _finnhub_history_fixture(monkeypatch, body={"status": "ok"})
    assert sd.get_daily_history("ZZZZ") is None


def test_twelvedata_filters_to_last_year(client, monkeypatch):
    """Twelve Data returns the requested outputsize; we still defensively
    filter rows older than 365 days (e.g. if user changes outputsize)."""
    from datetime import date as _d, timedelta as _td

    today = _d.today()
    old_row = today - _td(days=400)
    new_row = today - _td(days=10)
    body = {
        "status": "ok",
        "values": [
            {"datetime": new_row.isoformat(), "close": "20"},
            {"datetime": old_row.isoformat(), "close": "10"},
        ],
    }
    sd, _ = _finnhub_history_fixture(monkeypatch, body=body)
    hist = sd.get_daily_history("AAPL")
    assert hist == [(new_row, 20.0)]  # old row dropped


def test_history_returns_none_when_twelvedata_key_missing(client, monkeypatch, caplog):
    monkeypatch.setenv("STOCK_DATA_PROVIDER", "finnhub")
    monkeypatch.setenv("FINNHUB_API_KEY", "fh-key")
    monkeypatch.setenv("TWELVEDATA_API_KEY", "")
    get_settings.cache_clear()
    from app.services.stock_data import get_daily_history
    with caplog.at_level("WARNING", logger="app.services.stock_data"):
        assert get_daily_history("AAPL") is None
    assert any("TWELVEDATA_API_KEY" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# validate_provider — health check the user can run from the CLI
# ---------------------------------------------------------------------------


def test_validate_provider_reports_ok_when_everything_works(client, monkeypatch):
    monkeypatch.setenv("STOCK_DATA_PROVIDER", "finnhub")
    monkeypatch.setenv("FINNHUB_API_KEY", "fh-key")
    monkeypatch.setenv("TWELVEDATA_API_KEY", "td-key")
    get_settings.cache_clear()

    from app.services import stock_data as sd

    today = date.today()
    finnhub_body = {"c": 100.5, "pc": 99.5, "h": 101, "l": 99,
                    "o": 100, "t": 1700000000}
    twelvedata_body = {
        "status": "ok",
        "values": [{"datetime": today.isoformat(), "close": "100.0"}],
    }

    class FakeFinnhub:
        status_code = 200
        text = "{}"
        def json(self):
            return finnhub_body

    class FakeTwelveData:
        status_code = 200
        text = "{}"
        def json(self):
            return twelvedata_body

    def fake_get(url, params=None, timeout=None, headers=None, follow_redirects=False):
        if "finnhub" in url:
            return FakeFinnhub()
        return FakeTwelveData()

    monkeypatch.setattr(sd.httpx, "get", fake_get)

    result = sd.validate_provider("AAPL")
    assert result["ok"] is True
    assert result["current_price"] == 100.5
    assert result["previous_close"] == 99.5
    assert result["history_rows"] == 1


def test_validate_provider_flags_missing_key(client, monkeypatch):
    monkeypatch.setenv("STOCK_DATA_PROVIDER", "finnhub")
    monkeypatch.setenv("FINNHUB_API_KEY", "")
    monkeypatch.setenv("TWELVEDATA_API_KEY", "")
    get_settings.cache_clear()

    from app.services import stock_data as sd

    result = sd.validate_provider("AAPL")
    assert result["ok"] is False
    assert result["finnhub_key_present"] is False
