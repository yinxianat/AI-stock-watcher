"""Data Lens — read-only SQL against the live database, for a human admin.

Why this exists: Railway's console is the only way to ask the production
database a question, and it is a poor place to learn in. There is no history,
no schema help, and one mistyped keyword is a write against real data. So the
questions never get asked, and the answers get guessed instead.

This module is the safety layer that makes asking cheap. The guarantee is
layered, because any single check can be wrong:

1. **The transaction is read-only.** On Postgres every query runs inside
   `SET TRANSACTION READ ONLY`, and the connection is rolled back on every
   path including exceptions. This is the one that actually holds: even if
   everything below is defeated, the database itself refuses the write.
2. **The statement is validated** before it is sent (`validate`). This is
   not the guarantee — it is how the admin gets a sentence they can learn
   from instead of a Postgres error, and how columns the database would
   happily return (credential hashes) are kept out.
3. **The result is bounded** — a row cap applied when reading the cursor, not
   just a `LIMIT` in the text, so a query that smuggles past the cap still
   can't stream the table into a browser.
4. **Personal data is masked on the way out** unless the admin deliberately
   turns that off, so a screenshot or a stolen session yields pseudonyms.

What this module deliberately does NOT do is parse SQL properly. A real
parser (sqlglot and friends) would be more precise about where an identifier
appears, and it is the right answer if this ever accepts queries from anyone
but the owner of the deployment. For one admin, against a read-only
transaction, a conservative scanner that fails *closed* on anything it does
not recognise is the better trade: no new dependency, and no false sense that
the text analysis is what keeps the data safe. It isn't; (1) is.
"""

from __future__ import annotations

import difflib
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date as date_
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.core.settings import get_settings
from app.models import Base

log = logging.getLogger(__name__)


class QueryRejected(Exception):
    """A query failed validation and was never sent to the database.

    `hint` is the teaching half of the message: what to do instead. The
    console shows both, because "rejected" without "try this" is how an
    admin learns to stop asking.
    """

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


# ---------------------------------------------------------------------------
# What is off limits
# ---------------------------------------------------------------------------

# Columns never served, whatever the query asks for. These hold the SHA-256
# of a live credential. The digest can't be replayed, but it is still the
# material half of a session token and has no diagnostic use whatsoever —
# nobody debugging a login needs the hash, they need `created_at`,
# `expires_at` and `consumed_at`.
BLOCKED_COLUMNS: frozenset[str] = frozenset({"token_hash"})

# Statement words that have no place in a read-only question. Checked as
# whole words against the query with comments and string literals removed.
#
# Two notes on what is NOT here. `END` is absent because `CASE … END` is
# ordinary SELECT syntax, and `REPLACE` is absent because `replace(col, …)`
# is a useful string function — the write form `REPLACE INTO` is already
# stopped by `INTO`. Both were in an earlier draft of this list and both
# would have rejected perfectly good queries, which is its own kind of
# failure: a console that cries wolf stops being used.
FORBIDDEN_WORDS: frozenset[str] = frozenset({
    "insert", "update", "delete", "drop", "alter", "create", "truncate",
    "grant", "revoke", "copy", "vacuum", "analyze", "reindex", "cluster",
    "lock", "set", "reset", "call", "do", "execute", "prepare", "deallocate",
    "discard", "listen", "notify", "unlisten", "attach", "detach", "pragma",
    "merge", "into", "returning", "savepoint", "commit", "rollback", "begin",
    "start", "comment", "refresh", "import", "load", "install", "restore",
    "backup", "current_setting", "load_extension", "dblink", "lo_import",
    "lo_export",
})

# Identifier prefixes that reach outside the application's own schema:
# server internals, the on-disk catalogue, other people's settings. Blocking
# the prefix rather than naming functions one by one means `pg_sleep`,
# `pg_read_file` and `pg_catalog.*` are all covered by one rule, and a
# function added in the next Postgres release is covered before we hear
# about it.
FORBIDDEN_PREFIXES: tuple[str, ...] = ("pg_", "sqlite_")
FORBIDDEN_IDENTIFIERS: frozenset[str] = frozenset({"information_schema"})

_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# A table reference: FROM/JOIN followed by an identifier, optionally
# schema-qualified. A following "(" (a subquery or VALUES) simply doesn't
# match, which is correct — the real tables inside it are matched separately.
_FROM_JOIN = re.compile(
    r"\b(?:from|join)\s+([A-Za-z_][A-Za-z0-9_]*(?:\s*\.\s*[A-Za-z_][A-Za-z0-9_]*)?)",
    re.IGNORECASE,
)
# `WITH name AS (` and `, name AS (`, including RECURSIVE and MATERIALIZED.
_CTE_NAME = re.compile(
    r"(?:\bwith\b|,)\s*(?:recursive\s+)?([A-Za-z_][A-Za-z0-9_]*)\s+as\s*"
    r"(?:not\s+)?(?:materialized\s*)?\(",
    re.IGNORECASE,
)
_LINE_COMMENT = re.compile(r"--[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_SINGLE_QUOTED = re.compile(r"'(?:[^']|'')*'")
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# Exactly the shape SQLAlchemy writes into a SQLite DateTime column. Anchored
# and narrow on purpose — see `_cell` for why a string ever needs sniffing.
_SQLITE_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?$")

# Long text columns (an AI story's JSON runs to several KB) are truncated for
# display. The console is for spotting shape and anomalies; reading a whole
# story is what the stories card is for.
_MAX_CELL_CHARS = 600


# ---------------------------------------------------------------------------
# The table catalogue
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TableNote:
    """What one table is for, in the words an admin needs.

    `purpose` answers "what does a row here mean", which is the question the
    column names don't answer. `lifecycle` answers "why is this table small /
    why does it keep growing", which is the one that causes wrong conclusions
    — querying `price_snapshots` for history returns today only, by design,
    and without this note that reads as data loss.
    """

    purpose: str
    lifecycle: str
    written_by: str
    time_column: str | None = None


# Keyed by table name. Every mapped table must appear here; the test suite
# fails if a model is added without a note, the same way the delivery reason
# catalogue is enforced. That is deliberate: a table nobody documented is
# exactly the table whose contents nobody understands later.
TABLE_NOTES: dict[str, TableNote] = {
    # --- identity & access ---
    "users": TableNote(
        "One row per registered person. The email is the identity — there are "
        "no passwords. `notify_email` is where alerts go and may differ from "
        "the login email, but only after it has been confirmed.",
        "Durable. Never pruned.",
        "the sign-in flow (app/services/auth.py)",
        "created_at",
    ),
    "login_tokens": TableNote(
        "A magic-link token. Only the hash is stored, so a row here cannot be "
        "turned back into a working link. `consumed_at` set means the link was "
        "used; null with `expires_at` in the past means it was never clicked.",
        "Expiring. Rows outlive their use and are cleaned up.",
        "the sign-in flow (app/services/auth.py)",
        "created_at",
    ),
    "sessions": TableNote(
        "A signed-in browser session, keyed by a hashed token. `revoked_at` is "
        "a sign-out.",
        "Expiring.",
        "the sign-in flow (app/services/auth.py)",
        "created_at",
    ),
    "confirmation_tokens": TableNote(
        "A pending change of notification email, waiting for the user to click "
        "the confirmation link. Until it is consumed, alerts keep going to the "
        "old address.",
        "Expiring.",
        "app/services/confirm_email.py",
        "created_at",
    ),
    "admin_roles": TableNote(
        "Grants one user access to /admin. Additive to the ADMIN_EMAILS "
        "environment variable, which is the bootstrap that works before "
        "anyone has signed in.",
        "Durable. Tiny by nature.",
        "inserted by hand",
        "granted_at",
    ),
    "service_sessions": TableNote(
        "A short-lived token issued to the monitoring agent in exchange for "
        "ADMIN_API_TOKEN, so the long-lived secret crosses the wire rarely. "
        "`client_net` is a /24, not a full address.",
        "Expiring, minutes not days.",
        "app/services/service_auth.py",
        "created_at",
    ),

    # --- what people watch ---
    "tickers": TableNote(
        "The catalogue of symbols the app knows about — seeded ones plus "
        "anything a user has added. The only table with no timestamp at all, "
        "so it is the one table this console cannot sort by time.",
        "Durable.",
        "app/db/seed.py and the add-ticker endpoint",
        None,
    ),
    "watchlist_items": TableNote(
        "A (user, ticker) pair: this person is watching this symbol. Being on "
        "a watchlist is what puts a ticker in the recap; it is NOT what "
        "triggers an alert — that needs a rule.",
        "Durable.",
        "the watchlist endpoints",
        "created_at",
    ),
    "notification_rules": TableNote(
        "A trigger: (user, ticker, event_type), at most one of each "
        "combination. For price_change_range, `pct_low`/`pct_high` are the "
        "band — a move outside it fires. `enabled` false is a rule switched "
        "off without being deleted.",
        "Durable.",
        "the rules endpoints",
        "created_at",
    ),
    "story_preferences": TableNote(
        "Which age groups' stories a parent wants. No row means the default "
        "(ages 6-9). Only the group is stored — never a child's age or name.",
        "Durable. One row per user at most.",
        "the preferences endpoints",
        "updated_at",
    ),
    "email_opt_outs": TableNote(
        "A row means this user unsubscribed from alert emails. Deleting the "
        "row resubscribes them. Sign-in links still send — those are "
        "transactional.",
        "Durable. Presence is the whole meaning.",
        "the unsubscribe link",
        "created_at",
    ),
    "recap_opt_outs": TableNote(
        "A row means this user switched the daily recap off. Opting out of "
        "all alert emails (email_opt_outs) stops the recap too.",
        "Durable. Presence is the whole meaning.",
        "the recap footer link",
        "created_at",
    ),

    # --- prices ---
    "daily_closes": TableNote(
        "One closing price per (ticker, trading day). This is the SOURCE OF "
        "TRUTH for price history, and the baseline every day-over-day alert "
        "percentage is measured against. A gap here is why an alert can quote "
        "a percentage nobody else is quoting.",
        "Pruned on PRICE_HISTORY_LIFETIME. Also kept as model training data, "
        "so the window is set long deliberately.",
        "the ingest job (app/jobs/ingest.py)",
        "date",
    ),
    "intraday_prices": TableNote(
        "A price tick during market hours, every INTRADAY_TICK_MINUTES.",
        "Pruned on INTRADAY_RETENTION, days not months.",
        "the intraday job (app/jobs/intraday.py)",
        "captured_at",
    ),
    "price_snapshots": TableNote(
        "The derived current view per ticker: latest price, the previous "
        "trading day's close, and the high/low of each window EXCLUDING "
        "today. Read it as 'where each ticker stands right now', never as "
        "history — see the lifecycle.",
        "REPLACED WHOLESALE on every batch run. One row per ticker, and "
        "yesterday's contents are gone. Nothing durable may be stored here.",
        "the compute job (app/jobs/compute.py)",
        "captured_at",
    ),
    "trend_analyses": TableNote(
        "Per (user, ticker) booleans derived from the snapshot: did today set "
        "a new weekly / monthly / quarterly / yearly extreme.",
        "REPLACED WHOLESALE on every compute run, like price_snapshots.",
        "the compute job (app/jobs/compute.py)",
        "computed_at",
    ),

    # --- what was sent ---
    "notification_logs": TableNote(
        "An alert email that went out. Also the duplicate guard: the notify "
        "job checks here before sending, so a row's absence is the reason a "
        "re-run sends again.",
        "Durable, unbounded. Grows with emails sent, not with time.",
        "the notify and intraday jobs",
        "sent_at",
    ),
    "recap_sends": TableNote(
        "One row per (user, trading day) recap sent. Unique on that pair, so "
        "a restart or a re-run can never send the same day's recap twice.",
        "Durable, unbounded.",
        "the recap job (app/jobs/recap.py)",
        "sent_at",
    ),
    "money_idea_sends": TableNote(
        "Which Money Idea each user got, and when. Drives their progress "
        "through the 20-idea curriculum and the Money Ideas page.",
        "Durable, unbounded.",
        "the recap job (app/jobs/recap.py)",
        "sent_at",
    ),
    "delivery_outcomes": TableNote(
        "WHY one email carried an AI story or didn't — recorded by the "
        "sending job at the moment it decided, not worked out afterwards. "
        "This is the table to query first when someone says they got nothing: "
        "`reason` is a code from the catalogue and `detail` is the sentence "
        "with that send's real numbers in it.",
        "Durable, unbounded. It is the only record of what a family saw.",
        "every sending job, via app/services/delivery.py",
        "sent_at",
    ),
    "article_followups": TableNote(
        "A user whose alert said \"we're still looking\" and is owed the story "
        "as a threaded reply. `status` is waiting → sent | abandoned.",
        "Durable.",
        "app/services/articles/",
        "created_at",
    ),

    # --- AI content ---
    "stock_articles": TableNote(
        "One AI \"why it moved\" story per (ticker, trading day, direction), "
        "shared by every user whose alert fired for that move so it is only "
        "written once. `status` is pending → ready | no_clear_cause | failed. "
        "The numbers in emails come from the price columns here, never from "
        "the AI.",
        "Durable, unbounded, and never to be pruned — this is model training "
        "data.",
        "app/services/articles/service.py",
        "created_at",
    ),
    "content_feedback": TableNote(
        "A parent's like / neutral / dislike on one story or Money Idea. The "
        "latest rating wins. `context_json` carries the state at rating time "
        "so a future retrieval pipeline needn't re-join half the schema.",
        "Durable, and never to be pruned — training labels.",
        "the feedback endpoints",
        "created_at",
    ),

    # --- observability ---
    "job_runs": TableNote(
        "One row per scheduled job execution, with status, duration and a "
        "one-line result summary. The daily health digest counts runs from "
        "here rather than from log text, so this is the authority on whether "
        "a pipeline ran.",
        "Pruned on JOB_RUNS_RETENTION.",
        "every job, via the run wrapper",
        "started_at",
    ),
    "log_entries": TableNote(
        "Durable WARNING-and-above application log. ERROR and above also "
        "email the admin, deduped for 15 minutes.",
        "Pruned on LOG_LIFETIME.",
        "the logging handlers (app/services/alerts.py)",
        "created_at",
    ),
    "info_logs": TableNote(
        "INFO-level log, from the app's own loggers only — no uvicorn or "
        "scheduler noise. This is the narration of what the jobs did, and the "
        "place to look when nothing errored but the outcome was wrong.",
        "Pruned on INFO_LOG_LIFETIME, the shortest window of any table.",
        "the logging handlers (app/services/alerts.py)",
        "created_at",
    ),
}


def _mapped_tables() -> dict[str, list[dict[str, str]]]:
    """Column metadata straight from the ORM, so it cannot drift.

    Deriving the column list rather than writing it down is the whole reason
    autocomplete can be trusted: a column renamed in `models.py` is renamed
    here in the same commit, with no second place to forget.
    """
    out: dict[str, list[dict[str, str]]] = {}
    for name, table in Base.metadata.tables.items():
        cols = []
        for col in table.columns:
            if col.name in BLOCKED_COLUMNS:
                continue
            cols.append({
                "name": col.name,
                "type": str(col.type),
                "nullable": col.nullable,
                "primary_key": col.primary_key,
                "references": (
                    next(iter(col.foreign_keys)).target_fullname
                    if col.foreign_keys else None
                ),
            })
        out[name] = cols
    return out


def allowed_tables() -> frozenset[str]:
    return frozenset(Base.metadata.tables.keys())


def _allowed_columns() -> frozenset[str]:
    """Every column name in the schema.

    Used to let `EXTRACT(EPOCH FROM finished_at)` through the table check:
    the scanner sees `FROM finished_at` and would otherwise reject a
    perfectly good query for referencing a table that doesn't exist.
    """
    return frozenset(
        col.name
        for table in Base.metadata.tables.values()
        for col in table.columns
    )


def schema_catalogue() -> dict:
    """Everything the console needs to help someone write a query.

    Served rather than duplicated in the frontend: autocomplete, the schema
    sidebar and the developer panel all read this, so there is one answer to
    "what columns does this table have" and it comes from the ORM.
    """
    cols = _mapped_tables()
    tables = []
    for name in sorted(cols):
        note = TABLE_NOTES.get(name)
        tables.append({
            "table": name,
            "purpose": note.purpose if note else "",
            "lifecycle": note.lifecycle if note else "",
            "written_by": note.written_by if note else "",
            "time_column": note.time_column if note else None,
            "columns": cols[name],
        })
    settings = get_settings()
    return {
        "tables": tables,
        "blocked_columns": sorted(BLOCKED_COLUMNS),
        # Served here as well as on a result, so the console can show the
        # real ceiling before the first query instead of guessing at one.
        "limits": {
            "max_rows": settings.data_lens_max_rows,
            "timeout_ms": settings.data_lens_timeout_ms,
        },
        # Whether "explain this result" can work, so the console can say it is
        # switched off instead of offering a button whose first press is an
        # error. `ai_rows` is stated too, because a reader of a summary should
        # know how much of the result the AI was actually shown.
        "ai": {
            "enabled": settings.data_lens_ai_enabled,
            "rows": settings.data_lens_ai_max_rows,
        },
        "note": (
            "Read-only. Every query runs in a transaction the database itself "
            "refuses writes in, and is rolled back afterwards."
        ),
    }


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@dataclass
class QueryPlan:
    """A validated query, plus what validation learned about it."""

    sql: str
    tables: list[str] = field(default_factory=list)
    limit_added: bool = False


def _strip_noise(sql: str) -> str:
    """Remove comments and single-quoted strings.

    Keyword scanning has to happen on this, not the raw text, or
    `SELECT 'drop table users'` trips every alarm. Double-quoted text is
    deliberately left in place: in SQL that is an *identifier*, so
    `"token_hash"` must still be caught.
    """
    out = _BLOCK_COMMENT.sub(" ", sql)
    out = _LINE_COMMENT.sub(" ", out)
    out = _SINGLE_QUOTED.sub("''", out)
    return out


def _did_you_mean(name: str, candidates) -> str:
    close = difflib.get_close_matches(name, sorted(candidates), n=1, cutoff=0.6)
    return f" Did you mean `{close[0]}`?" if close else ""


def validate(sql: str, *, limit: int) -> QueryPlan:
    """Check a query and return the statement to run, or raise QueryRejected.

    Order matters: the cheapest and most likely mistake is checked first so
    the admin gets the most relevant sentence rather than the first rule that
    happens to fire.
    """
    raw = (sql or "").strip().rstrip(";").strip()
    if not raw:
        raise QueryRejected(
            "The query is empty.",
            "Pick one of the saved queries to start from, or type a SELECT.",
        )

    scan = _strip_noise(raw)

    # Dollar quoting is Postgres's other string syntax, and it would hide a
    # whole statement from the scanner above. Nothing a read-only question
    # needs uses `$`, so refusing it outright is cheaper than parsing it.
    if "$" in scan:
        raise QueryRejected(
            "`$` is not allowed in this console.",
            "Dollar-quoted strings and parameter placeholders aren't needed "
            "here — write values inline as 'text' or a number.",
        )

    if ";" in scan:
        raise QueryRejected(
            "Only one statement at a time.",
            "Remove the `;` in the middle. A single SELECT, no semicolons.",
        )

    words = [w.lower() for w in _WORD.findall(scan)]
    if not words:
        raise QueryRejected("That doesn't look like SQL.", "Start with SELECT.")

    if words[0] not in ("select", "with"):
        raise QueryRejected(
            f"A query here must start with SELECT or WITH — this one starts "
            f"with `{words[0].upper()}`.",
            "This console can only read. Nothing typed here can change "
            "production data, so write the question as a SELECT.",
        )

    hit = next((w for w in words if w in FORBIDDEN_WORDS), None)
    if hit:
        raise QueryRejected(
            f"`{hit.upper()}` is not allowed in this console.",
            "Only reading is possible here — no statement that writes, "
            "changes the schema or touches server settings will run.",
        )

    for w in words:
        if w in FORBIDDEN_IDENTIFIERS or w.startswith(FORBIDDEN_PREFIXES):
            raise QueryRejected(
                f"`{w}` reaches outside this application's own tables.",
                "The system catalogue and server-internal functions are off "
                "limits. Use the schema sidebar to see what is queryable.",
            )

    # Identifiers, with double quotes removed so `"token_hash"` is seen for
    # what it is. Checked against the blocked set in every position, not just
    # the select list: `substr(token_hash, 1, 8)` and `token_hash AS x` are
    # both ways of asking for the same thing.
    unquoted = scan.replace('"', " ")
    for w in (w.lower() for w in _WORD.findall(unquoted)):
        if w in BLOCKED_COLUMNS:
            raise QueryRejected(
                f"`{w}` is a credential column and is never served.",
                "Nothing is diagnosable from a token hash. For a login or "
                "session question use `created_at`, `expires_at`, "
                "`consumed_at` and `revoked_at` instead.",
            )

    # Table references. CTE names and column names are accepted alongside the
    # real tables — the first because `FROM my_cte` is legitimate, the second
    # because `EXTRACT(EPOCH FROM finished_at)` looks identical to a table
    # reference from here.
    tables = allowed_tables()
    known = tables | _allowed_columns() | {
        m.group(1).lower() for m in _CTE_NAME.finditer(scan)
    }
    seen: list[str] = []
    for m in _FROM_JOIN.finditer(scan):
        ref = re.sub(r"\s+", "", m.group(1))
        if "." in ref:
            schema, _, ref = ref.partition(".")
            if schema.lower() != "public":
                raise QueryRejected(
                    f"`{schema}` is not a schema you can query.",
                    "Leave the schema off, or write `public.` — the "
                    "application's tables all live there.",
                )
        low = ref.lower()
        if low not in known:
            raise QueryRejected(
                f"`{ref}` is not a table in this database.{_did_you_mean(low, tables)}",
                "The schema sidebar lists every table, what a row in it "
                "means, and whether it keeps history.",
            )
        if low in tables and low not in seen:
            seen.append(low)

    # A LIMIT helps the database plan the query; it is not what bounds the
    # result. That is done when the cursor is read, so a query whose own
    # LIMIT is enormous is still capped.
    #
    # `limit + 1`, not `limit`: the extra row is the probe that tells the
    # reader whether anything was left behind. Injecting a bare `LIMIT
    # limit` would make the cap invisible — the result would arrive exactly
    # full every time, and "there is more, narrow it down" could never be
    # distinguished from "that is all of it".
    limit_added = "limit" not in words
    final = f"{raw}\nLIMIT {limit + 1}" if limit_added else raw
    return QueryPlan(sql=final, tables=seen, limit_added=limit_added)


# ---------------------------------------------------------------------------
# Masking
# ---------------------------------------------------------------------------


def _mask_email(match: re.Match[str]) -> str:
    local, _, domain = match.group(0).partition("@")
    return f"{local[:2]}{'*' * max(3, len(local) - 2)}@{domain}"


def _scrub(value: str) -> str:
    return _EMAIL.sub(_mask_email, value)


def _cell(value, *, mask: bool):
    """One result value, JSON-safe, labelled and bounded.

    Timestamps are the awkward part. Everywhere else in this application a
    datetime arrives through `UTCDateTime`, which attaches `tzinfo=UTC` on
    read so a timestamp cannot leave unlabelled. A textual query bypasses
    that: SQLAlchemy has no type information for `SELECT created_at`, so the
    driver's raw value comes through — a *naive* datetime on Postgres, and
    the stored *string* on SQLite.

    Both are relabelled here, at the same boundary and by the same
    convention the column type uses ("a naive one is trusted as UTC
    already"). The SQLite string is sniffed against an anchored pattern
    matching only the format SQLAlchemy writes for a DateTime column; a text
    column would have to hold exactly that shape to be caught, and if it
    does, it is a timestamp. Doing this for both backends rather than only
    for Postgres is deliberate: a console that labels times in production
    but not in development teaches the wrong habit in the place the admin
    does their learning.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    if isinstance(value, date_):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    if isinstance(value, str):
        if _SQLITE_TIMESTAMP.match(value):
            return f"{value.replace(' ', 'T')}+00:00"
        out = _scrub(value) if mask else value
        if len(out) > _MAX_CELL_CHARS:
            return f"{out[:_MAX_CELL_CHARS]}… ({len(out)} chars total)"
        return out
    if isinstance(value, (int, float, bool)):
        return value
    return str(value)


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list]
    row_count: int
    truncated: bool
    duration_ms: int
    sql: str
    tables: list[str]
    masked: bool
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "columns": self.columns,
            "rows": self.rows,
            "row_count": self.row_count,
            "truncated": self.truncated,
            "duration_ms": self.duration_ms,
            "sql": self.sql,
            "tables": self.tables,
            "masked": self.masked,
            "notes": self.notes,
        }


def _db_error_message(exc: SQLAlchemyError) -> str:
    """The database's own complaint, trimmed.

    Passed through rather than replaced: "column reference is ambiguous" and
    "column dc.symbol does not exist" are the two most useful teaching
    messages in the whole system, and nothing written here would beat them.
    """
    orig = getattr(exc, "orig", None)
    msg = str(orig or exc).strip()
    first = msg.split("\n")[0]
    return first[:400]


def run_query(sql: str, *, actor: str, limit: int | None = None) -> QueryResult:
    """Validate and run one read-only query.

    `actor` goes into the audit line, so there is a record of who asked what
    even though nothing can be changed from here.
    """
    settings = get_settings()
    if not settings.data_lens_enabled:
        raise QueryRejected(
            "The query console is switched off.",
            "Set DATA_LENS_ENABLED=true to turn it on.",
        )

    cap = settings.data_lens_max_rows
    want = cap if limit is None else max(1, min(limit, cap))
    plan = validate(sql, limit=want)
    mask = not settings.data_lens_show_emails

    from app.db.database import get_engine

    engine = get_engine()
    started = time.monotonic()
    conn = engine.connect()
    try:
        if conn.dialect.name == "postgresql":
            # The real guarantee. This must be the first statement in the
            # transaction, which it is: SQLAlchemy emits BEGIN immediately
            # before it. After this the server rejects any write, whatever
            # the validator above missed.
            conn.execute(text("SET TRANSACTION READ ONLY"))
            conn.execute(
                text(f"SET LOCAL statement_timeout = '{settings.data_lens_timeout_ms}ms'")
            )
        try:
            cursor = conn.execute(text(plan.sql))
        except SQLAlchemyError as e:
            log.warning(
                "Data Lens query failed for %s: %s", actor, _db_error_message(e)
            )
            raise QueryRejected(
                _db_error_message(e),
                "The database rejected the query. Check the column names in "
                "the schema sidebar — most of these are a typo or a column "
                "that lives on a different table.",
            ) from None

        columns = list(cursor.keys())
        # One more than the cap, so "there was more" is known rather than
        # guessed from a result that happens to be exactly `want` long.
        fetched = cursor.fetchmany(want + 1)
    finally:
        # Every path, including an exception: nothing from this console is
        # ever committed.
        conn.rollback()
        conn.close()

    truncated = len(fetched) > want
    kept = fetched[:want]
    duration_ms = int((time.monotonic() - started) * 1000)

    notes: list[str] = []
    if plan.limit_added:
        notes.append(f"No LIMIT was given, so LIMIT {want} was added.")
    if truncated:
        notes.append(
            f"More than {want} rows matched. Showing the first {want} — add a "
            f"WHERE or a GROUP BY to narrow it."
        )
    if mask:
        notes.append(
            "Email addresses are masked. Set DATA_LENS_SHOW_EMAILS=true to "
            "see them in full."
        )

    # Dropped rather than returned if a blocked column arrived anyway — via
    # `SELECT *`, which names nothing the validator could object to.
    drop = {i for i, c in enumerate(columns) if c.lower() in BLOCKED_COLUMNS}
    if drop:
        notes.append(
            f"Hidden from the result: "
            f"{', '.join(columns[i] for i in sorted(drop))} (credential columns)."
        )
        columns = [c for i, c in enumerate(columns) if i not in drop]

    rows = [
        [_cell(v, mask=mask) for i, v in enumerate(row) if i not in drop]
        for row in kept
    ]

    log.info(
        "Data Lens: %s ran a query over %s — %d row(s) in %dms",
        actor, ",".join(plan.tables) or "no table", len(rows), duration_ms,
    )
    return QueryResult(
        columns=columns,
        rows=rows,
        row_count=len(rows),
        truncated=truncated,
        duration_ms=duration_ms,
        sql=plan.sql,
        tables=plan.tables,
        masked=mask,
        notes=notes,
    )
