"""Stock-data provider abstraction.

The ingest and intraday jobs call:

* `get_current_price(symbol) -> float | None`
* `get_daily_history(symbol) -> list[(date, close)] | None`
* `get_previous_close(symbol) -> float | None`

Routing matrix:

  STOCK_DATA_PROVIDER=yfinance (default)
    quote      → yfinance fast_info
    history    → yfinance 1y daily
    prev close → yfinance fast_info, falls back to history

  STOCK_DATA_PROVIDER=finnhub
    quote      → Finnhub /quote               (free, 60 req/min)
    prev close → Finnhub /quote (`pc` field)  (free)
    history    → Twelve Data /time_series     (free, 800 req/day)

Two reasons history isn't on Finnhub here:

  1. Finnhub's `/stock/candle` is paid-only since 2024 (returns HTTP 403
     "You don't have access to this resource" on the free tier).
  2. Twelve Data's free tier (800 req/day, 8 req/min) easily covers a
     once-per-day ingest of ~50 watched tickers, with no captcha or weird
     registration flow — just sign up at https://twelvedata.com and paste
     the key into TWELVEDATA_API_KEY.

Adding a new provider: implement quote/prev-close/history functions and
route them in the public dispatchers below.
"""

from __future__ import annotations

import logging
from datetime import date as date_, datetime, timedelta, timezone

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

    Used by `app.jobs.ingest`, `app.jobs.on_demand`, and
    `app.services.signin_backfill`.
    """
    provider = get_settings().stock_data_provider.lower()
    try:
        if provider == "finnhub":
            # Finnhub's /stock/candle is paid-only since 2024; route history
            # through Twelve Data. /quote stays on Finnhub for live ticks.
            return _history_twelvedata(symbol)
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


# ---------------------------------------------------------------------------
# Twelve Data backend — daily history. Requires TWELVEDATA_API_KEY.
# ---------------------------------------------------------------------------

_TWELVEDATA_TIME_SERIES_URL = "https://api.twelvedata.com/time_series"
_TWELVEDATA_HISTORY_DAYS = 365


def _twelvedata_key() -> str | None:
    return (get_settings().twelvedata_api_key or "").strip() or None


def _fetch_twelvedata_raw(
    symbol: str,
) -> tuple[int | None, dict | str | None, str | None]:
    """Hit Twelve Data and return (status_code, json_or_text, error).

    Used by `_history_twelvedata` and the debug mode of `validate_provider`.
    """
    key = _twelvedata_key()
    if not key:
        return None, None, "TWELVEDATA_API_KEY not set"
    params = {
        "symbol": symbol,
        "interval": "1day",
        "outputsize": _TWELVEDATA_HISTORY_DAYS,
        "format": "JSON",
        "apikey": key,
    }
    try:
        r = httpx.get(
            _TWELVEDATA_TIME_SERIES_URL,
            params=params,
            timeout=_HTTP_TIMEOUT,
            headers={"Accept": "application/json"},
        )
    except httpx.HTTPError as e:
        return None, None, f"httpx error: {e}"
    if r.status_code != 200:
        return r.status_code, (r.text or "")[:300], f"HTTP {r.status_code}"
    try:
        return r.status_code, r.json(), None
    except Exception as e:  # noqa: BLE001
        return r.status_code, (r.text or "")[:300], f"non-JSON body: {e}"


def _parse_twelvedata_response(
    data: dict, symbol: str, cutoff: date_,
) -> tuple[list[tuple[date_, float]], str | None]:
    """Parse Twelve Data /time_series JSON.

    Success shape:
        {"meta": {...}, "values": [{"datetime": "2026-06-02", "close": "314.26", ...}],
         "status": "ok"}
    Error shape:
        {"code": 400, "message": "...", "status": "error"}
    """
    if not isinstance(data, dict):
        return [], "non-dict response"
    if data.get("status") == "error":
        return [], f"Twelve Data error: {data.get('message') or data!r}"
    values = data.get("values")
    if not isinstance(values, list):
        return [], f"no 'values' array in response: keys={sorted(data.keys())}"

    out: list[tuple[date_, float]] = []
    skipped = 0
    for item in values:
        if not isinstance(item, dict):
            skipped += 1
            continue
        try:
            d = date_.fromisoformat(item["datetime"][:10])
            close = float(item["close"])
        except (KeyError, ValueError, TypeError):
            skipped += 1
            continue
        if close <= 0 or d < cutoff:
            continue
        out.append((d, close))
    if not out and skipped:
        return [], f"all {skipped} values failed to parse"
    # Twelve Data returns newest-first; normalize to ascending date.
    out.sort(key=lambda p: p[0])
    return out, None


def _history_twelvedata(symbol: str) -> list[tuple[date_, float]] | None:
    status, body, http_err = _fetch_twelvedata_raw(symbol)
    if http_err is not None:
        log.warning("Twelve Data fetch failed for %s: %s", symbol, http_err)
        if isinstance(body, str):
            log.warning("Twelve Data body preview for %s: %s", symbol, body[:200])
        return None
    if not isinstance(body, dict):
        log.warning("Twelve Data unexpected body type for %s: %r", symbol, type(body))
        return None
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=_TWELVEDATA_HISTORY_DAYS)
    rows, err = _parse_twelvedata_response(body, symbol, cutoff)
    if err is not None:
        log.warning("Twelve Data parse failed for %s: %s", symbol, err)
        return None
    if not rows:
        log.warning(
            "Twelve Data returned 0 rows in trailing %dd window for %s",
            _TWELVEDATA_HISTORY_DAYS, symbol,
        )
        return None
    return rows


# ---------------------------------------------------------------------------
# Health check (used by `python -m app.services.stock_data`)
# ---------------------------------------------------------------------------


def validate_provider(symbol: str = "AAPL", debug: bool = False) -> dict:
    """One-shot health probe. Returns a dict you can print or log.

    Useful for the user to confirm their setup works without spinning up
    a full ingest run. Never raises. Pass `debug=True` to include raw
    upstream response bodies so a failure mode that the parser can't make
    sense of becomes inspectable.
    """
    s = get_settings()
    result: dict = {
        "provider": s.stock_data_provider,
        "finnhub_key_present": bool(s.finnhub_api_key),
        "symbol": symbol,
        "current_price": None,
        "previous_close": None,
        "history_rows": None,
        "history_first": None,
        "history_last": None,
        "ok": False,
        "messages": [],
    }
    try:
        result["current_price"] = get_current_price(symbol)
        if result["current_price"] is None:
            result["messages"].append(f"get_current_price({symbol}) returned None")
    except Exception as e:  # noqa: BLE001
        result["messages"].append(f"get_current_price raised: {e}")
    try:
        result["previous_close"] = get_previous_close(symbol)
        if result["previous_close"] is None:
            result["messages"].append(f"get_previous_close({symbol}) returned None")
    except Exception as e:  # noqa: BLE001
        result["messages"].append(f"get_previous_close raised: {e}")
    try:
        hist = get_daily_history(symbol)
        if hist:
            result["history_rows"] = len(hist)
            result["history_first"] = str(hist[0])
            result["history_last"] = str(hist[-1])
        else:
            result["messages"].append(f"get_daily_history({symbol}) returned None")
    except Exception as e:  # noqa: BLE001
        result["messages"].append(f"get_daily_history raised: {e}")

    if debug:
        result["debug"] = _debug_dump(symbol)

    result["ok"] = bool(
        result["current_price"] is not None
        and result["previous_close"] is not None
        and result["history_rows"]
    )
    return result


def _debug_dump(symbol: str) -> dict:
    """Capture raw upstream responses for the configured provider.

    Used by `python -m app.services.stock_data <SYM> --debug` to make the
    exact failure visible without adding a `print()` to the codebase.
    """
    s = get_settings()
    out: dict = {}
    if s.stock_data_provider.lower() == "finnhub":
        try:
            data = _finnhub_get("/quote", {"symbol": symbol}) or {}
            out["finnhub_quote_keys"] = sorted(data.keys())
            out["finnhub_quote"] = data
        except Exception as e:  # noqa: BLE001
            out["finnhub_quote_error"] = str(e)

    # Twelve Data — daily history source for the finnhub provider.
    out["twelvedata_key_present"] = bool(_twelvedata_key())
    status, body, http_err = _fetch_twelvedata_raw(symbol)
    out["twelvedata_status"] = status
    out["twelvedata_http_error"] = http_err
    if isinstance(body, dict):
        out["twelvedata_response_keys"] = sorted(body.keys())
        # Echo top-level fields that matter for diagnosis without dumping
        # the full year of OHLC.
        out["twelvedata_status_field"] = body.get("status")
        out["twelvedata_message"] = body.get("message")
        values = body.get("values")
        if isinstance(values, list):
            out["twelvedata_values_len"] = len(values)
            out["twelvedata_values_first"] = values[0] if values else None
            out["twelvedata_values_last"] = values[-1] if values else None
        cutoff = datetime.now(timezone.utc).date() - timedelta(days=_TWELVEDATA_HISTORY_DAYS)
        rows, parse_err = _parse_twelvedata_response(body, symbol, cutoff)
        out["twelvedata_parsed_rows"] = len(rows)
        out["twelvedata_parse_error"] = parse_err
        if rows:
            out["twelvedata_parsed_first"] = str(rows[0])
            out["twelvedata_parsed_last"] = str(rows[-1])
    elif isinstance(body, str):
        out["twelvedata_body_preview"] = body[:400]
    return out


if __name__ == "__main__":  # pragma: no cover
    import json
    import sys

    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    sym = args[0] if args else "AAPL"
    print(json.dumps(
        validate_provider(sym, debug="--debug" in flags),
        indent=2, default=str,
    ))
