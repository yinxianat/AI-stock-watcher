"""Gemini + Finnhub news (the free setup), with HTTP faked. No network."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from functools import partial

import httpx
import pytest

from app.core.settings import get_settings
from app.services.articles import news, providers, stages
from app.services.articles.pipeline import key_problem_reason, run_pipeline
from app.services.articles.providers import AIServiceError, GeminiGenerator, StoryAIError
from tests.test_articles import KID_TEXT, TODAY, _request

NOON_ET = int(datetime(2026, 10, 29, 16, 0, tzinfo=timezone.utc).timestamp())


@pytest.fixture(autouse=True)
def _free_setup(monkeypatch):
    for k, v in {
        "AI_PROVIDER": "gemini", "AI_NEWS_SOURCE": "finnhub", "AI_ARTICLE_MODEL": "",
        "AI_API_KEY": "", "GEMINI_API_KEY": "gem-test-key", "FINNHUB_API_KEY": "fh-test-key",
        "AI_FALLBACK_PROVIDER": "", "ANTHROPIC_API_KEY": "",
        "AI_FINNHUB_SOURCES": "Yahoo,CNBC", "AI_NEWS_MAX_AGE_DAYS": "2",
    }.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    monkeypatch.setattr(providers, "_RETRY_WAITS", (0.0, 0.0))


def _http(status: int, body) -> httpx.Response:
    return httpx.Response(status, json=body, request=httpx.Request("GET", "https://x.test"))


def _gemini_answer(obj, finish="STOP", thought=False) -> dict:
    parts = [{"text": "thinking...", "thought": True}] if thought else []
    parts.append({"text": json.dumps(obj)})
    return {
        "candidates": [{"content": {"parts": parts}, "finishReason": finish}],
        "usageMetadata": {"promptTokenCount": 900, "candidatesTokenCount": 300,
                          "thoughtsTokenCount": 100},
    }


ARTICLES = [
    {"id": 1, "source": "Yahoo", "datetime": NOON_ET, "url": "https://finnhub.io/api/news?id=a1",
     "headline": "Apple beats estimates as iPhone sales jump",
     "summary": "Apple's quarterly revenue topped Wall Street forecasts, driven by strong iPhone demand."},
    {"id": 2, "source": "SeekingAlpha", "datetime": NOON_ET, "url": "https://finnhub.io/api/news?id=a2",
     "headline": "Why I'm selling Apple today", "summary": "An opinion piece."},
    {"id": 3, "source": "CNBC", "datetime": NOON_ET - 3600, "url": "https://finnhub.io/api/news?id=a3",
     "headline": "Tech stocks rally", "summary": "Big tech shares rose broadly on Thursday."},
]
FINDINGS = {"cause_found": True, "moved_with_market": False, "facts": [
    {"article_id": "a1", "claim": "Apple's results beat expectations.",
     "quote": "Apple's quarterly revenue topped Wall Street forecasts"},
]}
STORY = {
    "versions": [{"age_group": g, **t} for g, t in KID_TEXT.items()],
    "grown_up_summary": "Quarterly revenue came in above analyst forecasts.",
    "tone": "playful", "category": "earnings", "fact_ids": ["f1"],
}
REVIEW = {"grown_up_ok": True, "approved_age_groups": ["3-5", "6-9", "10-12", "13-17"], "problems": []}


@pytest.fixture
def web(monkeypatch):
    """Fake Finnhub (GET) and Gemini (POST). Queue Gemini answers in order."""
    state = {"posts": [], "gemini": [], "finnhub": _http(200, ARTICLES), "gets": []}

    def fake_get(url, params=None, timeout=None):
        state["gets"].append((url, params))
        return state["finnhub"]

    def fake_post(url, json=None, headers=None, timeout=None):
        state["posts"].append({"url": url, "json": json, "headers": headers})
        return state["gemini"].pop(0)

    monkeypatch.setattr(news.httpx, "get", fake_get)
    monkeypatch.setattr(providers.httpx, "post", fake_post)
    return state


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------


def test_gemini_request_shape_and_answer(web):
    web["gemini"] = [_http(200, _gemini_answer(REVIEW, thought=True))]
    text, usage = GeminiGenerator().generate("SYS", "PROMPT", stages.REVIEW_SCHEMA)

    assert json.loads(text) == REVIEW  # thought parts are skipped
    assert (usage.input_tokens, usage.output_tokens) == (900, 400)
    post = web["posts"][0]
    assert post["url"].endswith("/models/gemini-3.1-flash-lite:generateContent")
    assert post["headers"] == {"x-goog-api-key": "gem-test-key"}
    assert "key=" not in post["url"]  # key never in the URL (or logs)
    cfg = post["json"]["generationConfig"]
    assert cfg == {"responseMimeType": "application/json", "responseJsonSchema": stages.REVIEW_SCHEMA}
    assert post["json"]["systemInstruction"] == {"parts": [{"text": "SYS"}]}


def test_gemini_retries_busy_errors(web):
    web["gemini"] = [_http(503, {"error": {"status": "UNAVAILABLE"}}),
                     _http(200, _gemini_answer(REVIEW))]
    GeminiGenerator().generate("S", "P", stages.REVIEW_SCHEMA)
    assert len(web["posts"]) == 2


@pytest.mark.parametrize("answer,problem", [
    ({"promptFeedback": {"blockReason": "SAFETY"}}, "declined"),
    (_gemini_answer(REVIEW, finish="MAX_TOKENS"), "cut off"),
    (_gemini_answer(REVIEW, finish="SAFETY"), "stopped early"),
    ({"candidates": []}, "no answer"),
])
def test_gemini_unusable_answers_raise(web, answer, problem):
    web["gemini"] = [_http(200, answer)]
    with pytest.raises(StoryAIError, match=problem):
        GeminiGenerator().generate("S", "P", stages.REVIEW_SCHEMA)


@pytest.mark.parametrize("status,body,expected", [
    (400, {"error": {"status": "INVALID_ARGUMENT", "message": "API key not valid. Please pass a valid API key."}},
     "Gemini API key was rejected"),
    (403, {"error": {"status": "PERMISSION_DENIED", "message": "Your project has been denied access."}},
     "isn't allowed"),
    (429, {"error": {"status": "RESOURCE_EXHAUSTED", "message": "You exceeded your current quota."}},
     "quota is used up"),
    (400, {"error": {"status": "INVALID_ARGUMENT", "message": "Invalid JSON payload"}}, None),
])
def test_gemini_key_and_quota_problems(web, status, body, expected):
    web["gemini"] = [_http(status, body)] * 3
    with pytest.raises(AIServiceError) as info:
        GeminiGenerator().generate("S", "P", stages.REVIEW_SCHEMA)
    reason = key_problem_reason(info.value)
    assert (reason is None) if expected is None else (expected in reason)


# ---------------------------------------------------------------------------
# Finnhub news
# ---------------------------------------------------------------------------


def test_fetch_news_keeps_allowed_publishers_newest_first(web):
    items = news.fetch_news("AAPL", TODAY)
    assert [i.source for i in items] == ["Yahoo Finance", "CNBC"]  # SeekingAlpha filtered out
    assert [i.id for i in items] == ["a1", "a2"]
    assert items[0].published == TODAY
    url, params = web["gets"][0]
    assert params["symbol"] == "AAPL" and params["from"] == "2026-10-27" and params["to"] == "2026-10-29"


def test_missing_or_rejected_finnhub_key_is_a_key_problem(web, monkeypatch):
    web["finnhub"] = _http(401, {"error": "Invalid API key"})
    with pytest.raises(AIServiceError) as info:
        news.fetch_news("AAPL", TODAY)
    assert "Finnhub API key was rejected" in key_problem_reason(info.value)


def test_quotes_must_be_word_for_word():
    item = news.NewsItem("a1", "Yahoo Finance", TODAY, "Apple beats estimates",
                         "Apple’s quarterly revenue topped  Wall Street forecasts.", "https://finnhub.io/x")
    assert news.quote_matches("Apple's quarterly revenue topped Wall Street forecasts", item)
    assert news.quote_matches("APPLE BEATS ESTIMATES", item)
    assert not news.quote_matches("Apple's revenue crushed forecasts", item)
    assert not news.quote_matches("", item)


def test_research_from_news_takes_links_and_dates_from_finnhub(web):
    bad = {"article_id": "a1", "claim": "x", "quote": "Apple will double next year"}
    made_up = {"article_id": "a9", "claim": "y", "quote": "Tech stocks rally"}
    web["gemini"] = [_http(200, _gemini_answer({**FINDINGS, "facts": FINDINGS["facts"] + [bad, made_up]}))]

    found = news.research_from_news(GeminiGenerator(), _request())

    assert [f.quote for f in found.research.facts] == [FINDINGS["facts"][0]["quote"]]
    fact = found.research.facts[0]
    assert fact.source_url == "https://finnhub.io/api/news?id=a1"
    assert fact.source_name == "Yahoo Finance" and fact.published == TODAY
    prompt = web["posts"][0]["json"]["contents"][0]["parts"][0]["text"]
    assert "Apple beats estimates" in prompt and "Why I'm selling" not in prompt


def test_no_news_means_no_ai_call(web):
    web["finnhub"] = _http(200, [])
    found = news.research_from_news(GeminiGenerator(), _request())
    assert found.research.cause_found is False
    assert web["posts"] == []  # free


# ---------------------------------------------------------------------------
# The whole free pipeline
# ---------------------------------------------------------------------------


def test_free_pipeline_end_to_end(web):
    web["gemini"] = [_http(200, _gemini_answer(FINDINGS)),
                     _http(200, _gemini_answer(STORY)),
                     _http(200, _gemini_answer(REVIEW))]
    gen = GeminiGenerator()
    result = run_pipeline(_request(), partial(news.research_from_news, gen),
                          partial(stages.write, gen), partial(stages.review, gen))

    assert result.status == "ready", result.error
    assert result.model == "gemini-3.1-flash-lite"
    assert [f.source_url for f in result.facts] == ["https://finnhub.io/api/news?id=a1"]
    assert len(web["posts"]) == 3


def test_service_uses_the_configured_provider(web, monkeypatch):
    from app.services.articles import service
    web["gemini"] = [_http(200, _gemini_answer(FINDINGS)),
                     _http(200, _gemini_answer(STORY)),
                     _http(200, _gemini_answer(REVIEW))]
    assert service._generate_with_ai(_request()).status == "ready"
    assert all("generativelanguage.googleapis.com" in p["url"] for p in web["posts"])
