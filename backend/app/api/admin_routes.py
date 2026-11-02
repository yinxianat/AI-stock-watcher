"""Read-only production diagnostics for the dashboard and the review agent.

Auth is a single bearer token (`ADMIN_API_TOKEN`), deliberately not a user
session: the scheduled review agent has no user to log in as, and these
endpoints expose cross-user data that no end user should ever see.

If `ADMIN_API_TOKEN` is unset every endpoint here returns 503. A missing
token must never mean "open to everyone".

Everything served here comes from `app.services.diagnostics`, so the
dashboard, the nightly digest email and the agent can't drift apart.
"""

from __future__ import annotations

import logging
import secrets
from datetime import date as date_
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Header, Query, status

from app.core.settings import get_settings
from app.services import diagnostics

log = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])

_ET = ZoneInfo("America/New_York")
# A day report scans several indexed tables; a month is a reasonable ceiling
# for one request and keeps an agent from accidentally asking for a year.
_MAX_DAYS = 31

# Paths the monitoring agent may reach. An ALLOW-list, not a deny-list: a
# new endpoint added later is invisible to the agent until it is named
# here, so forgetting to exclude something can't leak it. Every path on
# this list must be free of personal data at SCOPE_SERVICE — enforced by
# test_no_service_path_leaks_personal_data.
SERVICE_SAFE_PATHS: frozenset[str] = frozenset({
    "/admin/watch",            # pseudonymised per-user delivery
    "/admin/findings",         # ranked problems, no user rows
    "/admin/storage",          # table growth, retention, log-noise ratio
    "/admin/ml-corpus",        # training-data health, counts only
    "/admin/reason-catalogue",  # static help text
})

# Deliberately NOT service-safe:
#   /admin/diagnostics   — the unfiltered firehose; a new PII field would
#                          surface here first.
#   /admin/health-summary — carries the admin's own alert address.


def require_admin(authorization: str | None = Header(default=None)) -> None:
    """Constant-time bearer check against ADMIN_API_TOKEN."""
    configured = get_settings().admin_api_token.strip()
    if not configured:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Admin endpoints are disabled (ADMIN_API_TOKEN is not set)",
        )
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Bearer token required")
    if not secrets.compare_digest(token.strip(), configured):
        # Logged so repeated probing is visible in log_entries.
        log.warning("Admin endpoint rejected a bad token")
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid admin token")


def _eastern_today() -> date_:
    return datetime.now(tz=_ET).date()


def _parse_day(value: str | None) -> date_:
    if not value:
        return _eastern_today()
    try:
        return date_.fromisoformat(value)
    except ValueError:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "date must be YYYY-MM-DD"
        ) from None


@router.get("/diagnostics", dependencies=[Depends(require_admin)])
def get_diagnostics(date: str | None = Query(default=None)) -> dict:
    """Full report for one Eastern trading day (default: today).

    `findings` holds the deterministic problems found, worst first — the
    dashboard surfaces it at the top and the agent starts from it.
    """
    return diagnostics.day_report(_parse_day(date))


@router.get("/findings", dependencies=[Depends(require_admin)])
def get_findings(days: int = Query(default=1, ge=1, le=_MAX_DAYS)) -> dict:
    """Just the problems, across the last `days` Eastern days.

    This is what the scheduled agent polls: small payload, already ranked,
    so the agent spends its reasoning on trends and judgement calls rather
    than on re-deriving facts a rule can already check.
    """
    today = _eastern_today()
    out = []
    for i in range(days):
        day = today - timedelta(days=i)
        report = diagnostics.day_report(day)
        for f in report["findings"]:
            out.append({"date": day.isoformat(), **f})
    order = {"high": 0, "medium": 1, "low": 2}
    out.sort(key=lambda f: (order.get(f["severity"], 9), f["date"]))
    return {
        "from": (today - timedelta(days=days - 1)).isoformat(),
        "to": today.isoformat(),
        "count": len(out),
        "high": sum(1 for f in out if f["severity"] == "high"),
        "findings": out,
    }


@router.get("/watch", dependencies=[Depends(require_admin)])
def get_watch() -> dict:
    """Today and yesterday in one call, split by urgency.

    This is the agent's main entry point. The split matters: a `high`
    finding from **today** means something is broken right now and wants
    attention within the hour, while the same finding from yesterday is
    history for the weekly write-up. Mixing them buries the live problem.

    `act_now` is pre-computed so the agent (and a human skimming) can't
    miss it: non-empty means go and look.
    """
    today = _eastern_today()
    yesterday = today - timedelta(days=1)
    now_et = datetime.now(tz=_ET)

    # Built at SCOPE_SERVICE: this endpoint is on SERVICE_SAFE_PATHS, so
    # users appear as pseudonyms ("u7") rather than addresses. The dashboard
    # uses /admin/diagnostics when it needs real identities.
    live = diagnostics.day_report(today, scope=diagnostics.SCOPE_SERVICE)
    prior = diagnostics.day_report(yesterday, scope=diagnostics.SCOPE_SERVICE)

    # A job that hasn't reached its slot yet isn't missing — it's pending.
    # Without this every morning check would scream about the 16:05 batch.
    pending, live_findings = [], []
    for f in live["findings"]:
        if f["code"] == "job_missing":
            slot = next(
                (s for s in live["jobs"]["slots"] if s["job_id"] in f["detail"]), None,
            )
            if slot and slot["expected_et"] > now_et.strftime("%H:%M"):
                pending.append(slot["job_id"])
                continue
        live_findings.append(f)

    act_now = [f for f in live_findings if f["severity"] == "high"]
    return {
        "checked_at_et": now_et.isoformat(),
        "today": {
            "date": today.isoformat(),
            "urgency": "act_now" if act_now else "ok",
            "act_now": act_now,
            "findings": live_findings,
            "not_due_yet": pending,
            "jobs": live["jobs"]["slots"],
            "stories": live["stories"]["by_status"],
            "errors": live["errors"]["errors"],
        },
        "yesterday": {
            "date": yesterday.isoformat(),
            "urgency": "review",
            "findings": prior["findings"],
            "jobs": prior["jobs"]["slots"],
            "stories": prior["stories"],
            "delivery": prior["delivery"],
            "errors": prior["errors"],
        },
        "storage": live["storage"],
        "config": live["config"],
    }


@router.get("/storage", dependencies=[Depends(require_admin)])
def get_storage() -> dict:
    """Table growth, retention enforcement and log-noise ratio.

    Separate from the day report because it's about trend and cost rather
    than about one day, and it's the slowest part to compute.
    """
    return diagnostics.storage_report()


@router.get("/ml-corpus", dependencies=[Depends(require_admin)])
def get_ml_corpus() -> dict:
    """Training-data health for the planned story model: how many labelled
    stories exist, how far back they stay usable, and how much human
    feedback has accumulated."""
    return diagnostics.ml_corpus_report()


@router.get("/health-summary", dependencies=[Depends(require_admin)])
def get_health_summary() -> dict:
    """The same rolling-24h digest the nightly email sends, as JSON."""
    from app.jobs.daily_summary import collect_summary, health

    data = collect_summary()
    return {"health": health(data), "summary": data}


@router.get("/reason-catalogue", dependencies=[Depends(require_admin)])
def get_reason_catalogue() -> dict:
    """What each delivery reason means and what to do about it.

    Served rather than duplicated in the frontend and the agent prompt, so
    there is one wording for every outcome.
    """
    return {"help": diagnostics.REASON_HELP, "remediation": diagnostics.REMEDIATION}
