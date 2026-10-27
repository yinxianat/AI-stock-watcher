"""Throttle for user-triggered emails (sign-in links, address confirmations).

Stops repeated clicks — or a script — from flooding an inbox: at most one
email per minute and five per hour for the same recipient/user. Callers
pass the send times they already store (e.g. `login_tokens.created_at`),
so no extra table is needed.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

from app.models import utcnow

RESEND_COOLDOWN = timedelta(seconds=60)
HOURLY_LIMIT = 5
WINDOW = timedelta(hours=1)


class TooManyEmails(Exception):
    """Raised when another email is requested too soon."""

    def __init__(self, retry_after_seconds: int):
        super().__init__(f"Retry after {retry_after_seconds}s")
        self.retry_after_seconds = retry_after_seconds


def window_start() -> datetime:
    """Only sends after this time count toward the limit."""
    return utcnow() - WINDOW


def enforce_send_limit(sent_at_newest_first: list[datetime]) -> None:
    """Raise TooManyEmails if sending one more email now would exceed the limit.

    `sent_at_newest_first` are send times within the last hour, newest first.
    """
    if not sent_at_newest_first:
        return
    now = utcnow()
    wait_until = None
    if now - sent_at_newest_first[0] < RESEND_COOLDOWN:
        wait_until = sent_at_newest_first[0] + RESEND_COOLDOWN
    elif len(sent_at_newest_first) >= HOURLY_LIMIT:
        wait_until = sent_at_newest_first[HOURLY_LIMIT - 1] + WINDOW
    if wait_until is not None:
        raise TooManyEmails(max(1, math.ceil((wait_until - now).total_seconds())))
