"""Tests for the parent feedback feature.

Covers:
- ContentFeedback model and DB constraints
- Signed token generation and one-click rating
- Authenticated CRUD (PUT, GET)
- Upsert semantics (change rating, preserve context)
- Email feedback link generation (text + HTML)
- RAG-ready context_json storage
- Invalid / expired tokens
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from app.api.feedback_routes import (
    FEEDBACK_PURPOSE,
    FEEDBACK_TTL_SECONDS,
    VALID_CONTENT_TYPES,
    VALID_RATINGS,
    feedback_links_html,
    feedback_links_text,
    feedback_url,
)
from app.core.security import issue_token
from app.db.database import get_session_factory
from app.models import ContentFeedback, User


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_user(db, email="parent@test.com"):
    u = User(email=email, notify_email=email, notify_email_confirmed=True)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _auth(client, email="parent@test.com"):
    """Sign in via the magic-link flow and return (user, headers)."""
    client.post("/auth/request-link", json={"email": email})
    body = client.sent_emails[-1]["body_text"]
    token = body.split("token=")[1].split("\n")[0].strip()
    r = client.post("/auth/verify", json={"token": token})
    data = r.json()
    user = data["user"]
    headers = {"Authorization": f"Bearer {data['session_token']}"}
    return user, headers


# ---------------------------------------------------------------------------
# Model / DB
# ---------------------------------------------------------------------------

class TestContentFeedbackModel:

    def test_create_feedback(self, db_session):
        u = _make_user(db_session)
        fb = ContentFeedback(
            user_id=u.id, content_type="money_idea",
            content_id="company:2026-10-30", rating="like",
            age_group="6-9",
            context_json=json.dumps({"idea_key": "company", "position": 1}),
        )
        db_session.add(fb)
        db_session.commit()
        assert fb.id is not None
        assert fb.rating == "like"

    def test_unique_constraint(self, db_session):
        u = _make_user(db_session)
        fb1 = ContentFeedback(
            user_id=u.id, content_type="article",
            content_id="42", rating="like",
        )
        db_session.add(fb1)
        db_session.commit()
        fb2 = ContentFeedback(
            user_id=u.id, content_type="article",
            content_id="42", rating="dislike",
        )
        db_session.add(fb2)
        with pytest.raises(Exception):
            db_session.commit()
        db_session.rollback()

    def test_different_content_ids_allowed(self, db_session):
        u = _make_user(db_session)
        db_session.add(ContentFeedback(
            user_id=u.id, content_type="money_idea",
            content_id="company:2026-10-30", rating="like",
        ))
        db_session.add(ContentFeedback(
            user_id=u.id, content_type="money_idea",
            content_id="share:2026-10-31", rating="neutral",
        ))
        db_session.commit()

    def test_context_json_stores_rag_metadata(self, db_session):
        u = _make_user(db_session)
        ctx = {"ticker": "AAPL", "move_pct": -2.3, "direction": "down", "trading_date": "2026-10-30"}
        fb = ContentFeedback(
            user_id=u.id, content_type="article",
            content_id="99", rating="like",
            age_group="10-12", context_json=json.dumps(ctx),
        )
        db_session.add(fb)
        db_session.commit()
        loaded = json.loads(fb.context_json)
        assert loaded["ticker"] == "AAPL"
        assert loaded["move_pct"] == -2.3


# ---------------------------------------------------------------------------
# Token-based rating (email one-click)
# ---------------------------------------------------------------------------

class TestTokenRating:

    def _user_id(self, client):
        """Create a user via the auth flow and return their id."""
        user, _ = _auth(client)
        return user["id"]

    def test_rate_from_valid_token(self, client):
        uid = self._user_id(client)
        token = issue_token(FEEDBACK_PURPOSE, {
            "u": uid, "ct": "money_idea", "cid": "company:2026-10-30", "r": "like",
            "ag": "6-9", "ctx": {"idea_key": "company", "position": 1},
        })
        resp = client.post("/feedback/rate", json={"token": token})
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["rating"] == "like"
        assert data["content_type"] == "money_idea"

    def test_rate_creates_db_row(self, client):
        uid = self._user_id(client)
        token = issue_token(FEEDBACK_PURPOSE, {
            "u": uid, "ct": "article", "cid": "42", "r": "dislike",
            "ag": "13-17", "ctx": {"ticker": "TSLA", "move_pct": 5.1},
        })
        client.post("/feedback/rate", json={"token": token})
        db = get_session_factory()()
        try:
            row = db.query(ContentFeedback).filter_by(
                user_id=uid, content_type="article", content_id="42"
            ).one()
            assert row.rating == "dislike"
            assert row.age_group == "13-17"
            assert json.loads(row.context_json)["ticker"] == "TSLA"
        finally:
            db.close()

    def test_rate_same_content_twice_updates(self, client):
        uid = self._user_id(client)
        for rating in ("like", "neutral"):
            token = issue_token(FEEDBACK_PURPOSE, {
                "u": uid, "ct": "money_idea", "cid": "share:2026-10-30", "r": rating,
            })
            resp = client.post("/feedback/rate", json={"token": token})
            assert resp.status_code == 200
        db = get_session_factory()()
        try:
            rows = db.query(ContentFeedback).filter_by(
                user_id=uid, content_type="money_idea", content_id="share:2026-10-30"
            ).all()
            assert len(rows) == 1
            assert rows[0].rating == "neutral"
        finally:
            db.close()

    def test_invalid_token_rejected(self, client):
        resp = client.post("/feedback/rate", json={"token": "garbage"})
        assert resp.status_code == 400

    def test_expired_token_wrong_secret(self, client):
        from itsdangerous import URLSafeTimedSerializer
        s = URLSafeTimedSerializer("wrong-secret", salt="ai-stock-watcher::feedback")
        token = s.dumps({"u": 1, "ct": "money_idea", "cid": "x", "r": "like"})
        resp = client.post("/feedback/rate", json={"token": token})
        assert resp.status_code == 400

    def test_invalid_rating_rejected(self, client):
        uid = self._user_id(client)
        token = issue_token(FEEDBACK_PURPOSE, {
            "u": uid, "ct": "money_idea", "cid": "x", "r": "love",
        })
        resp = client.post("/feedback/rate", json={"token": token})
        assert resp.status_code == 400

    def test_invalid_content_type_rejected(self, client):
        uid = self._user_id(client)
        token = issue_token(FEEDBACK_PURPOSE, {
            "u": uid, "ct": "unknown", "cid": "x", "r": "like",
        })
        resp = client.post("/feedback/rate", json={"token": token})
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Authenticated endpoints (frontend)
# ---------------------------------------------------------------------------

class TestAuthenticatedFeedback:

    def test_put_feedback(self, client):
        _, headers = _auth(client)
        resp = client.put("/feedback", json={
            "content_type": "money_idea", "content_id": "company:2026-10-30", "rating": "like",
        }, headers=headers)
        assert resp.status_code == 200
        assert resp.json()["rating"] == "like"

    def test_put_updates_existing(self, client):
        _, headers = _auth(client)
        client.put("/feedback", json={
            "content_type": "money_idea", "content_id": "company:2026-10-30", "rating": "like",
        }, headers=headers)
        resp = client.put("/feedback", json={
            "content_type": "money_idea", "content_id": "company:2026-10-30", "rating": "dislike",
        }, headers=headers)
        assert resp.status_code == 200
        assert resp.json()["rating"] == "dislike"

    def test_list_feedback_empty(self, client):
        _, headers = _auth(client)
        resp = client.get("/feedback", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["feedback"] == []

    def test_list_feedback_with_data(self, client):
        _, headers = _auth(client)
        client.put("/feedback", json={
            "content_type": "money_idea", "content_id": "a:2026-10-30", "rating": "like",
        }, headers=headers)
        client.put("/feedback", json={
            "content_type": "article", "content_id": "99", "rating": "neutral",
        }, headers=headers)
        resp = client.get("/feedback", headers=headers)
        assert len(resp.json()["feedback"]) == 2

    def test_list_feedback_filter_by_type(self, client):
        _, headers = _auth(client)
        client.put("/feedback", json={
            "content_type": "money_idea", "content_id": "a:2026-10-30", "rating": "like",
        }, headers=headers)
        client.put("/feedback", json={
            "content_type": "article", "content_id": "99", "rating": "neutral",
        }, headers=headers)
        resp = client.get("/feedback?content_type=money_idea", headers=headers)
        items = resp.json()["feedback"]
        assert len(items) == 1
        assert items[0]["content_type"] == "money_idea"

    def test_put_invalid_rating(self, client):
        _, headers = _auth(client)
        resp = client.put("/feedback", json={
            "content_type": "money_idea", "content_id": "x", "rating": "amazing",
        }, headers=headers)
        assert resp.status_code == 400

    def test_put_requires_auth(self, client):
        resp = client.put("/feedback", json={
            "content_type": "money_idea", "content_id": "x", "rating": "like",
        })
        assert resp.status_code == 401

    def test_get_requires_auth(self, client):
        resp = client.get("/feedback")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Email feedback link helpers
# ---------------------------------------------------------------------------

class TestFeedbackLinks:

    def test_feedback_url_contains_token(self):
        url = feedback_url(1, "money_idea", "company:2026-10-30", "like")
        assert "/feedback?token=" in url

    def test_feedback_url_includes_frontend_base(self):
        url = feedback_url(1, "article", "42", "dislike")
        assert url.startswith("http")

    def test_feedback_links_text_has_three_options(self):
        text = feedback_links_text(1, "money_idea", "company:2026-10-30")
        assert "Love it" in text
        assert "So-so" in text
        assert "Not great" in text

    def test_feedback_links_html_has_three_buttons(self):
        html = feedback_links_html(1, "money_idea", "company:2026-10-30")
        assert html.count("<a href=") == 3
        assert "Love it" in html
        assert "So-so" in html
        assert "Not great" in html

    def test_feedback_links_with_context(self):
        ctx = {"ticker": "AAPL", "move_pct": -2.3}
        url = feedback_url(
            1, "article", "42", "like",
            age_group="10-12", context=ctx,
        )
        assert "/feedback?token=" in url


# ---------------------------------------------------------------------------
# RAG readiness
# ---------------------------------------------------------------------------

class TestRAGReadiness:

    def _user_id(self, client):
        user, _ = _auth(client)
        return user["id"]

    def test_article_feedback_stores_ticker_context(self, client):
        uid = self._user_id(client)
        token = issue_token(FEEDBACK_PURPOSE, {
            "u": uid, "ct": "article", "cid": "42", "r": "like",
            "ag": "6-9",
            "ctx": {"ticker": "MSFT", "move_pct": 3.2, "direction": "up", "trading_date": "2026-10-30"},
        })
        client.post("/feedback/rate", json={"token": token})
        db = get_session_factory()()
        try:
            row = db.query(ContentFeedback).filter_by(content_id="42").one()
            ctx = json.loads(row.context_json)
            assert ctx["ticker"] == "MSFT"
            assert ctx["direction"] == "up"
            assert ctx["trading_date"] == "2026-10-30"
        finally:
            db.close()

    def test_idea_feedback_stores_position_context(self, client):
        uid = self._user_id(client)
        token = issue_token(FEEDBACK_PURPOSE, {
            "u": uid, "ct": "money_idea", "cid": "company:2026-10-30", "r": "like",
            "ag": "3-5",
            "ctx": {"idea_key": "company", "position": 1},
        })
        client.post("/feedback/rate", json={"token": token})
        db = get_session_factory()()
        try:
            row = db.query(ContentFeedback).filter_by(content_id="company:2026-10-30").one()
            ctx = json.loads(row.context_json)
            assert ctx["idea_key"] == "company"
            assert ctx["position"] == 1
            assert row.age_group == "3-5"
        finally:
            db.close()

    def test_context_not_overwritten_on_rating_change(self, client):
        uid = self._user_id(client)
        token1 = issue_token(FEEDBACK_PURPOSE, {
            "u": uid, "ct": "article", "cid": "42", "r": "like",
            "ctx": {"ticker": "AAPL"},
        })
        client.post("/feedback/rate", json={"token": token1})
        token2 = issue_token(FEEDBACK_PURPOSE, {
            "u": uid, "ct": "article", "cid": "42", "r": "dislike",
        })
        client.post("/feedback/rate", json={"token": token2})
        db = get_session_factory()()
        try:
            row = db.query(ContentFeedback).filter_by(content_id="42").one()
            assert row.rating == "dislike"
            assert json.loads(row.context_json)["ticker"] == "AAPL"
        finally:
            db.close()

    def test_feedback_has_all_rag_fields(self, client):
        user, headers = _auth(client)
        uid = user["id"]
        token = issue_token(FEEDBACK_PURPOSE, {
            "u": uid, "ct": "article", "cid": "100", "r": "like",
            "ag": "10-12",
            "ctx": {"ticker": "GOOG", "move_pct": 1.5, "direction": "up", "trading_date": "2026-10-30"},
        })
        client.post("/feedback/rate", json={"token": token})
        resp = client.get("/feedback?content_type=article", headers=headers)
        items = resp.json()["feedback"]
        assert len(items) == 1
        assert items[0]["age_group"] == "10-12"
        assert items[0]["content_id"] == "100"
        assert items[0]["rating"] == "like"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class TestValidation:

    def test_valid_ratings(self):
        assert VALID_RATINGS == {"like", "neutral", "dislike"}

    def test_valid_content_types(self):
        assert VALID_CONTENT_TYPES == {"article", "money_idea"}
