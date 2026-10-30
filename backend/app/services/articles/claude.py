"""Claude-specific pieces: the client, and research with Claude's web search.

Writing and reviewing go through `providers.AnthropicGenerator`, which uses
`create` / `check_stop` / `usage_of` from here.

`research` (only with AI_NEWS_SOURCE=web_search): web search limited to
AI_NEWS_DOMAINS, then the findings come back through a strict
`report_findings` tool. (Structured output can't be used here: it's
incompatible with the citations web search always returns.) Every URL the
search returned is collected, so the validate stage can drop facts citing
anything else. A site that blocks Anthropic's crawler makes the API reject
the whole request; that site is dropped for the rest of the process (the
admin is told once to fix AI_NEWS_DOMAINS) and the search is retried.

On models that support it, requests use the server-side refusal fallback
("default"): if the model declines on safety grounds, the API retries on a
suitable model in the same call (Claude Haiku 5.5 has no fallback).
"""

from __future__ import annotations

import logging
import re
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
_BLOCKED_RE = re.compile(r"domains are not accessible to our user agent: \[([^\]]*)\]")
# Sites web search refused this process; skipped until restart.
_blocked_domains: set[str] = set()

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
            api_key=get_settings().key_for("anthropic"), timeout=_TIMEOUT_S, max_retries=2,
        )
    return _client


def create(**kwargs: Any):
    model = get_settings().model_for("anthropic")
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


def _search_domains() -> list[str]:
    return [d for d in get_settings().ai_news_domain_list if d not in _blocked_domains]


def _drop_blocked_domains(e: anthropic.BadRequestError) -> bool:
    """True if the API refused some of our news sites (and they're now
    skipped), so the search is worth retrying without them."""
    match = _BLOCKED_RE.search(str(e))
    new = set(re.findall(r"'([^']+)'", match.group(1))) - _blocked_domains if match else set()
    if not new:
        return False
    _blocked_domains.update(new)
    log.error(
        "Web search can't use %s (the site blocks Anthropic's crawler); "
        "searching %s instead. Remove it from AI_NEWS_DOMAINS.",
        ", ".join(sorted(new)), ", ".join(_search_domains()) or "nothing",
    )
    return True


def research(req: ArticleRequest) -> ResearchResult:
    settings = get_settings()
    tools = [
        {
            "type": "web_search_20260318",
            "name": "web_search",
            "max_uses": settings.ai_article_max_searches,
            "allowed_domains": _search_domains(),
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
        if not tools[0]["allowed_domains"]:
            raise StoryAIError("research: every site in AI_NEWS_DOMAINS blocks web search")
        try:
            response = create(
                system=prompts.RESEARCH_SYSTEM,
                messages=messages,
                tools=tools,
                tool_choice={"type": "auto"},
                output_config={"effort": "medium"},
            )
        except anthropic.BadRequestError as e:
            if not _drop_blocked_domains(e):
                raise
            tools = [{**tools[0], "allowed_domains": _search_domains()}, *tools[1:]]
            continue
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
