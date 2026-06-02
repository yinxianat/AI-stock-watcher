"""Daily retention cleanup.

Two retention policies, each backed by its own setting:

* `LOG_LIFETIME` → prunes `log_entries` (default 30 days).
* `PRICE_HISTORY_LIFETIME` → prunes `daily_closes` (default 365 days).

Both run inside one job so APScheduler only schedules a single off-peak
cron tick.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import delete

from app.core.settings import get_settings
from app.db.database import get_session_factory
from app.models import LogEntry
from app.services.alerts import parse_lifetime
from app.services.price_history import prune_daily_closes

log = logging.getLogger(__name__)


def _prune_log_entries(session, lifetime_spec: str) -> int:
    try:
        lifetime = parse_lifetime(lifetime_spec)
    except ValueError:
        log.error("Invalid LOG_LIFETIME=%r; skipping log_entries cleanup", lifetime_spec)
        return 0

    cutoff = datetime.utcnow() - lifetime
    result = session.execute(delete(LogEntry).where(LogEntry.created_at < cutoff))
    session.commit()
    deleted = result.rowcount or 0
    log.info(
        "Log cleanup: deleted %d log_entries rows older than %s (cutoff=%s)",
        deleted, lifetime_spec, cutoff.isoformat(),
    )
    return deleted


def _prune_price_history(session, lifetime_spec: str) -> int:
    try:
        lifetime = parse_lifetime(lifetime_spec)
    except ValueError:
        log.error(
            "Invalid PRICE_HISTORY_LIFETIME=%r; skipping daily_closes cleanup",
            lifetime_spec,
        )
        return 0
    return prune_daily_closes(session, lifetime)


def run_cleanup() -> int:
    """Run all retention pruning. Returns total rows deleted across tables."""
    settings = get_settings()
    session = get_session_factory()()
    try:
        deleted = 0
        deleted += _prune_log_entries(session, settings.log_lifetime)
        deleted += _prune_price_history(session, settings.price_history_lifetime)
        return deleted
    finally:
        session.close()
