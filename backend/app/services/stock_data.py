"""Stock-data provider abstraction.

The ingest and intraday jobs call:

* `get_current_price(symbol) -> float | None`
* `get_daily_history(symbol) -> list[(date, close)] | None`

Both helpers dispatch to whatever provider `STOCK_DATA_PROVIDER` selects:

* `"yfinance"` (default) — uses the `yfinance` library. Free, no key, but
  Yahoo's anti-bot measures break it periodically. Used to be the default
  for v1 of this app; kept for backward compatibility.
* `"finnhub"` — uses the official Finnhub REST API. Requires
  `FINNHUB_API_KEY`. Free tier is 60 calls/min — comfortable for the
  default schedule (50 watched tickers × every 10 min during market hours
  = ~50 calls/tick, well under the limit).

Adding a new provider: implement `_get_current_price_X` and
`_get_daily_history_X`, then route them in the public functions below.
"""

from __future__ import annotations

import logging
from datetime import date as date_, datetime, timedelta, timezone
from typing import Iterable

import httpx

from app.core.settings import get_settings

log = logging.getLogger(__name__)

# httpx timeout for all upstream calls. Kept short so a hung provider can't
# stall an entire ingest run.
_HTTP_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=10.0)


# ---------------------------------------------------------------------------
# Public entrypoints
# ---------------------------------------------------------------------------


def get_current_price(symbol: str) -> float | None:
    """Latest trade price for `symbol`, or None on any failure.

    Provider-agnostic; never raises. Used by `app.jobs.intraday`.
    """
    provider = get_settings().stock_data_provider.lower()
    try:
        if provider == "finnhub":
            return _quote_finnhub(symbol)
        return _quote_yfinance(symbol)
    except Exception as e:  # noqa: BLE001
        log.warning("get_current_price(%s) via %s failed: %s", symbol, provider, e)
        return None


def get_daily_history(symbol: str) -> list[tuple[date_, float]] | None:
    """1-year of daily (date, close) pairs, ascending. None on failure.

    Used by `app.jobs.ingest` and `app.jobs.on_demand`.
    """
    provider = get_settings().stock_data_provider.lower()
    try:
        if provider == "finnhub":
            return _history_finnhub(symbol)
        return _history_yfinance(symbol)
    except Exception as e:  # noqa: BLE001
        log.warning("get_daily_history(%s) via %s failed: %s", symbol, provider, e)
        return None


def get_previous_close(symbol: str) -> float | None:
    """Yesterday's regular-session close, or None.

    Used by the sign-in backfill (auth verify) and the daily-baseline
    intraday alert when the local `daily_closes` table doesn't have
    today-minus-1 yet.
    """
    provider = get_settings().stock_data_provider.lower()
    try:
        if provider == "finnhub":
            return _prev_close_finnhub(symbol)
        return _prev_close_yfinance(symbol)
    except Exception as e:  # noqa: BLE001
        log.warning("get_previous_close(%s) via %s failed: %s", symbol, provider, e)
        return None


# ---------------------------------------------------------------------------
# yfinance backend
# ---------------------------------------------------------------------------


def _yf_history(symbol: str, period: str = "1y", interval: str = "1d"):
    import yfinance as yf

    return yf.Ticker(symbol).history(period=period, interval=interval, auto_adjust=False)


def _quote_yfinance(symbol: str) -> float | None:
    import yfinance as yf

    try:
        info = yf.Ticker(symbol).fast_info
    except Exception as e:
        log.warning("yfinance fast_info failed for %s: %s", symbol, e)
        return None
    for key in ("last_price", "regularMarketPrice", "regular_market_price"):
        v = info.get(key) if hasattr(info, "get") else None
        if v is not None:
            try:
                fv = float(v)
                if fv == fv and fv > 0:
                    return fv
            except (TypeError, ValueError):
                continue
    return None


def _history_yfinance(symbol: str) -> list[tuple[date_, float]] | None:
    hist = _yf_history(symbol)
    if hist is None or hist.empty:
        return None
    closes = hist["Close"].dropna()
    if closes.empty:
        return None
    out: list[tuple[date_, float]] = []
    for idx, val in closes.items():
        try:
            d = idx.date() if hasattr(idx, "date") else date_.fromisoformat(str(idx)[:10])
            out.append((d, float(val)))
        except Exception:
            continue
    return out or None


def _prev_close_yfinance(symbol: str) -> float | None:
    """Use fast_info.previous_close, fall back to history."""
    import yfinance as yf

    try:
        fi = yf.Ticker(symbol).fast_info
        for key in ("previous_close", "regularMarketPreviousClose"):
            v = fi.get(key) if hasattr(fi, "get") else None
            if v is not None:
                try:
                    fv = float(v)
                    if fv == fv and fv > 0:
                        return fv
                except (TypeError, ValueError):
                    pass
    except Exception:
        pass
    hist = _history_yfinance(symbol)
    if hist is None or len(hist) < 2:
        return None
    return hist[-2][1]  # second-to-last close


# ---------------------------------------------------------------------------
# Finnhub backend
# ---------------------------------------------------------------------------

_FINNHUB_BASE = "https://finnhub.io/api/v1"


def _finnhub_key() -> str | None:
    return (get_settings().finnhub_api_key or "").strip() or None


def _finnhub_get(path: str, params: dict) -> dict | None:
    key = _finnhub_key()
    if not key:
        log.error("Finnhub provider selected but FINNHUB_API_KEY is not set")
        return None
    params = dict(params)
    params["token"] = key
    try:
        r = httpx.get(_FINNHUB_BASE + path, params=params, timeout=_HTTP_TIMEOUT)
    except httpx.HTTPError as e:
        log.warning("Finnhub %s failed: %s", path, e)
        return None
    if r.status_code != 200:
        log.warning("Finnhub %s returned HTTP %d: %s", path, r.status_code, r.text[:160])
        return None
    try:
        return r.json()
    except Exception as e:  # noqa: BLE001
        log.warning("Finnhub %s non-JSON body: %s", path, e)
        return None


def _quote_finnhub(symbol: str) -> float | None:
    data = _finnhub_get("/quote", {"symbol": symbol})
    if not data:
        return None
    # /quote returns {c: current, pc: prev close, h, l, o, t}
    c = data.get("c")
    if c in (None, 0):
        return None
    try:
        return float(c)
    except (TypeError, ValueError):
        return None


def _prev_close_finnhub(symbol: str) -> float | None:
    data = _finnhub_get("/quote", {"symbol": symbol})
    if not data:
        return None
    pc = data.get("pc")
    if pc in (None, 0):
        return None
    try:
        return float(pc)
    except (TypeError, ValueError):
        return None


def _history_finnhub(symbol: str) -> list[tuple[date_, float]] | None:
    now_ts = int(datetime.now(timezone.utc).timestamp())
    one_year_ago = now_ts - 365 * 24 * 60 * 60
    data = _finnhub_get(
        "/stock/candle",
        {"symbol": symbol, "resolution": "D", "from": one_year_ago, "to": now_ts},
    )
    if not data or data.get("s") != "ok":
        return None
    closes: Iterable = data.get("c") or []
    times: Iterable = data.get("t") or []
    out: list[tuple[date_, float]] = []
    for c, t in zip(closes, times):
        try:
            d = datetime.fromtimestamp(int(t), tz=timezone.utc).date()
            cv = float(c)
            if cv > 0:
                out.append((d, cv))
        except (TypeError, ValueError):
            continue
    return out or None
