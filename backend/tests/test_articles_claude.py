"""The Claude calls (research / write / review) against a fake client.

No network: the fake returns canned responses shaped like the SDK's.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from app.core.settings import get_settings
from functools import partial

from app.services.articles import claude, stages
from app.services.articles.pipeline import key_problem_reason, run_pipeline
from app.services.articles.providers import AnthropicGenerator, StoryAIError
from tests.test_articles import GOOD_URL, KID_TEXT, TODAY, _request


def _resp(*content, stop="end_turn"):
    return SimpleNamespace(
        content=list(content), stop_reason=stop,
        usage=SimpleNamespace(input_tokens=1000, output_tokens=200,
                              cache_creation_input_tokens=0, cache_read_input_tokens=50),
    )


def _search_result(*urls):
    return SimpleNamespace(type="web_search_tool_result",
                           content=[SimpleNamespace(url=u) for u in urls])


def _report(**kw):
    data = dict(cause_found=True, moved_with_market=False, facts=[dict(
        claim="Apple sold more iPhones than expected.",
        quote="iPhone revenue beat analyst estimates",
        source_url=GOOD_URL, source_name="Reuters", published=TODAY.isoformat(),
    )])
    return SimpleNamespace(type="tool_use", name="report_findings", input={**data, **kw})


def _text(obj):
    return SimpleNamespace(type="text", text=json.dumps(obj))


class _FakeClient:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[dict] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def fake(monkeypatch):
    # Pin these: settings also read backend/.env, which may override defaults.
    monkeypatch.setenv("AI_PROVIDER", "anthropic")
    monkeypatch.setenv("AI_NEWS_SOURCE", "web_search")
    monkeypatch.setenv("AI_API_KEY", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("AI_FALLBACK_PROVIDER", "")
    monkeypatch.setenv("AI_ARTICLE_MODEL", "claude-haiku-5-5")
    monkeypatch.setenv("AI_ARTICLE_MAX_SEARCHES", "2")
    monkeypatch.setenv("AI_NEWS_DOMAINS", "finance.yahoo.com,reuters.com")
    monkeypatch.setattr(claude, "_blocked_domains", set())
    get_settings.cache_clear()

    def install(*responses):
        client = _FakeClient(*responses)
        monkeypatch.setattr(claude, "_get_client", lambda: client)
        return client
    return install


STORY_JSON = {
    "versions": [{"age_group": g, **t} for g, t in KID_TEXT.items()],
    "grown_up_summary": "Quarterly iPhone sales came in above analyst expectations.",
    "tone": "playful", "category": "earnings", "fact_ids": ["f1"],
}
REVIEW_JSON = {"grown_up_ok": True, "approved_age_groups": ["3-5", "6-9", "10-12", "13-17"],
               "problems": []}


# ---------------------------------------------------------------------------
# research
# ---------------------------------------------------------------------------


def test_research_reports_facts_and_every_url_the_search_saw(fake):
    error_result = SimpleNamespace(type="web_search_tool_result",
                                   content=SimpleNamespace(error_code="unavailable"))
    client = fake(_resp(_search_result(GOOD_URL, "https://finance.yahoo.com/x"),
                        error_result, _report(), stop="tool_use"))

    found = claude.research(_request())

    assert found.seen_urls == [GOOD_URL, "https://finance.yahoo.com/x"]
    assert [f.id for f in found.research.facts] == ["f1"]
    assert found.research.facts[0].published == TODAY
    assert found.usage.input_tokens == 1050  # includes cache reads
    call = client.calls[0]
    search_tool = call["tools"][0]
    assert search_tool["allowed_domains"] == ["finance.yahoo.com", "reuters.com"]
    assert search_tool["max_uses"] == 2
    assert search_tool["allowed_callers"] == ["direct"]
    assert call["model"] == "claude-haiku-5-5"
    assert "fallbacks" not in call  # Haiku has no refusal fallback


def _blocked_error(*domains):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    message = f"The following domains are not accessible to our user agent: {list(domains)}."
    return anthropic.BadRequestError(
        message, response=httpx2.Response(400, request=request), body=None,
    )


def test_site_blocking_the_crawler_is_dropped_and_search_retried(fake, caplog):
    client = fake(_blocked_error("reuters.com"),
                  _resp(_search_result(GOOD_URL), _report(), stop="tool_use"),
                  _resp(_search_result(GOOD_URL), _report(), stop="tool_use"))

    assert claude.research(_request()).research.facts
    assert claude.research(_request()).research.facts

    assert [c["tools"][0]["allowed_domains"] for c in client.calls] == [
        ["finance.yahoo.com", "reuters.com"], ["finance.yahoo.com"], ["finance.yahoo.com"],
    ]
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1 and "reuters.com" in errors[0].getMessage()


def test_every_site_blocked_fails_without_a_key_problem(fake):
    fake(_blocked_error("finance.yahoo.com", "reuters.com"))
    result = run_pipeline(_request(), claude.research, *_claude_stages())
    assert result.status == "failed"
    assert not result.key_problem


def test_other_bad_requests_still_raise(fake):
    fake(_blocked_error().__class__(
        "something else", response=httpx2.Response(
            400, request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")),
        body=None,
    ))
    with pytest.raises(anthropic.BadRequestError):
        claude.research(_request())


def test_defaults_are_the_cheap_ones():
    from app.core.settings import Settings
    fields = Settings.model_fields
    assert fields["ai_provider"].default == "anthropic"
    assert fields["ai_news_source"].default == "web_search"
    assert Settings(ai_article_model="").story_model == "claude-haiku-5-5"
    assert fields["ai_article_max_searches"].default == 2
    assert fields["ai_article_always_pct"].default == 5.0
    assert fields["ai_news_domains"].default == "finance.yahoo.com"  # reuters.com blocks web search
    assert Settings(ai_provider="anthropic", ai_article_model="").story_model == "claude-haiku-5-5"
    assert Settings(ai_provider="gemini", ai_article_model="").story_model == "gemini-3.1-flash-lite"
    # A model from the other provider is ignored rather than sent to the wrong API.
    assert Settings(ai_provider="gemini", ai_article_model="claude-opus-5-5").story_model == "gemini-3.1-flash-lite"
    # One model per provider: Claude uses Haiku, Gemini its cheapest.
    both = Settings(ai_provider="gemini", ai_fallback_provider="anthropic", ai_article_model="claude-haiku-5-5")
    assert both.provider_chain == ["gemini", "anthropic"]
    assert (both.model_for("gemini"), both.model_for("anthropic")) == ("gemini-3.1-flash-lite", "claude-haiku-5-5")
    # Per-provider keys win; AI_API_KEY only fills in for the main provider.
    keys = Settings(ai_provider="gemini", ai_api_key="old", anthropic_api_key="sk-ant", gemini_api_key="")
    assert (keys.key_for("gemini"), keys.key_for("anthropic")) == ("old", "sk-ant")


def test_refusal_fallback_is_used_on_models_that_have_it(fake, monkeypatch):
    monkeypatch.setenv("AI_ARTICLE_MODEL", "claude-opus-5-5")
    get_settings.cache_clear()
    client = fake(_resp(_report(), stop="tool_use"))
    claude.research(_request())
    assert client.calls[0]["fallbacks"] == "default"
    assert client.calls[0]["betas"] == ["server-side-fallback-2026-07-01"]


def test_research_resumes_after_pause_turn(fake):
    paused = _resp(_search_result(GOOD_URL), stop="pause_turn")
    client = fake(paused, _resp(_report(), stop="tool_use"))

    found = claude.research(_request())

    assert found.seen_urls == [GOOD_URL]
    second = client.calls[1]["messages"]
    assert second[-1] == {"role": "assistant", "content": paused.content}  # no "continue" message


def test_research_reminds_once_then_gives_up(fake):
    client = fake(*[_resp(SimpleNamespace(type="text", text="I looked around."))] * 4)
    with pytest.raises(StoryAIError):
        claude.research(_request())
    assert "report_findings" in client.calls[1]["messages"][-1]["content"]


def test_research_skips_malformed_facts(fake):
    bad = dict(claim="x", quote="y", source_url=GOOD_URL, source_name="Reuters",
               published="not a date")
    fake(_resp(_report(facts=[bad]), stop="tool_use"))
    assert claude.research(_request()).research.facts == []


def test_declined_request_raises(fake):
    fake(_resp(stop="refusal"))
    with pytest.raises(StoryAIError):
        claude.research(_request())


# ---------------------------------------------------------------------------
# write / review
# ---------------------------------------------------------------------------


def test_write_uses_structured_output(fake):
    client = fake(_resp(SimpleNamespace(type="thinking", thinking=""), _text(STORY_JSON)))
    from app.services.articles.schemas import Fact
    fact = Fact(id="f1", claim="c", quote="q", source_url=GOOD_URL, source_name="Reuters",
                published=TODAY)

    written = stages.write(AnthropicGenerator(), _request(), [fact], False)

    assert [v.age_group for v in written.story.versions] == list(KID_TEXT)
    fmt = client.calls[0]["output_config"]["format"]
    assert fmt == {"type": "json_schema", "schema": stages.STORY_SCHEMA}
    assert "tools" not in client.calls[0]  # the writer never touches the web


def test_cut_off_answer_raises(fake):
    fake(_resp(_text(STORY_JSON), stop="max_tokens"))
    with pytest.raises(StoryAIError):
        stages.write(AnthropicGenerator(), _request(), [], False)


def test_schemas_are_strict_everywhere():
    def objects(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                yield node
            for v in node.values():
                yield from objects(v)
        elif isinstance(node, list):
            for v in node:
                yield from objects(v)
    from app.services.articles.news import FINDINGS_SCHEMA
    for schema in (stages.STORY_SCHEMA, stages.REVIEW_SCHEMA, FINDINGS_SCHEMA,
                   claude.REPORT_TOOL["input_schema"]):
        for obj in objects(schema):
            assert obj["additionalProperties"] is False
            assert set(obj["required"]) == set(obj["properties"])


def _claude_stages():
    gen = AnthropicGenerator()
    return partial(stages.write, gen), partial(stages.review, gen)


# ---------------------------------------------------------------------------
# End to end through the pipeline
# ---------------------------------------------------------------------------


def test_whole_pipeline_with_claude_stages(fake):
    fake(
        _resp(_search_result(GOOD_URL), _report(), stop="tool_use"),
        _resp(_text(STORY_JSON)),
        _resp(_text(REVIEW_JSON)),
    )
    result = run_pipeline(_request(), claude.research, *_claude_stages())
    assert result.status == "ready", result.error
    assert len(result.story.versions) == 4
    assert result.usage.input_tokens == 3 * 1050


def test_made_up_source_never_reaches_the_writer(fake):
    """The AI cites a page the search never returned: the fact is dropped,
    so there's nothing to write about and the writer isn't called."""
    client = fake(_resp(_search_result("https://finance.yahoo.com/other"), _report(), stop="tool_use"))
    result = run_pipeline(_request(), claude.research, *_claude_stages())
    assert result.status == "no_clear_cause"
    assert len(client.calls) == 1


def test_real_sdk_auth_error_counts_as_a_key_problem(fake):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    err = anthropic.AuthenticationError(
        "invalid x-api-key", response=httpx2.Response(401, request=request), body=None,
    )
    fake(err)
    result = run_pipeline(_request(), claude.research, *_claude_stages())
    assert result.status == "failed"
    assert "rejected" in key_problem_reason(err)
    assert result.key_problem and "rejected" in result.key_problem
