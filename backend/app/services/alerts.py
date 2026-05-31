"""Logging + alerting plumbing.

Two channels, deliberately separate:

* **Error channel** — auto-triggered by stdlib `logging` records at ERROR or
  higher. Deduped (no more than one email per `(logger, message)` fingerprint
  per `DEDUP_WINDOW`). Subject prefix `[AISW ALERT]`.
* **Event channel** — explicit `notify_admin(event, ...)` calls for business
  events (signup, first-ever notification, etc.). Not deduped. Subject prefix
  `[AISW EVENT]`.

Both go through `app.services.emailer.send_email`. Recipient defaults to
`SMTP_USERNAME` and is overridable via `ALERT_RECEIVER`.

Two design rules that must NOT be relaxed:

1. Alert delivery must NEVER raise. If SMTP is the failure mode, alerting on
   it would recurse forever. Every public function here wraps the entire body
   in `try/except` and writes diagnostics to `stderr`, never back through the
   logging system.
2. DB log persistence must NEVER raise. If the DB is the failure mode, every
   ERROR log would trigger another failed DB insert. The handler degrades to
   stdout-only after consecutive failures and re-enables itself after a
   cool-down.
"""

from __future__ import annotations

import enum
import hashlib
import json
import logging
import re
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta
from typing import Any

from app.core.settings import get_settings

# -----------------------------------------------------------------------------
# Lifetime parser
# -----------------------------------------------------------------------------

_LIFETIME_RE = re.compile(r"^\s*(\d+)\s*([smhdwMy]?)\s*$")
_LIFETIME_UNITS = {
    "s": timedelta(seconds=1),
    "m": timedelta(minutes=1),
    "h": timedelta(hours=1),
    "d": timedelta(days=1),
    "w": timedelta(weeks=1),
    "M": timedelta(days=30),  # calendar-month approximation
    "y": timedelta(days=365),
}


def parse_lifetime(spec: str) -> timedelta:
    """Parse a human-readable lifetime ("30d", "24h", "1m", "90d", "1y").

    Bare integers are interpreted as days. The single letter `m` is
    intentionally lowercase = minutes; uppercase `M` = month. We pick this
    convention because most log/retention configs read in days, and "30d" /
    "1M" reads more naturally than overloading `m`. The settings comment
    documents both.
    """
    m = _LIFETIME_RE.match(spec)
    if not m:
        raise ValueError(f"Invalid lifetime spec: {spec!r}")
    n = int(m.group(1))
    unit = m.group(2) or "d"
    if unit not in _LIFETIME_UNITS:
        raise ValueError(f"Invalid lifetime unit: {unit!r}")
    return _LIFETIME_UNITS[unit] * n


# -----------------------------------------------------------------------------
# Alert channel
# -----------------------------------------------------------------------------


class AlertChannel(str, enum.Enum):
    ERROR = "error"
    EVENT = "event"
    SUMMARY = "summary"


_DEDUP_WINDOW = timedelta(minutes=15)
_dedup_lock = threading.Lock()
_dedup_state: dict[str, datetime] = {}


def _stderr(msg: str) -> None:
    """Last-resort diagnostic that never goes through logging."""
    print(f"[alerts] {msg}", file=sys.stderr, flush=True)


def _fingerprint(logger_name: str, message: str) -> str:
    payload = f"{logger_name}::{message[:200]}"
    return hashlib.sha1(payload.encode("utf-8", errors="replace")).hexdigest()


def _recently_alerted(fp: str, window: timedelta = _DEDUP_WINDOW) -> bool:
    now = datetime.utcnow()
    with _dedup_lock:
        last = _dedup_state.get(fp)
        if last is not None and now - last < window:
            return True
        _dedup_state[fp] = now
        # cheap GC: drop fingerprints older than 10x the window
        cutoff = now - window * 10
        for key in [k for k, v in _dedup_state.items() if v < cutoff]:
            _dedup_state.pop(key, None)
        return False


def _send_alert_email(
    subject: str, body_text: str, body_html: str | None = None
) -> None:
    """Send an alert email, silently swallowing any failure.

    Imports `send_email` lazily so tests that monkeypatch the emailer module
    still hit the patched function.
    """
    try:
        settings = get_settings()
        if not settings.alerts_enabled:
            return
        to = settings.effective_alert_receiver
        if not to:
            _stderr("alert suppressed: no recipient configured")
            return
        from app.services import emailer  # late-bound for monkeypatching

        emailer.send_email(to, subject, body_text, body_html=body_html)
    except Exception as e:  # noqa: BLE001
        _stderr(f"alert send failed: {e}")


def notify_admin(
    event: str,
    subject: str,
    body_text: str,
    body_html: str | None = None,
    channel: AlertChannel = AlertChannel.EVENT,
) -> None:
    """Explicit admin alert for business events / daily summaries.

    Never raises. Not deduped (the caller chose to send it).
    """
    prefix = {
        AlertChannel.ERROR: "[AISW ALERT]",
        AlertChannel.EVENT: "[AISW EVENT]",
        AlertChannel.SUMMARY: "[AISW DAILY]",
    }[channel]
    full_subject = f"{prefix} {subject}"
    full_body = f"Event: {event}\n\n{body_text}"
    _send_alert_email(full_subject, full_body, body_html=body_html)


# -----------------------------------------------------------------------------
# Logging handlers
# -----------------------------------------------------------------------------


class EmailAlertHandler(logging.Handler):
    """Emit an alert email on ERROR+ records, deduped."""

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.levelno < logging.ERROR:
                return
            settings = get_settings()
            if not settings.alerts_enabled:
                return
            msg = record.getMessage()
            fp = _fingerprint(record.name, msg)
            if _recently_alerted(fp):
                return
            subject = f"[AISW ALERT] {record.levelname} {record.name}: {msg[:80]}"
            body = self._format_body(record, msg)
            _send_alert_email(subject, body)
        except Exception as e:  # noqa: BLE001
            _stderr(f"EmailAlertHandler.emit failed: {e}")

    @staticmethod
    def _format_body(record: logging.LogRecord, msg: str) -> str:
        parts = [
            f"Time:    {datetime.utcfromtimestamp(record.created).isoformat()}Z",
            f"Level:   {record.levelname}",
            f"Logger:  {record.name}",
            f"Module:  {record.module}:{record.lineno}",
            "",
            "Message:",
            msg,
        ]
        if record.exc_info:
            parts += ["", "Traceback:", "".join(traceback.format_exception(*record.exc_info))]
        return "\n".join(parts)


class DBLogHandler(logging.Handler):
    """Persist WARNING+ log records to the `log_entries` table.

    Includes a circuit-breaker: after `_MAX_CONSEC_FAILURES` consecutive DB
    write failures, the handler disables itself for `_COOLDOWN_SECS` so a DB
    outage doesn't pile errors into the failing logger.
    """

    _MAX_CONSEC_FAILURES = 5
    _COOLDOWN_SECS = 60

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self._failures = 0
        self._disabled_until = 0.0
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno < logging.WARNING:
            return
        # Suppress logs originating from the SQLAlchemy engine itself; logging
        # them re-enters the DB and can recurse during shutdown.
        if record.name.startswith("sqlalchemy"):
            return
        with self._lock:
            if time.monotonic() < self._disabled_until:
                return
        try:
            settings = get_settings()
            if not settings.log_db_persistence:
                return
            # Lazy import; ensures monkeypatched session factory in tests works.
            from app.db.database import get_session_factory
            from app.models import LogEntry

            exc_text: str | None = None
            if record.exc_info:
                exc_text = "".join(traceback.format_exception(*record.exc_info))[:8000]
            context_text: str | None = None
            ctx = getattr(record, "context", None)
            if ctx is not None:
                try:
                    context_text = json.dumps(ctx, default=str)[:4000]
                except Exception:  # noqa: BLE001
                    context_text = None

            session = get_session_factory()()
            try:
                session.add(
                    LogEntry(
                        level=record.levelname,
                        logger=record.name[:160],
                        message=record.getMessage()[:10_000],
                        exc_info=exc_text,
                        context=context_text,
                    )
                )
                session.commit()
            finally:
                session.close()

            with self._lock:
                self._failures = 0
        except Exception as e:  # noqa: BLE001
            with self._lock:
                self._failures += 1
                if self._failures >= self._MAX_CONSEC_FAILURES:
                    self._disabled_until = time.monotonic() + self._COOLDOWN_SECS
                    self._failures = 0
                    _stderr(
                        f"DBLogHandler disabled for {self._COOLDOWN_SECS}s after repeated failures"
                    )
            _stderr(f"DBLogHandler.emit failed: {e}")


# -----------------------------------------------------------------------------
# Installer
# -----------------------------------------------------------------------------


_installed = False
_installed_lock = threading.Lock()


def install_handlers() -> None:
    """Attach DB + email handlers to the root logger. Idempotent."""
    global _installed
    with _installed_lock:
        if _installed:
            return
        root = logging.getLogger()
        root.addHandler(DBLogHandler())
        root.addHandler(EmailAlertHandler())
        _installed = True


def reset_for_tests() -> None:
    """Detach handlers + clear dedup state. Called from test teardown."""
    global _installed
    with _installed_lock:
        root = logging.getLogger()
        for h in list(root.handlers):
            if isinstance(h, (DBLogHandler, EmailAlertHandler)):
                root.removeHandler(h)
        _installed = False
    with _dedup_lock:
        _dedup_state.clear()


# Convenience helpers used by jobs/services -----------------------------------


def alert_event_signup(email: str) -> None:
    notify_admin(
        "user_signup",
        f"New user signed up: {email}",
        f"A new user just completed sign-in for the first time.\n\nEmail: {email}\n",
        channel=AlertChannel.EVENT,
    )


def alert_event_first_notification(
    user_email: str, ticker_symbol: str, event_type: str, summary: str
) -> None:
    notify_admin(
        "first_notification",
        f"First-ever notification fired for {user_email}",
        (
            f"A user just received their first notification.\n\n"
            f"User:       {user_email}\n"
            f"Ticker:     {ticker_symbol}\n"
            f"Event:      {event_type}\n"
            f"Summary:    {summary}\n"
        ),
        channel=AlertChannel.EVENT,
    )


def alert_event_upstream_api_down(attempted: int, succeeded: int, sample_errors: list[str]) -> None:
    body = (
        f"yfinance returned no usable data for any of {attempted} watched tickers.\n"
        f"Successful fetches: {succeeded}\n\n"
        "This usually means Yahoo Finance is rate-limiting, has changed its API, or "
        "the network from Railway is blocked. Investigate before the next batch run.\n"
    )
    if sample_errors:
        body += "\nRecent per-ticker failures (sample):\n" + "\n".join(
            f"  - {e}" for e in sample_errors[:5]
        )
    notify_admin(
        "upstream_api_down",
        "Upstream price API returned no data",
        body,
        channel=AlertChannel.ERROR,
    )


def _diagnostics() -> dict[str, Any]:
    """Cheap snapshot used by tests + future admin endpoint."""
    return {
        "dedup_entries": len(_dedup_state),
        "installed": _installed,
    }
