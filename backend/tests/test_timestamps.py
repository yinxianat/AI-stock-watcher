"""Every timestamp says which timezone it is in.

The bug this closes is not a crash, it is a misreading: a monitoring row
saying "15:02" with nothing to place it, which a reader at 10am Eastern
cannot tell is three hours ago or five. It cost real time in this codebase
already.

The fix deliberately does NOT change what is on disk. Columns still hold
naive UTC, so no migration runs and the rows production already has keep
their meaning; `UTCDateTime` attaches the timezone on the way out. These
tests pin both halves, because either one alone is a silent data bug:
storage drifting would corrupt history, and reads going back to naive would
let the ambiguity return.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base, InfoLog, JobRun, LogEntry, UTCDateTime, utcnow
from app.services import diagnostics


def _engine():
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=eng)
    return eng


# ---------------------------------------------------------------------------
# The clock
# ---------------------------------------------------------------------------


def test_utcnow_is_timezone_aware_and_utc():
    now = utcnow()
    assert now.tzinfo is not None, "a naive clock is how the ambiguity got in"
    assert now.utcoffset() == timedelta(0)


def test_no_module_calls_the_naive_clock_directly():
    """One clock, or the aware and naive values meet and raise. `utcnow` is
    the only place `datetime.utcnow()` may appear."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "app"
    offenders = [
        str(p.relative_to(root)) for p in root.rglob("*.py")
        if "datetime.utcnow()" in p.read_text()
    ]
    assert not offenders, f"these bypass utcnow(): {offenders}"


# ---------------------------------------------------------------------------
# Reads carry the timezone
# ---------------------------------------------------------------------------


def test_every_monitoring_table_returns_aware_utc():
    eng = _engine()
    db = sessionmaker(bind=eng)()
    db.add_all([
        LogEntry(level="WARNING", logger="x", message="m"),
        InfoLog(level="INFO", logger="x", message="m"),
        JobRun(job_name="ingest", status="SUCCESS", started_at=utcnow(),
               duration_seconds=1.0),
    ])
    db.commit()
    for model in (LogEntry, InfoLog, JobRun):
        row = db.execute(select(model)).scalars().first()
        col = "started_at" if model is JobRun else "created_at"
        value = getattr(row, col)
        assert value.tzinfo is not None, f"{model.__tablename__}.{col} came back naive"
        assert value.utcoffset() == timedelta(0)
    db.close()


def test_isoformat_carries_an_explicit_offset():
    """What the API and the dashboard actually emit. Without the offset,
    JavaScript parses the string as the viewer's LOCAL time and every
    timestamp on the page silently shifts."""
    eng = _engine()
    db = sessionmaker(bind=eng)()
    db.add(LogEntry(level="ERROR", logger="x", message="m"))
    db.commit()
    iso = db.execute(select(LogEntry)).scalars().one().created_at.isoformat()
    db.close()
    assert iso.endswith("+00:00"), iso


# ---------------------------------------------------------------------------
# Nothing on disk changed
# ---------------------------------------------------------------------------


def test_storage_is_still_naive_utc_so_no_migration_is_needed():
    """The column keeps holding naive UTC. `create_all` adds missing tables
    but never alters existing columns, so a storage change would need a
    hand-run migration against production — this proves none is required."""
    eng = _engine()
    db = sessionmaker(bind=eng)()
    db.add(LogEntry(level="WARNING", logger="x", message="m",
                    created_at=datetime(2026, 11, 3, 15, 2, tzinfo=timezone.utc)))
    db.commit()
    db.close()
    with eng.connect() as c:
        raw = c.execute(text("select created_at from log_entries")).scalar_one()
    assert "+" not in str(raw) and "Z" not in str(raw), f"offset leaked into storage: {raw}"
    assert str(raw).startswith("2026-11-03 15:02"), raw


def test_rows_written_before_this_change_still_mean_utc():
    """The important one for safety: production is full of naive rows. They
    must come back as aware UTC — the same instant, relabelled — never
    shifted by an offset."""
    eng = _engine()
    with eng.begin() as c:
        c.execute(text(
            "insert into log_entries (created_at, level, logger, message) "
            "values ('2026-11-03 15:02:00', 'WARNING', 'legacy', 'old row')"
        ))
    db = sessionmaker(bind=eng)()
    row = db.execute(select(LogEntry)).scalars().one()
    db.close()
    assert row.created_at == datetime(2026, 11, 3, 15, 2, tzinfo=timezone.utc)
    assert row.created_at.astimezone(diagnostics._ET).hour == 10  # 15:02 UTC = 10:02 EST


def test_an_aware_value_is_converted_rather_than_stored_as_written():
    """A caller in another zone must not write wall-clock time into a UTC
    column. 10:02 Eastern is 15:02 UTC, and that is what lands."""
    eng = _engine()
    db = sessionmaker(bind=eng)()
    db.add(LogEntry(level="WARNING", logger="x", message="m",
                    created_at=datetime(2026, 11, 3, 10, 2, tzinfo=diagnostics._ET)))
    db.commit()
    db.close()
    with eng.connect() as c:
        raw = str(c.execute(text("select created_at from log_entries")).scalar_one())
    assert raw.startswith("2026-11-03 15:02"), raw


def test_naive_values_still_bind_in_queries():
    """Thousands of existing comparisons pass naive datetimes into queries.
    The type trusts them as UTC rather than rejecting them, so none of that
    code had to change."""
    eng = _engine()
    db = sessionmaker(bind=eng)()
    db.add(LogEntry(level="WARNING", logger="x", message="m"))
    db.commit()
    naive_cutoff = datetime.utcnow() - timedelta(hours=1)  # noqa: DTZ003 - the point
    found = db.execute(
        select(LogEntry).where(LogEntry.created_at >= naive_cutoff)
    ).scalars().all()
    db.close()
    assert len(found) == 1


def test_the_type_handles_none():
    assert UTCDateTime().process_result_value(None, None) is None
    assert UTCDateTime().process_bind_param(None, None) is None


# ---------------------------------------------------------------------------
# What a human reads
# ---------------------------------------------------------------------------


def test_eastern_rendering_is_right_on_both_sides_of_dst():
    """The off-by-one-hour bug twice a year. November is EST (UTC-5), July
    is EDT (UTC-4), and a monitoring page that gets this wrong sends someone
    to look at the wrong hour."""
    est = datetime(2026, 11, 3, 15, 2, tzinfo=timezone.utc)
    edt = datetime(2026, 7, 1, 15, 2, tzinfo=timezone.utc)
    assert "10:02" in diagnostics.et(est)
    assert "11:02" in diagnostics.et(edt)
    assert diagnostics.et(est).endswith("ET")


def test_eastern_rendering_treats_a_legacy_naive_value_as_utc():
    assert "10:02" in diagnostics.et(datetime(2026, 11, 3, 15, 2))


def test_et_handles_none():
    assert diagnostics.et(None) is None


def test_monitoring_rows_ship_both_clocks(client):
    """Both forms, because they answer different questions: the ISO value is
    what a machine sorts on, the ET string is what a human compares against
    a 09:35 ET schedule."""
    from app.db.database import get_session_factory

    db = get_session_factory()()
    db.add(LogEntry(level="ERROR", logger="x", message="boom"))
    db.add(JobRun(job_name="ingest", status="SUCCESS", started_at=utcnow(),
                  duration_seconds=1.0))
    db.commit()
    day = utcnow().astimezone(diagnostics._ET).date()
    errors = diagnostics.errors_report(day, db)
    jobs = diagnostics.jobs_report(day, db)
    db.close()

    item = errors["items"][0]
    assert item["created_at"].endswith("+00:00")
    assert item["created_et"].endswith("ET")

    ran = [s for s in jobs["slots"] if s["actual_utc"]]
    for slot in ran:
        assert slot["actual_et"].endswith("ET")


def test_the_report_states_its_timezone_convention(client):
    """Stated in the payload, not just in a docstring: the dashboard labels
    its clocks from this and the monitoring agent has no other way to know."""
    from app.db.database import get_session_factory

    db = get_session_factory()()
    day = utcnow().astimezone(diagnostics._ET).date()
    report = diagnostics.day_report(day, db)
    db.close()
    tz = report["timezone"]
    assert tz["stored"] == "UTC"
    assert tz["iso_fields_carry_offset"] is True
    assert tz["display"] == "America/New_York"
    assert report["generated_at"].endswith("+00:00")
    assert report["generated_et"].endswith("ET")
