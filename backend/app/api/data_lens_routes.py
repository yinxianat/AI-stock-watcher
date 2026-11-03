"""Data Lens — the admin's read-only query console.

Human admins only. Deliberately NOT reachable with the monitoring agent's
service token and never listed in `admin_routes.SERVICE_SAFE_PATHS`: the
whole point of that allow-list is that every path on it is free of personal
data, and a console that runs arbitrary SELECTs can't promise that about
itself. The agent gets shaped, pseudonymised reports; a person gets SQL.

Everything here is served from `app.services.data_lens` and
`app.services.query_library`, so the console, the schema sidebar and the
developer panel cannot disagree about what the database looks like.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import current_admin
from app.core.ratelimit import RateLimit
from app.core.settings import get_settings
from app.models import User
from app.services import data_lens, query_library

log = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/data-lens", tags=["admin"])


class QueryRequest(BaseModel):
    """A query to run.

    The SQL arrives in the body, never a query string: query strings are
    written to proxy and access logs, and a SELECT naming a user's email
    address does not belong in one.
    """

    sql: str = Field(min_length=1, max_length=20_000)
    limit: int | None = Field(default=None, ge=1, le=5_000)


@router.get("/schema")
def get_schema(admin: User = Depends(current_admin)) -> dict:
    """Every queryable table, its columns, and what a row in it means.

    This is what powers autocomplete and the schema sidebar. Columns come
    from the ORM rather than a hand-written list, so they cannot drift from
    the real schema; the prose comes from the catalogue in `data_lens`.
    """
    return data_lens.schema_catalogue()


@router.get("/queries")
def get_queries(admin: User = Depends(current_admin)) -> dict:
    """The saved queries, grouped, each with the SQL idea it demonstrates."""
    return query_library.catalogue()


@router.post(
    "/query",
    dependencies=[Depends(RateLimit("120/hour", "data_lens_query"))],
)
def post_query(payload: QueryRequest, admin: User = Depends(current_admin)) -> dict:
    """Run one read-only SELECT and return the rows.

    A rejected query comes back as 422 with both what was wrong and what to
    do instead — the console shows the hint, because an error message that
    only says "no" teaches the admin to stop asking.
    """
    try:
        result = data_lens.run_query(
            payload.sql, actor=f"user:{admin.id}", limit=payload.limit,
        )
    except data_lens.QueryRejected as e:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": e.message, "hint": e.hint},
        ) from None

    out = result.as_dict()
    # The same convention the rest of the monitoring payloads carry: say what
    # the timestamps in this result mean rather than leaving the reader to
    # assume. Every datetime above is ISO-8601 with an explicit offset.
    out["timezone"] = _timezone_note()
    out["limits"] = {
        "max_rows": get_settings().data_lens_max_rows,
        "timeout_ms": get_settings().data_lens_timeout_ms,
    }
    return out


def _timezone_note() -> dict:
    from app.services.diagnostics import TIMEZONE_NOTE

    return TIMEZONE_NOTE
