"""Root Cause — the admin's diagnostic agent.

Human admins only, and deliberately NOT in `admin_routes.SERVICE_SAFE_PATHS`.
Two reasons: the agent can read application source, and it can run a
read-only query whose shape nobody vetted in advance, so this endpoint can
make none of the promises that allow-list exists to make.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import current_admin
from app.core.ratelimit import RateLimit
from app.core.settings import get_settings
from app.models import User
from app.services import root_cause

log = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/root-cause", tags=["admin"])


class Question(BaseModel):
    """One question about production.

    In the body rather than a query string: questions get long, and a
    question naming a user's symptom does not belong in a proxy log.
    """

    question: str = Field(min_length=5, max_length=2_000)
    # Omitted means the cheapest allowed model, so a question costs the
    # least by default and spending more is a deliberate act.
    model: str | None = None


@router.get("/models")
def get_models(admin: User = Depends(current_admin)) -> dict:
    """Which models may be picked, and what each costs per question."""
    settings = get_settings()
    return {
        "enabled": settings.root_cause_enabled,
        "models": root_cause.models(),
        "note": (
            "A cheap model answers a lookup — a switch that is off, a job "
            "that did not run. Pay for a larger one when the question needs "
            "the code read and reasoned about."
        ),
    }


@router.post(
    "/ask",
    # Tighter than Data Lens: each question is two model calls over a large
    # evidence pack, and the expensive models are on the picker.
    dependencies=[Depends(RateLimit("15/hour", "root_cause_ask"))],
)
def post_ask(payload: Question, admin: User = Depends(current_admin)) -> dict:
    """Answer one question about production, and say how to fix it."""
    try:
        return root_cause.investigate(
            payload.question, actor=f"user:{admin.id}", model=payload.model,
        )
    except root_cause.Unavailable as e:
        # 503, not 422: the question was fine. Something the agent needs is
        # missing, and telling the admin they asked wrongly would send them
        # rewriting a question that was never the problem.
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": e.message, "hint": e.hint},
        ) from None
