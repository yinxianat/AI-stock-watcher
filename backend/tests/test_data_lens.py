"""Data Lens: the read-only query console.

The tests are grouped by what they protect. The safety group is the one that
matters: each test there is a way someone could get a write or a credential
column past the validator, written down so a later refactor has to keep
refusing it.
"""

from __future__ import annotations

import pytest

from app.core.settings import get_settings
from app.models import DailyClose, Ticker, TickerType, User, utcnow
from app.services import data_lens, query_library
from app.services.data_lens import QueryRejected


@pytest.fixture
def lens(engine, monkeypatch):
    """`data_lens` pointed at this test's in-memory database."""
    from app.db import database as db_mod

    monkeypatch.setattr(db_mod, "get_engine", lambda: engine)
    get_settings.cache_clear()
    return engine


def _run(sql: str, **kw):
    return data_lens.run_query(sql, actor="test", **kw)


# ---------------------------------------------------------------------------
# Safety: nothing here may write, and credential columns never leave
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sql", [
    "INSERT INTO users (email) VALUES ('x@y.com')",
    "UPDATE users SET email = 'x@y.com'",
    "DELETE FROM users",
    "DROP TABLE users",
    "ALTER TABLE users ADD COLUMN x int",
    "CREATE TABLE evil (id int)",
    "TRUNCATE users",
    "GRANT ALL ON users TO public",
    "COPY users TO '/tmp/out.csv'",
    "VACUUM",
])
def test_a_statement_that_is_not_a_select_never_runs(sql):
    with pytest.raises(QueryRejected):
        data_lens.validate(sql, limit=10)


def test_a_write_smuggled_after_a_select_is_refused():
    """The classic: a valid query, a semicolon, then the real payload."""
    with pytest.raises(QueryRejected) as e:
        data_lens.validate("SELECT 1; DROP TABLE users", limit=10)
    assert "one statement" in e.value.message


def test_a_write_hidden_behind_a_comment_is_still_seen():
    """Comments are stripped before scanning, so the DROP is left exposed
    rather than hidden on the far side of a `--`."""
    with pytest.raises(QueryRejected) as e:
        data_lens.validate("SELECT 1 -- harmless\nDROP TABLE users", limit=10)
    assert "DROP" in e.value.message


def test_a_keyword_inside_a_string_literal_is_not_a_write(lens):
    """The other half of the stripper: scanning raw text would reject this
    perfectly innocent query, and a console that cries wolf stops being used."""
    r = _run("SELECT 'drop table users' AS harmless_text")
    assert r.rows == [["drop table users"]]


def test_dollar_quoting_is_refused_rather_than_parsed():
    """Postgres's other string syntax would hide a whole statement from the
    scanner, and nothing a read-only question needs uses `$`."""
    with pytest.raises(QueryRejected) as e:
        data_lens.validate("SELECT $$anything$$", limit=10)
    assert "$" in e.value.message


@pytest.mark.parametrize("sql", [
    "SELECT token_hash FROM sessions",
    "SELECT token_hash AS t FROM sessions",
    "SELECT s.token_hash FROM sessions s",
    "SELECT substr(token_hash, 1, 8) FROM sessions",
    'SELECT "token_hash" FROM sessions',
    "SELECT id FROM sessions ORDER BY token_hash",
    "SELECT id FROM login_tokens WHERE token_hash LIKE 'a%'",
])
def test_a_credential_column_is_refused_however_it_is_asked_for(sql):
    """Aliasing, qualifying, quoting or wrapping it in a function are all the
    same request, so all of them are refused."""
    with pytest.raises(QueryRejected) as e:
        data_lens.validate(sql, limit=10)
    assert "token_hash" in e.value.message


def test_select_star_cannot_leak_a_credential_column(lens, db_session):
    """`SELECT *` names nothing the validator can object to, so the result
    itself is filtered — the second net, and the one this needs."""
    from app.models import Session as SessionRow

    db_session.add(User(email="a@b.com", notify_email="a@b.com"))
    db_session.flush()
    db_session.add(SessionRow(
        user_id=1, token_hash="deadbeef" * 8,
        expires_at=utcnow(),
    ))
    db_session.commit()

    r = _run("SELECT * FROM sessions")
    assert "token_hash" not in r.columns
    assert not any("deadbeef" in str(c) for row in r.rows for c in row)
    assert any("credential" in n for n in r.notes)


def test_the_schema_catalogue_never_advertises_a_credential_column():
    served = {
        c["name"]
        for t in data_lens.schema_catalogue()["tables"]
        for c in t["columns"]
    }
    assert "token_hash" not in served


@pytest.mark.parametrize("sql", [
    "SELECT * FROM pg_catalog.pg_user",
    "SELECT pg_sleep(10)",
    "SELECT * FROM information_schema.columns",
    "SELECT * FROM sqlite_master",
    "SELECT pg_read_file('/etc/passwd')",
    "SELECT current_setting('data_directory')",
])
def test_reaching_outside_our_own_tables_is_refused(sql):
    with pytest.raises(QueryRejected):
        data_lens.validate(sql, limit=10)


def test_an_unknown_table_fails_closed_with_a_suggestion():
    """Fails closed: a table this build has never heard of is refused rather
    than passed through to see what happens."""
    with pytest.raises(QueryRejected) as e:
        data_lens.validate("SELECT * FROM userz", limit=10)
    assert "not a table" in e.value.message
    assert "`users`" in e.value.message  # did-you-mean


def test_another_schema_is_refused():
    with pytest.raises(QueryRejected):
        data_lens.validate("SELECT * FROM secrets.users", limit=10)


def test_the_public_schema_is_accepted():
    plan = data_lens.validate("SELECT id FROM public.users", limit=10)
    assert plan.tables == ["users"]


def test_an_empty_query_is_refused_kindly():
    with pytest.raises(QueryRejected) as e:
        data_lens.validate("   ", limit=10)
    assert e.value.hint


def test_every_rejection_explains_what_to_do_instead():
    """The hint is the teaching half. A console that only says "no" teaches
    the admin to stop asking, which is how the database became a mystery in
    the first place."""
    for sql in ("DELETE FROM users", "SELECT token_hash FROM sessions",
                "SELECT * FROM nope", "SELECT 1; SELECT 2"):
        with pytest.raises(QueryRejected) as e:
            data_lens.validate(sql, limit=10)
        assert e.value.hint, sql


# ---------------------------------------------------------------------------
# Validation of legitimate SQL — the false-positive side
# ---------------------------------------------------------------------------


def test_a_cte_name_is_not_mistaken_for_an_unknown_table():
    plan = data_lens.validate(
        "WITH recent AS (SELECT id FROM users) SELECT * FROM recent", limit=10,
    )
    assert plan.tables == ["users"]


def test_extract_from_a_column_is_not_a_table_reference():
    """`EXTRACT(EPOCH FROM finished_at)` looks exactly like a table reference
    to a scanner, and rejecting it would rule out most useful date queries."""
    plan = data_lens.validate(
        "SELECT EXTRACT(EPOCH FROM finished_at) FROM job_runs", limit=10,
    )
    assert plan.tables == ["job_runs"]


def test_a_subquery_in_the_from_clause_is_allowed():
    plan = data_lens.validate(
        "SELECT x.id FROM (SELECT id FROM users) AS x", limit=10,
    )
    assert plan.tables == ["users"]


def test_case_end_and_the_replace_function_are_not_write_keywords():
    """Both were on an early forbidden-word list and both are ordinary
    SELECT syntax."""
    data_lens.validate(
        "SELECT CASE WHEN enabled THEN 1 ELSE 0 END, replace(event_type, '_', ' ') "
        "FROM notification_rules",
        limit=10,
    )


def test_joins_collect_every_table_touched():
    plan = data_lens.validate(
        "SELECT u.id FROM users u JOIN watchlist_items w ON w.user_id = u.id "
        "LEFT JOIN tickers t ON t.id = w.ticker_id",
        limit=10,
    )
    assert set(plan.tables) == {"users", "watchlist_items", "tickers"}


# ---------------------------------------------------------------------------
# Execution and bounds
# ---------------------------------------------------------------------------


def _seed_closes(db, n: int) -> None:
    t = Ticker(symbol="AAA", name="A", type=TickerType.STOCK)
    db.add(t)
    db.flush()
    from datetime import date, timedelta

    base = date(2026, 1, 1)
    for i in range(n):
        db.add(DailyClose(ticker_id=t.id, date=base + timedelta(days=i), close=100.0 + i))
    db.commit()


def test_a_query_returns_columns_and_rows(lens, db_session):
    _seed_closes(db_session, 3)
    r = _run("SELECT date, close FROM daily_closes ORDER BY date")
    assert r.columns == ["date", "close"]
    assert r.row_count == 3
    assert r.rows[0] == ["2026-01-01", 100.0]
    assert r.tables == ["daily_closes"]


def test_a_missing_limit_is_added_and_said_so(lens, db_session):
    _seed_closes(db_session, 2)
    r = _run("SELECT close FROM daily_closes")
    assert "LIMIT" in r.sql
    assert any("LIMIT" in n for n in r.notes)


def test_the_row_cap_truncates_and_admits_it(lens, db_session):
    _seed_closes(db_session, 12)
    r = _run("SELECT close FROM daily_closes", limit=5)
    assert r.row_count == 5
    assert r.truncated is True
    assert any("More than 5 rows" in n for n in r.notes)


def test_the_cap_is_enforced_when_the_cursor_is_read_not_just_in_the_text(
    lens, db_session,
):
    """A query carrying its own enormous LIMIT must still not stream the
    table into the browser — which is why the bound lives at the fetch."""
    _seed_closes(db_session, 12)
    r = _run("SELECT close FROM daily_closes LIMIT 100000", limit=4)
    assert r.row_count == 4
    assert r.truncated is True


def test_the_request_cannot_raise_the_configured_ceiling(lens, db_session, monkeypatch):
    _seed_closes(db_session, 12)
    monkeypatch.setenv("DATA_LENS_MAX_ROWS", "3")
    get_settings.cache_clear()
    r = _run("SELECT close FROM daily_closes", limit=1000)
    assert r.row_count == 3


def test_a_database_error_comes_back_as_the_databases_own_complaint(lens):
    """Passed through rather than replaced: "no such column" is the single
    most useful teaching message in the system."""
    with pytest.raises(QueryRejected) as e:
        _run("SELECT nope FROM users")
    assert "nope" in e.value.message
    assert e.value.hint


def test_the_console_can_be_switched_off(lens, monkeypatch):
    monkeypatch.setenv("DATA_LENS_ENABLED", "false")
    get_settings.cache_clear()
    with pytest.raises(QueryRejected) as e:
        _run("SELECT 1")
    assert "switched off" in e.value.message


def test_nothing_the_console_runs_is_committed(lens, db_session):
    """Belt and braces around the read-only transaction: even a statement
    that somehow wrote would be rolled back."""
    _seed_closes(db_session, 2)
    before = db_session.query(DailyClose).count()
    _run("SELECT close FROM daily_closes")
    assert db_session.query(DailyClose).count() == before


# ---------------------------------------------------------------------------
# Masking and formatting
# ---------------------------------------------------------------------------


def test_email_addresses_are_masked_by_default(lens, db_session):
    db_session.add(User(email="alice@example.com", notify_email="alice@example.com"))
    db_session.commit()
    r = _run("SELECT id, email FROM users")
    assert r.masked is True
    assert r.rows[0][1].startswith("al")
    assert "alice@example.com" not in r.rows[0][1]
    assert r.rows[0][1].endswith("@example.com")
    assert r.rows[0][0] == 1, "the row id stays, so rows can still be correlated"


def test_masking_can_be_turned_off_deliberately(lens, db_session, monkeypatch):
    db_session.add(User(email="alice@example.com", notify_email="alice@example.com"))
    db_session.commit()
    monkeypatch.setenv("DATA_LENS_SHOW_EMAILS", "true")
    get_settings.cache_clear()
    r = _run("SELECT email FROM users")
    assert r.rows[0][0] == "alice@example.com"
    assert r.masked is False


def test_an_email_inside_a_log_message_is_masked_too(lens, db_session):
    """Masking is applied to every text value, not just columns named
    `email` — a log line is the likeliest place for an address to turn up
    somewhere nobody expected."""
    from app.models import LogEntry

    db_session.add(LogEntry(
        level="WARNING", logger="app.jobs.notify",
        message="could not send to bob@example.com, retrying",
    ))
    db_session.commit()
    r = _run("SELECT message FROM log_entries")
    assert "bob@example.com" not in r.rows[0][0]
    assert "retrying" in r.rows[0][0]


def test_timestamps_keep_their_offset(lens, db_session):
    """An unlabelled timestamp on a monitoring page is how someone reads the
    wrong hour; `UTCDateTime` hands back aware values so this carries +00:00."""
    db_session.add(User(email="a@b.com", notify_email="a@b.com"))
    db_session.commit()
    r = _run("SELECT created_at FROM users")
    assert r.rows[0][0].endswith("+00:00")


def test_a_very_long_value_is_truncated_with_its_real_length(lens, db_session):
    from app.models import LogEntry

    db_session.add(LogEntry(level="INFO", logger="x", message="y" * 5000))
    db_session.commit()
    r = _run("SELECT message FROM log_entries")
    assert len(r.rows[0][0]) < 1000
    assert "5000 chars total" in r.rows[0][0]


# ---------------------------------------------------------------------------
# The catalogues stay complete as the schema grows
# ---------------------------------------------------------------------------


def test_every_table_in_the_orm_has_a_note():
    """The enforcement that keeps this honest, mirroring the delivery reason
    catalogue. A table nobody documented is exactly the table whose contents
    nobody understands a year later — which is the problem this console was
    built to fix, so it must not reintroduce it."""
    from app.models import Base

    missing = sorted(set(Base.metadata.tables) - set(data_lens.TABLE_NOTES))
    assert not missing, (
        f"Add a TableNote in app/services/data_lens.py for: {missing}"
    )


def test_no_note_describes_a_table_that_no_longer_exists():
    from app.models import Base

    stale = sorted(set(data_lens.TABLE_NOTES) - set(Base.metadata.tables))
    assert not stale, f"Remove these TableNotes: {stale}"


def test_each_notes_time_column_really_exists():
    """The console sorts by this column, so a typo here is a broken panel."""
    from app.models import Base

    for name, note in data_lens.TABLE_NOTES.items():
        if note.time_column is None:
            continue
        cols = {c.name for c in Base.metadata.tables[name].columns}
        assert note.time_column in cols, f"{name}.{note.time_column}"


def test_tickers_is_the_only_table_with_no_time_column():
    """Called out because the console's "sort by newest" promise has one
    exception, and it should be a known one rather than a surprise."""
    without = {
        n for n, note in data_lens.TABLE_NOTES.items() if note.time_column is None
    }
    assert without == {"tickers"}


def test_every_saved_query_passes_validation():
    """A saved query that the validator rejects is worse than no saved query:
    it teaches the admin the console is broken."""
    for q in query_library.QUERIES:
        try:
            data_lens.validate(q.sql, limit=100)
        except QueryRejected as e:
            pytest.fail(f"{q.key}: {e.message}")


def test_saved_query_keys_are_unique():
    keys = [q.key for q in query_library.QUERIES]
    assert len(keys) == len(set(keys))


def test_every_saved_query_is_filed_under_a_real_category():
    known = {k for k, _ in query_library.CATEGORIES}
    for q in query_library.QUERIES:
        assert q.category in known, q.key


def test_every_saved_query_says_what_it_teaches():
    """The console has two jobs: answer the question, and leave the admin
    able to write the next query without it."""
    for q in query_library.QUERIES:
        assert q.teaches.strip(), q.key
        assert q.question.strip(), q.key


def test_the_catalogue_groups_every_query():
    cat = query_library.catalogue()
    grouped = sum(len(c["queries"]) for c in cat["categories"])
    assert grouped == cat["count"] == len(query_library.QUERIES)


# ---------------------------------------------------------------------------
# The HTTP surface
# ---------------------------------------------------------------------------


@pytest.fixture
def admin_headers(client, monkeypatch):
    client.post("/auth/request-link", json={"email": "owner@example.com"})
    token = client.sent_emails[-1]["body_text"].split("token=")[1].split("\n")[0].strip()
    data = client.post("/auth/verify", json={"token": token}).json()
    monkeypatch.setenv("ADMIN_EMAILS", "owner@example.com")
    get_settings.cache_clear()
    return {"Authorization": f"Bearer {data['session_token']}"}


def test_the_console_needs_an_admin_session(client):
    for path in ("/admin/data-lens/schema", "/admin/data-lens/queries"):
        assert client.get(path).status_code == 401, path
    assert client.post("/admin/data-lens/query", json={"sql": "SELECT 1"}).status_code == 401


def test_an_ordinary_user_is_refused(client, signed_in):
    r = client.post(
        "/admin/data-lens/query",
        json={"sql": "SELECT 1"},
        headers=signed_in["auth"],
    )
    assert r.status_code == 403


def test_an_admin_can_run_a_query(client, admin_headers):
    r = client.post(
        "/admin/data-lens/query",
        json={"sql": "SELECT COUNT(*) AS users FROM users"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["columns"] == ["users"]
    assert body["timezone"]["stored"] == "UTC"
    assert body["limits"]["max_rows"] == get_settings().data_lens_max_rows


def test_a_rejected_query_returns_the_reason_and_the_hint(client, admin_headers):
    r = client.post(
        "/admin/data-lens/query",
        json={"sql": "DELETE FROM users"},
        headers=admin_headers,
    )
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "SELECT" in detail["error"] or "DELETE" in detail["error"]
    assert detail["hint"]


def test_the_schema_endpoint_describes_every_table(client, admin_headers):
    from app.models import Base

    body = client.get("/admin/data-lens/schema", headers=admin_headers).json()
    assert {t["table"] for t in body["tables"]} == set(Base.metadata.tables)
    assert all(t["purpose"] for t in body["tables"])


def test_the_saved_queries_endpoint_is_grouped(client, admin_headers):
    body = client.get("/admin/data-lens/queries", headers=admin_headers).json()
    assert body["count"] > 10
    assert body["categories"][0]["queries"]
