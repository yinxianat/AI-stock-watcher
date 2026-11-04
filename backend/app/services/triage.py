"""Admin triage for monitoring findings: identity, state and recurrence.

A finding has no row. `diagnostics.findings()` derives the day's problems
from the database every time it is asked, which is the right design — it
can never go stale — but it means there is nothing for an admin to act on.
Every morning the same list arrives in the same order with no record that
yesterday it was read, judged and parked.

This module adds the three things that turn a list into a worklist:

* **Identity** — a `fingerprint` for each finding, stable across
  recomputation, so a decision made today still matches tomorrow.
* **State** — the admin's instruction: acknowledged until a date, being
  worked on, on the backlog, or resolved.
* **Recurrence** — how many distinct days a finding has been seen, because
  day 1 and day 14 call for different reactions and otherwise render
  identically.

The single rule everything here protects: **no state can silence a finding
permanently.** An acknowledgement carries an expiry, and a resolved finding
that happens again comes back marked as having come back. A monitoring page
whose quiet cannot be distinguished from a forgotten mute is worse than no
page, because it is trusted and wrong.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date as date_
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app.models import FindingState, utcnow

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

# A fingerprint reaches us as a URL path segment, so it is constrained at
# both ends: built narrow here, and re-validated on the way in.
FINGERPRINT_RE = re.compile(r"^[a-z0-9_.:-]{1,160}$")


def fingerprint(code: str, subject: str | None = None) -> str:
    """Stable identity for a finding.

    `code` alone for a finding about the whole system; `code:subject` when
    it names one job or one table. Both halves matter. `code` alone would
    be too coarse — acknowledging one missing job would silence every
    missing job, which is exactly the outage you would then not see. Adding
    the day would be too fine: a problem into its third week would need
    acknowledging each morning, and nothing would ever accumulate a
    history.

    Deliberately readable rather than hashed. `job_missing:pipeline_0935`
    in a URL or a log line is self-explaining; a digest would need a lookup
    to mean anything, and this is a debugging surface.
    """
    base = code if not subject else f"{code}:{subject}"
    cleaned = re.sub(r"[^a-z0-9_.:-]+", "-", base.strip().lower())[:160]
    return cleaned or "unknown"


# ---------------------------------------------------------------------------
# The states
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class State:
    label: str
    what: str
    # Whether a finding in this state still belongs in the urgent lane.
    # Only "working" does: something being fixed is still broken.
    stays_visible: bool
    needs_snooze: bool = False


# The catalogue. `/admin/triage-states` serves it and the dashboard renders
# it, so the wording lives in one place — the same arrangement as
# `delivery.REASONS`. `test_triage.py` fails the build on a state the UI
# can set but this does not describe.
#
# These are the states an admin may SET. `STATE_NEW` below is a storage
# value only — a finding observed but never triaged — and the route
# refuses it, so "new" can never be used to overwrite a real decision.
STATES: dict[str, State] = {
    "ack": State(
        label="Acknowledged",
        what="Read and judged not urgent. Leaves the urgent lane until the snooze "
             "date, then comes back on its own.",
        stays_visible=False,
        needs_snooze=True,
    ),
    "working": State(
        label="Working on it",
        what="Being fixed now. Stays on screen, with its age showing — something "
             "that has been 'working' for three weeks is itself the finding.",
        stays_visible=True,
    ),
    "backlog": State(
        label="On the to-do list",
        what="Real, not now. Out of the urgent lane and into the backlog, where it "
             "keeps its age so it cannot quietly rot.",
        stays_visible=False,
    ),
    "resolved": State(
        label="Resolved",
        what="Fixed. Disappears — but if it happens again after today it comes back "
             "marked 'this came back', so silence always means fixed and never "
             "means muted.",
        stays_visible=False,
    ),
}

# How long an acknowledgement lasts when the caller doesn't say. A week is
# long enough to stop the daily re-reading that prompted this and short
# enough that a forgotten snooze surfaces while the context is still fresh.
DEFAULT_SNOOZE_DAYS = 7
MAX_SNOOZE_DAYS = 90
MAX_NOTE_CHARS = 1000

# A finding that has been observed but never triaged. It needs a stored
# value because recurrence is tracked for every finding, triaged or not —
# "this has happened on 14 of the last 20 days" is most useful about the
# thing nobody has got to yet.
STATE_NEW = "new"


# ---------------------------------------------------------------------------
# Recurrence
# ---------------------------------------------------------------------------


def observe(db: DBSession, day: date_, findings: list[dict]) -> int:
    """Record that these findings were seen on `day`. Returns rows touched.

    Idempotent per day: opening the dashboard five times in one morning
    counts as one day, because `days_seen` advances only when the day
    differs from `last_seen_date`. Without that the recurrence count would
    measure how often the admin looked rather than how often the problem
    happened.

    Called from both the dashboard read and the daily digest, on purpose.
    The digest alone would leave holes on days nobody opened the page — and
    one of the failure modes this page reports is the digest job not
    running, so it cannot be the only writer. The dashboard alone would
    leave holes on days nobody looked.

    Never raises. This is bookkeeping attached to a monitoring read, and
    losing a day of history must not take the page down with it.
    """
    touched = 0
    try:
        wanted = {f["fingerprint"]: f for f in findings if f.get("fingerprint")}
        if not wanted:
            return 0
        rows = db.execute(
            select(FindingState).where(FindingState.fingerprint.in_(wanted))
        ).scalars().all()
        existing = {r.fingerprint: r for r in rows}

        for fp, f in wanted.items():
            row = existing.get(fp)
            if row is None:
                db.add(FindingState(
                    fingerprint=fp,
                    code=f["code"],
                    subject=f.get("subject"),
                    # A finding seen for the first time has been given no
                    # instruction, so it is new. "backlog" is the parking
                    # state that means nothing is owed right now, and it
                    # is what an untouched row reads as.
                    state=STATE_NEW,
                    first_seen_date=day,
                    last_seen_date=day,
                    days_seen=1,
                    last_severity=f.get("severity"),
                    last_detail=f.get("detail"),
                ))
                touched += 1
                continue
            if row.last_seen_date != day:
                # A distinct day is a distinct day, so the count advances
                # either way — but `last_seen_date` only ever moves
                # forward. Letting a backfilled older day pull it back
                # would un-expire a snooze and rewind the age.
                row.days_seen += 1
                touched += 1
                if day > row.last_seen_date:
                    row.last_seen_date = day
                    row.last_severity = f.get("severity")
                    row.last_detail = f.get("detail")
                if day < row.first_seen_date:
                    row.first_seen_date = day
            else:
                row.last_severity = f.get("severity")
                row.last_detail = f.get("detail")
        db.commit()
    except Exception:  # noqa: BLE001 - history is not worth a 500
        log.warning("Could not record finding observations for %s", day, exc_info=True)
        db.rollback()
        return 0
    return touched


# ---------------------------------------------------------------------------
# Effective state — computed, never stored
# ---------------------------------------------------------------------------


def effective(row: FindingState, today: date_) -> dict:
    """What a stored instruction means *today*.

    Computed on read rather than written back by a sweep, for one reason:
    a sweep that fails leaves findings silenced, and the sweep's own
    failure is a finding. Deriving it means the expiry cannot be the thing
    that breaks. There is no job to run, nothing to catch up on after a
    restart, and no state in the database that is quietly out of date.

    Two instructions expire on their own:

    * an **acknowledgement** past its `snooze_until` reads as new again;
    * a **resolved** finding seen on a later day reads as new again, and
      carries `regressed` so the dashboard can say it came back — the
      difference between "I fixed this" and "I fixed this and it broke
      again" is the whole value of having marked it resolved.
    """
    state = row.state
    regressed = False
    expired = False

    if state == "resolved" and row.resolved_on and row.last_seen_date > row.resolved_on:
        state, regressed = STATE_NEW, True
    elif state == "ack" and (row.snooze_until is None or row.snooze_until <= today):
        # A missing snooze_until on an ack is a row written before the
        # expiry was required, or by hand. Treated as expired, because the
        # alternative is an acknowledgement that never ends.
        state, expired = STATE_NEW, True

    return {
        "fingerprint": row.fingerprint,
        "state": state,
        "stored_state": row.state,
        "label": STATES[state].label if state in STATES else "New",
        "note": row.note,
        "snooze_until": row.snooze_until.isoformat() if row.snooze_until else None,
        "resolved_on": row.resolved_on.isoformat() if row.resolved_on else None,
        "regressed": regressed,
        "snooze_expired": expired,
        "first_seen": row.first_seen_date.isoformat(),
        "last_seen": row.last_seen_date.isoformat(),
        "days_seen": row.days_seen,
        # Days between first and last sighting, inclusive — "day 14" as a
        # reader would count it, not a count of observations.
        "age_days": (row.last_seen_date - row.first_seen_date).days + 1,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def states_for(db: DBSession, fingerprints: list[str], today: date_) -> dict[str, dict]:
    """Effective triage state for each fingerprint that has a row.

    Never raises: a finding with no triage row, or a lookup that fails
    outright, must still render. The dashboard degrades to the plain list
    it showed before this existed rather than to an error page.
    """
    if not fingerprints:
        return {}
    try:
        rows = db.execute(
            select(FindingState).where(FindingState.fingerprint.in_(fingerprints))
        ).scalars().all()
        return {r.fingerprint: effective(r, today) for r in rows}
    except Exception:  # noqa: BLE001 - see docstring
        log.warning("Could not read finding triage states", exc_info=True)
        return {}


def annotate(db: DBSession, findings: list[dict], today: date_) -> list[dict]:
    """Attach `triage` to each finding, and say which lane it belongs in.

    `hidden` is advisory and the payload still carries the finding: the
    dashboard offers to show what it is holding back, and the agent reads
    everything. A monitoring API that drops rows because a human parked
    them would make the parking invisible to the next reader, which is the
    failure this whole module exists to prevent.
    """
    by_fp = states_for(db, [f["fingerprint"] for f in findings if f.get("fingerprint")], today)
    out = []
    for f in findings:
        t = by_fp.get(f.get("fingerprint"))
        state = t["state"] if t else STATE_NEW
        hidden = state in STATES and not STATES[state].stays_visible
        out.append({**f, "triage": t, "triage_state": state, "hidden": hidden})
    return out


# ---------------------------------------------------------------------------
# Writing a decision
# ---------------------------------------------------------------------------


class TriageError(ValueError):
    """A decision the caller may not make. Carries a reason for the 422."""


def set_state(
    db: DBSession,
    *,
    fingerprint_: str,
    state: str,
    code: str | None = None,
    subject: str | None = None,
    note: str | None = None,
    snooze_days: int | None = None,
    user_id: int | None = None,
    today: date_,
) -> dict:
    """Record an admin's decision about one finding. Raises `TriageError`.

    Unlike everything above, this one does raise. It is a deliberate write
    by a person who is waiting for an answer, and silently dropping their
    decision — leaving them looking at a page that says the opposite of
    what they just did — is worse than an error.

    Creates the row if the finding has never been observed, which happens
    when a decision is made from a stale page or from an API client. The
    sighting dates then both read as today; a later observation corrects
    `first_seen` downward if the finding turns out to be older.
    """
    if state not in STATES:
        raise TriageError(
            f"unknown state {state!r}; expected one of {', '.join(sorted(STATES))}"
        )
    if not FINGERPRINT_RE.match(fingerprint_):
        raise TriageError("fingerprint must be 1-160 chars of [a-z0-9_.:-]")
    if note and len(note) > MAX_NOTE_CHARS:
        raise TriageError(f"note must be {MAX_NOTE_CHARS} characters or fewer")

    spec = STATES[state]
    snooze_until = None
    if spec.needs_snooze:
        days = DEFAULT_SNOOZE_DAYS if snooze_days is None else snooze_days
        if days < 1 or days > MAX_SNOOZE_DAYS:
            raise TriageError(
                f"snooze_days must be between 1 and {MAX_SNOOZE_DAYS}; "
                "an acknowledgement that never expires is how a dashboard "
                "stops being read"
            )
        snooze_until = today + timedelta(days=days)

    row = db.execute(
        select(FindingState).where(FindingState.fingerprint == fingerprint_)
    ).scalar_one_or_none()
    if row is None:
        row = FindingState(
            fingerprint=fingerprint_,
            code=code or fingerprint_.split(":", 1)[0],
            subject=subject or (fingerprint_.split(":", 1)[1] if ":" in fingerprint_ else None),
            state=state,
            first_seen_date=today,
            last_seen_date=today,
            days_seen=1,
        )
        db.add(row)

    row.state = state
    row.snooze_until = snooze_until
    # Cleared when leaving `resolved`, so a later re-resolve cannot be
    # judged against a stale date and reopen itself immediately.
    row.resolved_on = today if state == "resolved" else None
    if note is not None:
        row.note = note.strip() or None
    row.updated_by_user_id = user_id
    row.updated_at = utcnow()
    db.commit()
    db.refresh(row)
    return effective(row, today)
