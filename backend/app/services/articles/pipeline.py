"""Research → validate → write, independent of which AI client is used.

`research` and `write` are passed in, so tests (and a future provider) can
swap them. Never raises: every problem becomes a `failed` result, so an
alert email is never held up by an exception here.
"""

from __future__ import annotations

import logging
from typing import Callable

from app.core.settings import get_settings
from app.services.articles.schemas import (
    ArticleRequest,
    Fact,
    GenerationResult,
    ResearchResult,
    Usage,
    WriteResult,
)
from app.services.articles.validate import story_problems, validate_facts

log = logging.getLogger(__name__)

ResearchFn = Callable[[ArticleRequest], ResearchResult]
WriteFn = Callable[[ArticleRequest, list[Fact], bool], WriteResult]


def key_problem_reason(e: Exception) -> str | None:
    """Plain-English reason if the AI service refused us because of the API
    key or billing, else None (outages and rate limits fix themselves)."""
    status = getattr(e, "status_code", None)
    text = str(e).lower()
    if status == 401:
        return "the API key was rejected (wrong, disabled or deleted)"
    if status == 403:
        return "the API key isn't allowed to use this model or feature"
    if status in (400, 402) and ("credit balance" in text or "billing" in text):
        return "the Anthropic account is out of credit or over its spending limit"
    return None


def run_pipeline(req: ArticleRequest, research: ResearchFn, write: WriteFn) -> GenerationResult:
    settings = get_settings()
    usage = Usage()
    try:
        found = research(req)
        usage += found.usage
        facts = validate_facts(
            found.research.facts,
            seen_urls=found.seen_urls,
            allowed_domains=settings.ai_news_domain_list,
            today=req.trading_date,
            max_age_days=settings.ai_news_max_age_days,
        )
        dropped = len(found.research.facts) - len(facts)
        if dropped:
            log.info("Story %s: dropped %d unverifiable fact(s)", req.symbol, dropped)
        if not found.research.cause_found or not facts:
            return GenerationResult(
                status="no_clear_cause",
                moved_with_market=found.research.moved_with_market,
                model=settings.ai_article_model,
                usage=usage,
            )

        written = write(req, facts, found.research.moved_with_market)
        usage += written.usage
        problems = story_problems(written.story, facts)
        if problems:
            log.warning("Story %s rejected: %s", req.symbol, "; ".join(problems))
            return GenerationResult(
                status="failed", model=settings.ai_article_model, usage=usage,
                error=("rejected: " + "; ".join(problems))[:500],
            )
        used = [f for f in facts if f.id in set(written.story.fact_ids)]
        return GenerationResult(
            status="ready",
            story=written.story,
            facts=used,
            moved_with_market=found.research.moved_with_market,
            model=settings.ai_article_model,
            usage=usage,
        )
    except Exception as e:  # noqa: BLE001 — never let a story break an alert
        log.warning("Story generation failed for %s: %s", req.symbol, e)
        return GenerationResult(
            status="failed", model=settings.ai_article_model, usage=usage,
            error=str(e)[:500], key_problem=key_problem_reason(e),
        )
