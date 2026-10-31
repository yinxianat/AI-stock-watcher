"""Ingest errors that happened in the browser.

The frontend posts here when a React render crashes, an API call blows up,
or a background action silently fails. We re-emit each entry through the
standard logger so the normal pipeline picks it up: `DBLogHandler` writes
it to `log_entries` and `EmailAlertHandler` emails the admin (deduped).

Rate-limited and strictly size-capped so a broken client in a tight loop
can't flood the DB or the admin's inbox.
"""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

router = APIRouter(prefix="/client-logs", tags=["client-logs"])

# Dedicated logger so a reader can tell these apart from server-side errors
# in the DB and in Railway logs.
log = logging.getLogger("app.client")

# Hard caps. These are also enforced by Pydantic below; keeping the
# constants here as the single source of truth for both the body limit and
# the server-side truncation that comes after.
_MAX_MSG = 500
_MAX_STACK = 4000
_MAX_URL = 500


class ClientLogEntry(BaseModel):
    """One error from the browser.

    `level` controls which handlers pick it up. Only "error" triggers the
    admin alert path, so routine warnings (API 400s from user mistakes,
    expected 401s) don't page us.
    """

    level: Literal["warning", "error"] = "error"
    message: str = Field(min_length=1, max_length=_MAX_MSG)
    stack: str | None = Field(default=None, max_length=_MAX_STACK)
    url: str | None = Field(default=None, max_length=_MAX_URL)
    source: str | None = Field(default=None, max_length=60)  # "feedback", "money-ideas", ...


@router.post("", status_code=204, response_model=None)
def ingest_client_log(entry: ClientLogEntry, request: Request) -> None:
    """Record a client-side error against the server log pipeline.

    No auth: errors can happen on signed-out pages (login, feedback link).
    The default 60/min per-IP limit from `app.main.limiter` applies.
    """
    source = entry.source or "unknown"
    ip = request.client.host if request.client else "unknown"

    # %s placeholders (not f-strings) so EmailAlertHandler's dedup groups
    # repeat occurrences by shape — same bug, one alert per 15 minutes.
    extra = {"client_url": entry.url or "", "client_ip": ip}
    message = f"[client:{source}] {entry.message}"
    if entry.stack:
        message = f"{message} | stack: {entry.stack[:_MAX_STACK]}"

    if entry.level == "warning":
        log.warning("%s", message, extra=extra)
    else:
        log.error("%s", message, extra=extra)
