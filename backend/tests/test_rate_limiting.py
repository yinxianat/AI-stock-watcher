"""Per-IP rate limits, and proof they actually fire.

This existed before as a `Limiter` with `default_limits` that was never
applied, because `SlowAPIMiddleware` was not installed — 80 requests in a
row returned 200. These tests assert the 429s rather than the config, so a
regression of that shape fails here instead of silently reopening the
endpoints.

Limiting is off by default in the suite (conftest), so each test here turns
it on and resets the limiter's state first.
"""

from __future__ import annotations

import pytest

from app.core.ratelimit import limiter, reset_route_limits
from app.core.settings import get_settings


@pytest.fixture
def limited(monkeypatch):
    """Rate limiting on, with a clean counter."""
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    get_settings.cache_clear()
    limiter.enabled = True
    limiter.reset()
    reset_route_limits()
    yield
    limiter.reset()
    reset_route_limits()
    limiter.enabled = False
    get_settings.cache_clear()


def _codes(responses) -> set[int]:
    return {r.status_code for r in responses}


# ---------------------------------------------------------------------------
# The global ceiling
# ---------------------------------------------------------------------------


def test_default_limit_is_actually_applied(client, limited):
    """The regression: default_limits do nothing without the middleware."""
    seen = [client.get("/tickers") for _ in range(70)]
    assert 429 in _codes(seen), "no 429 in 70 requests — middleware missing again"


def test_healthz_is_exempt(client, limited):
    """Railway polls this; a 429 here reads as the service being down."""
    seen = [client.get("/healthz") for _ in range(80)]
    assert _codes(seen) == {200}


# ---------------------------------------------------------------------------
# Sign-in links — the SMTP-quota and outbound-spam vector
# ---------------------------------------------------------------------------


def test_signin_is_capped_per_ip_across_different_addresses(client, limited):
    """The per-address throttle in send_limits is bypassed by rotating the
    target address. This per-IP cap is what actually protects the quota."""
    monkey = [
        client.post("/auth/request-link", json={"email": f"victim{i}@example.com"})
        for i in range(8)
    ]
    codes = [r.status_code for r in monkey]
    assert 429 in codes, "rotating addresses walked past the limit"
    # The cap is 5/hour, so the first few succeed and the rest are refused.
    assert codes.count(429) >= 2


def test_signin_limit_is_configurable(client, limited, monkeypatch):
    """Loosening it must not require a deploy — a household behind one NAT
    shares this budget."""
    monkeypatch.setenv("RATE_LIMIT_SIGNIN", "1/hour")
    get_settings.cache_clear()
    reset_route_limits()
    first = client.post("/auth/request-link", json={"email": "a@example.com"})
    second = client.post("/auth/request-link", json={"email": "b@example.com"})
    assert first.status_code == 204
    assert second.status_code == 429


def test_rate_limited_response_tells_the_caller_how_long_to_wait(client, limited):
    for i in range(8):
        r = client.post("/auth/request-link", json={"email": f"x{i}@example.com"})
        if r.status_code == 429:
            # The SPA reads this to show a countdown; CORS already exposes it.
            assert "retry-after" in {k.lower() for k in r.headers}
            return
    pytest.fail("never hit the limit")


# ---------------------------------------------------------------------------
# Client error ingest — unauthenticated write
# ---------------------------------------------------------------------------


def test_client_logs_is_capped(client, limited):
    """Unauthenticated, so without a cap a loop could grow log_entries and
    (by varying the message to defeat dedup) flood the admin's inbox."""
    seen = [
        client.post("/client-logs", json={"message": f"boom {i}", "source": "x"})
        for i in range(15)
    ]
    assert 429 in _codes(seen)


# ---------------------------------------------------------------------------
# Off switch
# ---------------------------------------------------------------------------


def test_limiting_can_be_switched_off_without_a_deploy(client, monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "false")
    get_settings.cache_clear()
    limiter.enabled = False
    limiter.reset()
    seen = [client.get("/tickers") for _ in range(70)]
    assert _codes(seen) == {200}


# ---------------------------------------------------------------------------
# Interactive docs
# ---------------------------------------------------------------------------


def test_docs_are_public_outside_production(monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    get_settings.cache_clear()
    from app.main import create_app

    app = create_app()
    assert app.docs_url == "/docs"
    assert app.openapi_url == "/openapi.json"


def test_docs_are_hidden_in_production(monkeypatch):
    """A complete API map is free reconnaissance."""
    monkeypatch.setenv("APP_ENV", "production")
    get_settings.cache_clear()
    from app.main import create_app

    app = create_app()
    assert app.docs_url is None
    assert app.redoc_url is None
    assert app.openapi_url is None


def test_production_docs_routes_are_absent(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    get_settings.cache_clear()
    from app.main import create_app

    paths = {getattr(r, "path", "") for r in create_app().routes}
    assert "/docs" not in paths and "/openapi.json" not in paths
    assert "/healthz" in paths  # the app still works
