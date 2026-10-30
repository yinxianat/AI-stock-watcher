"""Feedback API: one-click rating from emails + CRUD for the frontend.

Parents rate AI stories and Money Ideas as like / neutral / dislike.
Email links carry a signed token so rating works without logging in.
The frontend uses the authenticated endpoints to view and change ratings.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app.api.deps import current_user
from app.core.security import issue_token, verify_token
from app.core.settings import get_settings
from app.db.database import get_db
from app.models import ContentFeedback, StockArticle, User, utcnow

log = logging.getLogger(__name__)

router = APIRouter(prefix="/feedback", tags=["feedback"])

FEEDBACK_PURPOSE = "feedback"
FEEDBACK_TTL_SECONDS = 90 * 24 * 3600  # 90 days — emails sit in inboxes

VALID_RATINGS = {"like", "neutral", "dislike"}
VALID_CONTENT_TYPES = {"article", "money_idea"}


# ---------------------------------------------------------------------------
# Token helpers (used by email-rendering code)
# ---------------------------------------------------------------------------

def feedback_url(
    user_id: int,
    content_type: str,
    content_id: str,
    rating: str,
    age_group: str | None = None,
    context: dict | None = None,
) -> str:
    """Build a one-click feedback URL for an email."""
    payload = {
        "u": user_id,
        "ct": content_type,
        "cid": content_id,
        "r": rating,
    }
    if age_group:
        payload["ag"] = age_group
    if context:
        payload["ctx"] = context
    token = issue_token(FEEDBACK_PURPOSE, payload)
    return f"{get_settings().frontend_base_url}/feedback?token={token}"


def feedback_links_text(
    user_id: int,
    content_type: str,
    content_id: str,
    age_group: str | None = None,
    context: dict | None = None,
) -> str:
    """Plain-text feedback line for emails."""
    like = feedback_url(user_id, content_type, content_id, "like", age_group, context)
    neutral = feedback_url(user_id, content_type, content_id, "neutral", age_group, context)
    dislike = feedback_url(user_id, content_type, content_id, "dislike", age_group, context)
    return f"Rate this: 👍 Love it {like}  😐 So-so {neutral}  👎 Not great {dislike}"


def feedback_links_html(
    user_id: int,
    content_type: str,
    content_id: str,
    age_group: str | None = None,
    context: dict | None = None,
) -> str:
    """HTML feedback buttons for emails."""
    btn = (
        'display:inline-block;padding:6px 14px;border-radius:6px;'
        'text-decoration:none;font-size:14px;margin:0 4px'
    )
    like = feedback_url(user_id, content_type, content_id, "like", age_group, context)
    neutral = feedback_url(user_id, content_type, content_id, "neutral", age_group, context)
    dislike = feedback_url(user_id, content_type, content_id, "dislike", age_group, context)
    return (
        '<div style="text-align:center;margin:12px 0 4px">'
        '<span style="font-size:13px;color:#6e7781">Was this helpful?</span><br>'
        f'<a href="{like}" style="{btn};background:#dafbe1;color:#1a7f37">👍 Love it</a>'
        f'<a href="{neutral}" style="{btn};background:#fff8c5;color:#9a6700">😐 So-so</a>'
        f'<a href="{dislike}" style="{btn};background:#ffebe9;color:#cf222e">👎 Not great</a>'
        '</div>'
    )


# ---------------------------------------------------------------------------
# One-click endpoint (from email token, no login required)
# ---------------------------------------------------------------------------

class FeedbackTokenRequest(BaseModel):
    token: str


class FeedbackResponse(BaseModel):
    ok: bool
    rating: str
    content_type: str
    content_id: str


@router.post("/rate", response_model=FeedbackResponse)
def rate_from_token(
    body: FeedbackTokenRequest,
    db: DBSession = Depends(get_db),
):
    """Record feedback from a signed email link. No login required."""
    payload = verify_token(FEEDBACK_PURPOSE, body.token, FEEDBACK_TTL_SECONDS)
    if not payload:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired feedback link")

    user_id = payload["u"]
    content_type = payload["ct"]
    content_id = str(payload["cid"])
    rating = payload["r"]
    age_group = payload.get("ag")
    context = payload.get("ctx")

    if rating not in VALID_RATINGS or content_type not in VALID_CONTENT_TYPES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid rating or content type")

    _upsert(db, user_id, content_type, content_id, rating, age_group, context)
    return FeedbackResponse(ok=True, rating=rating, content_type=content_type, content_id=content_id)


# ---------------------------------------------------------------------------
# Authenticated endpoints (frontend)
# ---------------------------------------------------------------------------

class FeedbackPutRequest(BaseModel):
    content_type: str
    content_id: str
    rating: str


@router.put("", response_model=FeedbackResponse)
def update_feedback(
    body: FeedbackPutRequest,
    user: User = Depends(current_user),
    db: DBSession = Depends(get_db),
):
    """Create or update a rating (authenticated)."""
    if body.rating not in VALID_RATINGS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"rating must be one of {VALID_RATINGS}")
    if body.content_type not in VALID_CONTENT_TYPES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"content_type must be one of {VALID_CONTENT_TYPES}")
    _upsert(db, user.id, body.content_type, body.content_id, body.rating)
    return FeedbackResponse(ok=True, rating=body.rating, content_type=body.content_type, content_id=body.content_id)


class FeedbackItem(BaseModel):
    content_type: str
    content_id: str
    rating: str
    age_group: str | None = None
    created_at: str
    updated_at: str


class FeedbackListResponse(BaseModel):
    feedback: list[FeedbackItem]


@router.get("", response_model=FeedbackListResponse)
def list_feedback(
    content_type: str | None = Query(None),
    user: User = Depends(current_user),
    db: DBSession = Depends(get_db),
):
    """List the current user's feedback, optionally filtered by content_type."""
    q = select(ContentFeedback).where(ContentFeedback.user_id == user.id)
    if content_type:
        q = q.where(ContentFeedback.content_type == content_type)
    q = q.order_by(ContentFeedback.updated_at.desc())
    rows = db.execute(q).scalars().all()
    return FeedbackListResponse(feedback=[
        FeedbackItem(
            content_type=r.content_type,
            content_id=r.content_id,
            rating=r.rating,
            age_group=r.age_group,
            created_at=r.created_at.isoformat() + "Z",
            updated_at=r.updated_at.isoformat() + "Z",
        )
        for r in rows
    ])


# ---------------------------------------------------------------------------
# Shared upsert logic
# ---------------------------------------------------------------------------

def _upsert(
    db: DBSession,
    user_id: int,
    content_type: str,
    content_id: str,
    rating: str,
    age_group: str | None = None,
    context: dict | None = None,
) -> ContentFeedback:
    row = db.execute(
        select(ContentFeedback).where(
            ContentFeedback.user_id == user_id,
            ContentFeedback.content_type == content_type,
            ContentFeedback.content_id == content_id,
        )
    ).scalar_one_or_none()

    if row:
        row.rating = rating
        row.updated_at = utcnow()
        if age_group and not row.age_group:
            row.age_group = age_group
        if context and not row.context_json:
            row.context_json = json.dumps(context)
    else:
        row = ContentFeedback(
            user_id=user_id,
            content_type=content_type,
            content_id=content_id,
            rating=rating,
            age_group=age_group,
            context_json=json.dumps(context) if context else None,
        )
        db.add(row)

    db.commit()
    return row
