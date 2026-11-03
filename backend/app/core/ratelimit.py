"""The shared rate limiter.

Lives in its own module because both `app.main` and the route modules need
it: putting it in `main` would make `main → auth_routes → main` circular.

Two layers protect the email endpoints, and they catch different abuse:

* the per-address throttle in `app.services.send_limits` stops one person
  being mailed repeatedly, but an attacker rotating target addresses walks
  straight past it;
* the per-IP limit here caps that rotation, which is what protects the SMTP
  quota and stops the app being used to mail strangers.

`enabled` is set from settings in `create_app`, so tests (which fire far
more than 60 requests a minute from one address) can switch it off, and so
production has an off switch that needs no code change.
"""

from __future__ import annotations

import logging
import math
import time
from typing import Callable

from fastapi import HTTPException, Request, status
from limits import parse
from limits.storage import MemoryStorage
from limits.strategies import MovingWindowRateLimiter
from slowapi import Limiter
from slowapi.util import get_remote_address

log = logging.getLogger(__name__)

# The global ceiling, applied to every route by `SlowAPIMiddleware`.
# Generous: normal SPA use makes several calls per page.
DEFAULT_LIMITS = ["60/minute"]

limiter = Limiter(key_func=get_remote_address, default_limits=DEFAULT_LIMITS)


# ---------------------------------------------------------------------------
# Per-route limits
# ---------------------------------------------------------------------------
# These are a FastAPI dependency rather than slowapi's `@limiter.limit`
# decorator, and that is deliberate: the decorator cannot be used on any
# route in a module with `from __future__ import annotations` (which is all
# of ours). The wrapper slowapi returns carries slowapi's `__globals__`, so
# when FastAPI evaluates the route's string annotations it cannot resolve
# our own types — `payload: LoginRequest` silently degrades to a query
# parameter and every request 422s. A dependency sees the real `Request`
# and leaves the route signature untouched.
#
# Storage is per-process, which matches the scheduler and the dedup caches:
# one Railway container. If this ever runs multi-replica, swap MemoryStorage
# for the Redis backend or each replica will allow the full budget.

_storage = MemoryStorage()
_strategy = MovingWindowRateLimiter(_storage)


def reset_route_limits() -> None:
    """Forget all per-route counters. For tests."""
    _storage.reset()


def RateLimit(  # noqa: N802 — reads as a type at the call site
    spec: str | Callable[[], str], name: str
) -> Callable[[Request], None]:
    """Build a dependency enforcing `spec` (e.g. "5/hour") per client IP.

    A factory returning a plain function, not a callable class: FastAPI
    resolves a dependency's annotations using `__globals__` on the callable
    itself, and a class *instance* has none — so with
    `from __future__ import annotations` the `Request` annotation would be
    an unresolvable string and `request` would degrade to a query
    parameter. A module-level function carries this module's globals.

    `spec` may be a callable, so a limit can be read from settings at
    request time and changed without a deploy.
    """

    def dependency(request: Request) -> None:
        from app.core.settings import get_settings

        if not get_settings().rate_limit_enabled:
            return
        item = parse(spec() if callable(spec) else spec)
        client = get_remote_address(request)
        key = f"{name}:{client}"
        if _strategy.hit(item, key):
            return
        retry_after = max(
            1, math.ceil(_strategy.get_window_stats(item, key).reset_time - time.time())
        )
        # Visible in log_entries so repeated hits on the sign-in limit — the
        # shape of an address-enumeration attempt — are reviewable.
        log.warning("Rate limit %s hit by %s (retry in %ss)", name, client, retry_after)
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many requests. Try again in {retry_after} seconds.",
            headers={"Retry-After": str(retry_after)},
        )

    return dependency
