"""Admin visibility for AI stories: key checks, setup emails, cost estimates."""

from __future__ import annotations

import pytest

from app.core.settings import Settings, get_settings
from app.services.articles import costs, health, service
from app.services.articles.providers import AIServiceError


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    for k, v in {
        "AI_ARTICLES_ENABLED": "true", "AI_PROVIDER": "gemini", "AI_FALLBACK_PROVIDER": "anthropic",
        "GEMINI_API_KEY": "AIza-secret-gemini", "ANTHROPIC_API_KEY": "sk-ant-secret-claude",
        "AI_API_KEY": "", "FINNHUB_API_KEY": "fh-secret", "AI_ARTICLE_MODEL": "claude-haiku-5-5",
        "AI_NEWS_SOURCE": "finnhub", "AI_GEMINI_FREE_TIER": "true",
        "AI_ARTICLE_MAX_PER_DAY": "30", "AI_ARTICLE_ALWAYS_PCT": "5", "AI_ARTICLE_MAX_SEARCHES": "2",
    }.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    monkeypatch.setattr(service, "_paused_until", {})
    monkeypatch.setattr(service, "_alerted_at", {})


@pytest.fixture
def emails(monkeypatch):
    sent: list[dict] = []

    def capture(event, subject, body, **kw):
        sent.append({"event": event, "subject": subject, "body": body})
    monkeypatch.setattr(service, "notify_admin", capture)
    monkeypatch.setattr(health, "notify_admin", capture)
    return sent


@pytest.fixture
def keys_ok(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(health, "_check_gemini", lambda key, model: calls.append(f"gemini:{model}"))
    monkeypatch.setattr(health, "_check_anthropic", lambda key, model: calls.append(f"anthropic:{model}"))
    monkeypatch.setattr(health, "_check_finnhub", lambda key: calls.append("finnhub"))
    return calls


# ---------------------------------------------------------------------------
# Cost estimates
# ---------------------------------------------------------------------------


def test_cost_per_story_and_per_day():
    s = get_settings()
    assert costs.story_estimate("gemini", s) == 0.0  # free tier
    haiku = costs.story_estimate("anthropic", s)
    assert haiku == pytest.approx(12_000 / 1e6 * 0.10 + 6_000 / 1e6 * 0.50)  # ~$0.0042
    assert costs.max_stories_per_day(s) == 60  # 30, doubled for big moves
    assert costs.max_daily_cost(s) == pytest.approx(60 * haiku)  # all on the backup


def test_paid_and_web_search_estimates():
    paid = Settings(ai_provider="gemini", ai_gemini_free_tier=False)
    assert costs.story_estimate("gemini", paid) == pytest.approx(12_000 / 1e6 * 0.25 + 6_000 / 1e6 * 1.50)
    opus = Settings(ai_provider="anthropic", ai_article_model="claude-opus-5-5",
                    ai_news_source="web_search", ai_article_max_searches=2)
    assert costs.story_estimate("anthropic", opus) == pytest.approx(0.12 + 0.12 + 0.02)
    assert costs.story_estimate("anthropic", Settings(ai_provider="anthropic",
                                                       ai_article_model="claude-mystery-9")) is None


def test_money_format():
    assert costs.money(0) == "$0 (free)"
    assert costs.money(0.0042) == "$0.0042"
    assert costs.money(0.25) == "$0.25"
    assert costs.money(0.026) == "$0.026"
    assert costs.money(None) == "unknown price"


# ---------------------------------------------------------------------------
# Key checks: problems are emailed straight away
# ---------------------------------------------------------------------------


def test_all_keys_ok(keys_ok, emails):
    assert health.check_keys() == {"gemini": "OK", "anthropic": "OK", "finnhub": "OK"}
    assert keys_ok == ["gemini:gemini-3.1-flash-lite", "anthropic:claude-haiku-5-5", "finnhub"]
    assert emails == []


def test_bad_gemini_key_is_emailed_immediately(keys_ok, emails, monkeypatch):
    def bad(key, model):
        raise AIServiceError("Gemini", 400, "API key not valid. Please pass a valid API key.")
    monkeypatch.setattr(health, "_check_gemini", bad)

    statuses = health.check_keys()

    assert statuses["gemini"].startswith("PROBLEM: the Gemini API key was rejected")
    assert len(emails) == 1
    assert emails[0]["subject"] == "AI stories: Gemini problem, using Anthropic backup"
    assert "at most $0.25 per day" in emails[0]["body"]


def test_bad_anthropic_key_is_emailed_immediately(keys_ok, emails, monkeypatch):
    import anthropic
    import httpx2

    def bad(key, model):
        req = httpx2.Request("GET", "https://api.anthropic.com/v1/models/x")
        raise anthropic.AuthenticationError("invalid x-api-key",
                                            response=httpx2.Response(401, request=req), body=None)
    monkeypatch.setattr(health, "_check_anthropic", bad)

    statuses = health.check_keys()

    assert "Anthropic API key was rejected" in statuses["anthropic"]
    assert [e["subject"] for e in emails] == ["AI stories: Anthropic problem, using Gemini backup"]


def test_missing_key_and_unknown_model(keys_ok, emails, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    get_settings.cache_clear()

    def not_found(key, model):
        raise AIServiceError("Gemini", 404, "models/x is not found")
    monkeypatch.setattr(health, "_check_gemini", not_found)

    statuses = health.check_keys()

    assert statuses["anthropic"] == "not set"
    assert "wasn't found" in statuses["gemini"]
    assert len(emails) == 2


def test_network_trouble_is_not_reported_as_a_bad_key(keys_ok, emails, monkeypatch):
    def offline(key):
        raise AIServiceError("Finnhub", None, "connection refused")
    monkeypatch.setattr(health, "_check_finnhub", offline)
    assert health.check_keys()["finnhub"].startswith("couldn't check")
    assert emails == []


# ---------------------------------------------------------------------------
# Setup emails when anything changes
# ---------------------------------------------------------------------------


def test_setup_email_on_first_start_and_on_every_change(client, keys_ok, emails, monkeypatch):
    health.startup_check()
    assert len(emails) == 1
    first = emails[0]
    assert first["subject"] == "AI stories setup: ON, Gemini gemini-3.1-flash-lite, at most $0.25/day"
    for text in ("first setup report", "Main    Gemini gemini-3.1-flash-lite: $0 (free) per story; key OK",
                 "Backup  Anthropic claude-haiku-5-5: $0.0042 per story; key OK",
                 "Most per day:      $0.25", "Most per month:    $5.29"):
        assert text in first["body"], text

    health.startup_check()  # restart, nothing changed
    assert len(emails) == 1

    monkeypatch.setenv("AI_ARTICLE_MODEL", "claude-sonnet-5-5")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-NEW-secret")
    get_settings.cache_clear()
    health.startup_check()

    assert len(emails) == 2
    body = emails[1]["body"]
    assert "backup model: claude-haiku-5-5 → claude-sonnet-5-5" in body
    assert "ANTHROPIC_API_KEY: changed" in body
    for secret in ("AIza-secret-gemini", "sk-ant-NEW-secret", "sk-ant-secret-claude", "fh-secret"):
        assert secret not in body and secret not in emails[1]["subject"]


def test_turning_stories_off_is_announced(client, keys_ok, emails, monkeypatch):
    health.startup_check()
    monkeypatch.setenv("AI_ARTICLES_ENABLED", "false")
    get_settings.cache_clear()
    health.startup_check()
    assert "AI stories are OFF" in emails[-1]["body"]
    assert emails[-1]["subject"].startswith("AI stories setup: OFF")


# ---------------------------------------------------------------------------
# Daily summary
# ---------------------------------------------------------------------------


def test_daily_summary_shows_models_and_cost(client, keys_ok, emails):
    from datetime import date

    from app.db.database import get_session_factory
    from app.db.seed import seed
    from app.jobs.daily_summary import _format_text, collect_summary
    from app.models import StockArticle, Ticker

    seed()
    db = get_session_factory()()
    tid = db.query(Ticker).first().id
    for model, tin, tout in (("gemini-3.1-flash-lite", 10_000, 5_000), ("claude-haiku-5-5", 12_000, 6_000)):
        db.add(StockArticle(ticker_id=tid, trading_date=date.today(), direction="up" if "gem" in model else "down",
                            status="ready", move_pct=6, price=1, prev_close=1, model=model,
                            input_tokens=tin, output_tokens=tout))
    db.commit()
    db.close()

    text = _format_text(collect_summary())

    assert "In use now" in text and "key OK" in text
    assert "gemini-3.1-flash-lite × 1, claude-haiku-5-5 × 1" in text
    assert "Estimated cost:    $0.0042" in text  # Gemini free + one Haiku story
