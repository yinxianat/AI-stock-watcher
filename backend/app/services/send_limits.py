"""Throttle for user-triggered emails (sign-in links, address confirmations).

Stops repeated clicks — or a script — from flooding an inbox. Per
recipient/user: a short wait between emails, plus a cap on how many can be
sent in a burst window (see EMAIL_RESEND_COOLDOWN_SECONDS,
EMAIL_BURST_LIMIT, EMAIL_BURST_WINDOW_MINUTES). Callers pass the send times
they already store (e.g. `login_tokens.created_at`), so no extra table is
needed.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

from app.core.settings import get_settings
from app.models import utcnow


class TooManyEmails(Exception):
    """Raised when another email is requested too soon."""

    def __init__(self, retry_after_seconds: int):
        super().__init__(f"Retry after {retry_after_seconds}s")
        self.retry_after_seconds = retry_after_seconds


def _window() -> timedelta:
    return timedelta(minutes=get_settings().email_burst_window_minutes)


def window_start() -> datetime:
    """Only sends after this time count toward the limit."""
    return utcnow() - _window()


def enforce_send_limit(sent_at_newest_first: list[datetime]) -> None:
    """Raise TooManyEmails if sending one more email now would exceed the limit.

    `sent_at_newest_first` are send times since `window_start()`, newest first.
    """
    if not sent_at_newest_first:
        return
    settings = get_settings()
    cooldown = timedelta(seconds=settings.email_resend_cooldown_seconds)
    limit = settings.email_burst_limit
    now = utcnow()
    wait_until = None
    if now - sent_at_newest_first[0] < cooldown:
        wait_until = sent_at_newest_first[0] + cooldown
    elif len(sent_at_newest_first) >= limit:
        wait_until = sent_at_newest_first[limit - 1] + _window()
    if wait_until is not None:
        raise TooManyEmails(max(1, math.ceil((wait_until - now).total_seconds())))
