"""Daily log retention cleanup.

Deletes `LogEntry` rows older than `LOG_LIFETIME`. Runs once daily off-peak.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import delete

from app.core.settings import get_settings
from app.db.database import get_session_factory
from app.models import LogEntry
from app.services.alerts import parse_lifetime

log = logging.getLogger(__name__)


def run_cleanup() -> int:
    """Delete LogEntry rows older than LOG_LIFETIME. Returns rows deleted."""
    settings = get_settings()
    try:
        lifetime = parse_lifetime(settings.log_lifetime)
    except ValueError:
        log.error("Invalid LOG_LIFETIME=%r; skipping cleanup", settings.log_lifetime)
        return 0

    cutoff = datetime.utcnow() - lifetime
    session = get_session_factory()()
    try:
        result = session.execute(delete(LogEntry).where(LogEntry.created_at < cutoff))
        session.commit()
        deleted = result.rowcount or 0
        log.info("Log cleanup: deleted %d rows older than %s (cutoff=%s)",
                 deleted, settings.log_lifetime, cutoff.isoformat())
        return deleted
    finally:
        session.close()
