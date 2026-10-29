"""Claude-specific pieces: the client, and research with Claude's web search.

Writing and reviewing go through `providers.AnthropicGenerator`, which uses
`create` / `check_stop` / `usage_of` from here.

`research` (only with AI_NEWS_SOURCE=web_search): web search limited to
AI_NEWS_DOMAINS, then the findings come back through a strict
`report_findings` tool. (Structured output can't be used here: it's
incompatible with the citations web search always returns.) Every URL the
search returned is collected, so the validate stage can drop facts citing
anything else.

On models that support it, requests use the server-side refusal fallback
("default"): if the model declines on safety grounds, the API retries on a
suitable model in the same call (Claude Haiku 5.5 has no fallback).
"""

from __future__ import annotations

import logging
from typing import Any

import anthropic
from pydantic import ValidationError

from app.core.settings import get_settings
from app.services.articles import prompts
from app.services.articles.providers import StoryAIError
from app.services.articles.schemas import ArticleRequest, Fact, Research, ResearchResult, Usage

log = logging.getLogger(__name__)

__all__ = ["REPORT_TOOL", "StoryAIError", "check_stop", "create", "research", "usage_of"]

_REPORT_TOOL = "report_findings"
_MAX_CONTINUATIONS = 3  # pause_turn resumes / reminders before giving up
_TIMEOUT_S = 120.0
_FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Models that accept `fallbacks: "default"`; others reject the parameter.
_FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}

REPORT_TOOL: dict[str, Any] = {
    "name": _REPORT_TOOL,
    "description": (
        "Report why the stock moved today, based only on the search results. "
        "Call exactly once, when the research is done."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "cause_found": {"type": "boolean"},
            "moved_with_market": {"type": "boolean"},
            "facts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "claim": {"type": "string"},
                        "quote": {"type": "string"},
                        "source_url": {"type": "string"},
                        "source_name": {"type": "string"},
                        "published": {"type": "string", "format": "date"},
                    },
                    "required": ["claim", "quote", "source_url", "source_name", "published"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["cause_found", "moved_with_market", "facts"],
        "additionalProperties": False,
    },
}

_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(
            api_key=get_settings().story_api_key, timeout=_TIMEOUT_S, max_retries=2,
        )
    return _client


def create(**kwargs: Any):
    model = get_settings().story_model
    if model in _FALLBACK_MODELS:
        kwargs.update(betas=[_FALLBACK_BETA], fallbacks="default")
    return _get_client().beta.messages.create(
        model=model,
        max_tokens=16000,
        cache_control={"type": "ephemeral"},  # system prompts never change
        **kwargs,
    )


def usage_of(response) -> Usage:
    u = response.usage
    return Usage(
        input_tokens=(u.input_tokens or 0)
        + (getattr(u, "cache_creation_input_tokens", 0) or 0)
        + (getattr(u, "cache_read_input_tokens", 0) or 0),
        output_tokens=u.output_tokens or 0,
    )


def check_stop(response, stage: str) -> None:
    if response.stop_reason == "refusal":
        raise StoryAIError(f"{stage}: the model declined")
    if response.stop_reason == "max_tokens":
        raise StoryAIError(f"{stage}: the answer was cut off")


def _search_urls(response) -> list[str]:
    """Every URL web search returned in this response. A failed search's
    content is an error object, not a list."""
    urls: list[str] = []
    for block in response.content:
        if block.type == "web_search_tool_result" and isinstance(block.content, list):
            urls += [r.url for r in block.content if getattr(r, "url", None)]
    return urls


def _research_from(report: dict[str, Any]) -> Research:
    facts: list[Fact] = []
    for i, raw in enumerate(report.get("facts") or [], start=1):
        try:
            facts.append(Fact(id=f"f{i}", **raw))
        except (TypeError, ValidationError):
            log.info("Research: skipped a malformed fact")
    return Research(
        cause_found=bool(report.get("cause_found")),
        moved_with_market=bool(report.get("moved_with_market")),
        facts=facts,
    )


def research(req: ArticleRequest) -> ResearchResult:
    settings = get_settings()
    tools = [
        {
            "type": "web_search_20260318",
            "name": "web_search",
            "max_uses": settings.ai_article_max_searches,
            "allowed_domains": settings.ai_news_domain_list,
            # Plain search, no code-execution filtering: works on every model
            # (including Haiku) and keeps every result URL in the response.
            "allowed_callers": ["direct"],
        },
        REPORT_TOOL,
    ]
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": prompts.research_prompt(req)},
    ]
    usage, seen = Usage(), []
    for _ in range(_MAX_CONTINUATIONS + 1):
        response = create(
            system=prompts.RESEARCH_SYSTEM,
            messages=messages,
            tools=tools,
            tool_choice={"type": "auto"},
            output_config={"effort": "medium"},
        )
        usage += usage_of(response)
        seen += _search_urls(response)
        check_stop(response, "research")
        report = next(
            (b for b in response.content if b.type == "tool_use" and b.name == _REPORT_TOOL),
            None,
        )
        if report is not None:
            return ResearchResult(_research_from(report.input), seen_urls=seen, usage=usage)
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "pause_turn":
            # Finished without reporting. (After pause_turn the server
            # resumes by itself, so no extra message is sent.)
            messages.append({"role": "user", "content": f"Now call {_REPORT_TOOL} with what you found."})
    raise StoryAIError("research: no report after several tries")
