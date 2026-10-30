"""Tests for the Money Idea of the Day curriculum and integration."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select

from app.models import MoneyIdeaSend, User
from app.services import money_ideas
from app.services.articles.language import language_problems, readability_problems


# ---------------------------------------------------------------------------
# Curriculum quality
# ---------------------------------------------------------------------------

_AGES = ["3-5", "6-9", "10-12", "13-17"]


class TestCurriculum:
    """Every idea in the curriculum is complete, unique and readable."""

    def test_all_keys_unique(self):
        keys = [i.key for i in money_ideas.CURRICULUM]
        assert len(keys) == len(set(keys))

    def test_every_idea_has_four_age_groups(self):
        for idea in money_ideas.CURRICULUM:
            assert set(idea.versions.keys()) == set(_AGES), idea.key

    def test_position_round_trip(self):
        for i, idea in enumerate(money_ideas.CURRICULUM, 1):
            assert money_ideas.position(idea) == i

    @pytest.mark.parametrize(
        "idea", money_ideas.CURRICULUM,
        ids=[i.key for i in money_ideas.CURRICULUM],
    )
    def test_language_passes(self, idea):
        """Every version must pass the same language checks as AI stories."""
        for age, (text, question) in idea.versions.items():
            full = f"{text} {question}"
            problems = language_problems(full, age)
            assert not problems, f"{idea.key}/{age}: {problems}"

    @pytest.mark.parametrize(
        "idea", money_ideas.CURRICULUM,
        ids=[i.key for i in money_ideas.CURRICULUM],
    )
    def test_readability_passes(self, idea):
        for age, (text, question) in idea.versions.items():
            full = f"{text} {question}"
            problems = readability_problems(full, age)
            assert not problems, f"{idea.key}/{age}: {problems}"


# ---------------------------------------------------------------------------
# next_idea progression
# ---------------------------------------------------------------------------


class TestNextIdea:
    def _user(self, db):
        u = User(email="test@example.com", notify_email="test@example.com",
                 notify_email_confirmed=True)
        db.add(u)
        db.flush()
        return u

    def test_starts_with_first_idea(self, db_session):
        u = self._user(db_session)
        idea = money_ideas.next_idea(db_session, u.id, set())
        assert idea.key == money_ideas.CURRICULUM[0].key

    def test_foundation_before_skipping(self, db_session):
        """The first FOUNDATION ideas are sent in order, ignoring tags."""
        u = self._user(db_session)
        for i in range(money_ideas.FOUNDATION):
            idea = money_ideas.next_idea(db_session, u.id, {money_ideas.BIG_MOVE})
            assert idea.key == money_ideas.CURRICULUM[i].key
            money_ideas.record(db_session, u.id, idea, date(2026, 1, 1 + i))
            db_session.flush()

    def test_after_foundation_prefers_tag(self, db_session):
        """Once the foundation is done, the next idea should match a tag
        if any unsent idea has that tag."""
        u = self._user(db_session)
        for i in range(money_ideas.FOUNDATION):
            money_ideas.record(db_session, u.id, money_ideas.CURRICULUM[i],
                               date(2026, 1, 1 + i))
        db_session.flush()
        idea = money_ideas.next_idea(db_session, u.id, {money_ideas.SMALL_DAY})
        assert idea.tag == money_ideas.SMALL_DAY

    def test_no_tag_match_falls_through(self, db_session):
        """Without matching tags, it picks the next in order."""
        u = self._user(db_session)
        for i in range(money_ideas.FOUNDATION):
            money_ideas.record(db_session, u.id, money_ideas.CURRICULUM[i],
                               date(2026, 1, 1 + i))
        db_session.flush()
        # Use a tag that no idea has:
        idea = money_ideas.next_idea(db_session, u.id, {"nonexistent_tag"})
        assert idea.key == money_ideas.CURRICULUM[money_ideas.FOUNDATION].key

    def test_full_cycle_picks_oldest(self, db_session):
        """After the whole curriculum, it picks from the 5 oldest."""
        u = self._user(db_session)
        for i, idea in enumerate(money_ideas.CURRICULUM):
            money_ideas.record(db_session, u.id, idea, date(2026, 1, 1 + i))
        db_session.flush()
        next_one = money_ideas.next_idea(db_session, u.id, set())
        assert next_one.key == money_ideas.CURRICULUM[0].key  # sent on Jan 1

    def test_full_cycle_prefers_tagged_oldest(self, db_session):
        """After the full cycle, prefers a tag match among the 5 oldest."""
        u = self._user(db_session)
        for i, idea in enumerate(money_ideas.CURRICULUM):
            money_ideas.record(db_session, u.id, idea, date(2026, 1, 1 + i))
        db_session.flush()
        # The first 5 sent are ideas 0..4. If one has SMALL_DAY tag, prefer it.
        tagged = [idea for idea in money_ideas.CURRICULUM[:5] if idea.tag == money_ideas.SMALL_DAY]
        if tagged:
            result = money_ideas.next_idea(db_session, u.id, {money_ideas.SMALL_DAY})
            assert result.tag == money_ideas.SMALL_DAY


# ---------------------------------------------------------------------------
# Email rendering
# ---------------------------------------------------------------------------


class TestRendering:
    def test_idea_text_contains_title(self):
        idea = money_ideas.CURRICULUM[0]
        text = money_ideas.idea_text(idea, ["6-9"], "http://example.com/money-ideas")
        assert idea.title in text
        assert "MONEY IDEA OF THE DAY" in text
        assert "http://example.com/money-ideas" in text

    def test_idea_html_contains_title(self):
        idea = money_ideas.CURRICULUM[0]
        html = money_ideas.idea_html(idea, ["6-9"], "http://example.com/money-ideas")
        assert idea.title in html
        assert "money-ideas" in html

    def test_idea_text_multiple_ages(self):
        idea = money_ideas.CURRICULUM[0]
        text = money_ideas.idea_text(idea, ["3-5", "13-17"], "http://x.com/mi")
        assert "3–5" in text or "3-5" in text or idea.versions["3-5"][0] in text
        assert idea.versions["13-17"][0] in text

    def test_defaults_to_6_9_when_no_age(self):
        idea = money_ideas.CURRICULUM[0]
        text = money_ideas.idea_text(idea, [], "http://x.com/mi")
        assert idea.versions["6-9"][0] in text


# ---------------------------------------------------------------------------
# Recap integration
# ---------------------------------------------------------------------------


class TestRecapIntegration:
    """The recap job picks and records a money idea for each user."""

    def test_recap_includes_money_idea(self, client, db_session, monkeypatch):
        from app.db import database as db_mod
        from app.jobs import recap
        from app.models import Base, DailyClose, Ticker, WatchlistItem

        Base.metadata.create_all(bind=db_session.get_bind())

        # Create user
        u = User(email="alice@example.com", notify_email="alice@example.com",
                 notify_email_confirmed=True)
        db_session.add(u)
        db_session.flush()

        # Create ticker with closes
        t = Ticker(symbol="TEST", name="Test Corp", type="stock")
        db_session.add(t)
        db_session.flush()
        db_session.add(WatchlistItem(user_id=u.id, ticker_id=t.id))

        day = date(2026, 10, 28)
        db_session.add(DailyClose(ticker_id=t.id, date=day, close=100.0))
        prev = date(2026, 10, 27)
        db_session.add(DailyClose(ticker_id=t.id, date=prev, close=99.0))
        db_session.commit()

        monkeypatch.setattr("app.services.articles.service.trading_date", lambda: day)
        monkeypatch.setattr("app.services.articles.service.market_move_pct", lambda: 0.5)

        sent = recap.run_watchlist_recap(db=db_session)
        assert sent == 1

        emails = client.sent_emails
        assert any("MONEY IDEA" in e["body_text"] for e in emails)
        # Check that the idea was recorded
        rec = db_session.execute(
            select(MoneyIdeaSend).where(MoneyIdeaSend.user_id == u.id)
        ).scalar_one()
        assert rec.trading_date == day
        assert rec.idea_key == money_ideas.CURRICULUM[0].key


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


class TestMoneyIdeasAPI:
    def test_returns_empty_for_new_user(self, signed_in):
        r = signed_in["client"].get("/money-ideas", headers=signed_in["auth"])
        assert r.status_code == 200
        assert r.json()["ideas"] == []

    def test_returns_recorded_ideas(self, signed_in, db_session, monkeypatch):
        from app.db import database as db_mod

        # Record an idea for this user.
        user_id = signed_in["user"]["id"]
        idea = money_ideas.CURRICULUM[0]
        day = date(2026, 10, 28)
        db_session.add(MoneyIdeaSend(user_id=user_id, idea_key=idea.key, trading_date=day))
        db_session.commit()

        r = signed_in["client"].get("/money-ideas", headers=signed_in["auth"])
        assert r.status_code == 200
        items = r.json()["ideas"]
        assert len(items) == 1
        assert items[0]["key"] == idea.key
        assert items[0]["title"] == idea.title
        assert items[0]["position"] == 1
        assert items[0]["total"] == len(money_ideas.CURRICULUM)
        assert len(items[0]["versions"]) >= 1

    def test_requires_auth_without_token(self, client):
        assert client.get("/money-ideas").status_code == 401


class TestMoneyIdeasEmailLink:
    """Recap emails link here with a signed token, so a parent clicking from
    their inbox doesn't hit the login page."""

    def test_url_carries_token(self, signed_in):
        from app.api.money_ideas_routes import money_ideas_url

        url = money_ideas_url(signed_in["user"]["id"])
        assert "/money-ideas?token=" in url

    def test_token_opens_history_without_login(self, signed_in, db_session):
        from app.api.money_ideas_routes import money_ideas_url

        user_id = signed_in["user"]["id"]
        idea = money_ideas.CURRICULUM[0]
        db_session.add(MoneyIdeaSend(
            user_id=user_id, idea_key=idea.key, trading_date=date(2026, 10, 28),
        ))
        db_session.commit()

        token = money_ideas_url(user_id).split("token=")[1]
        # No Authorization header at all.
        r = signed_in["client"].get(f"/money-ideas?token={token}")
        assert r.status_code == 200
        items = r.json()["ideas"]
        assert len(items) == 1
        assert items[0]["key"] == idea.key

    def test_garbage_token_rejected(self, client):
        r = client.get("/money-ideas?token=not-a-real-token")
        assert r.status_code == 400

    def test_token_from_another_purpose_rejected(self, client, signed_in):
        """A feedback token must not open the history page."""
        from app.api.feedback_routes import FEEDBACK_PURPOSE
        from app.core.security import issue_token

        token = issue_token(FEEDBACK_PURPOSE, {"uid": signed_in["user"]["id"]})
        assert client.get(f"/money-ideas?token={token}").status_code == 400

    def test_recap_email_link_works_without_login(self, client, db_session, monkeypatch):
        """End-to-end: the history link inside a real recap email opens the
        page with no session — the bug a parent hit clicking from Gmail."""
        from app.jobs import recap
        from app.models import Base, DailyClose, Ticker, WatchlistItem

        Base.metadata.create_all(bind=db_session.get_bind())
        u = User(email="alice@example.com", notify_email="alice@example.com",
                 notify_email_confirmed=True)
        db_session.add(u)
        db_session.flush()
        t = Ticker(symbol="TEST", name="Test Corp", type="stock")
        db_session.add(t)
        db_session.flush()
        db_session.add(WatchlistItem(user_id=u.id, ticker_id=t.id))
        day = date(2026, 10, 28)
        db_session.add(DailyClose(ticker_id=t.id, date=day, close=100.0))
        db_session.add(DailyClose(ticker_id=t.id, date=date(2026, 10, 27), close=99.0))
        db_session.commit()

        monkeypatch.setattr("app.services.articles.service.trading_date", lambda: day)
        monkeypatch.setattr("app.services.articles.service.market_move_pct", lambda: 0.5)
        assert recap.run_watchlist_recap(db=db_session) == 1

        body = client.sent_emails[-1]["body_text"]
        assert "/money-ideas?token=" in body
        url = next(w for w in body.split() if "/money-ideas?token=" in w)
        r = client.get(f"/money-ideas?token={url.split('token=')[1]}")
        assert r.status_code == 200
        assert len(r.json()["ideas"]) == 1

    def test_feedback_response_includes_history_link(self, client, signed_in):
        """The thank-you page needs a login-free way onward."""
        from app.api.feedback_routes import FEEDBACK_PURPOSE
        from app.core.security import issue_token

        token = issue_token(FEEDBACK_PURPOSE, {
            "u": signed_in["user"]["id"], "ct": "money_idea",
            "cid": "company:2026-10-30", "r": "like",
        })
        r = client.post("/feedback/rate", json={"token": token})
        assert r.status_code == 200
        assert "/money-ideas?token=" in r.json()["money_ideas_url"]
