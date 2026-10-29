"""Research → validate → write → check → review, independent of which AI
client is used.

Two safety layers stand between the writer and a family's inbox: the
code-level language check (`language.py`, per age-group version) and an
independent AI reviewer. A version either layer rejects is dropped; if the
reviewer can't run, nothing is sent (fail closed).

`research`, `write` and `review` are passed in, so tests (and a future
provider) can swap them. Never raises: every problem becomes a `failed`
result, so an alert email is never held up by an exception here.
"""

from __future__ import annotations

import logging
from typing import Callable

import anthropic

from app.core.settings import get_settings
from app.services.articles.schemas import (
    ArticleRequest,
    Fact,
    GenerationResult,
    ResearchResult,
    ReviewResult,
    Story,
    Usage,
    WriteResult,
)
from app.services.articles.validate import shared_problems, validate_facts, version_problems

log = logging.getLogger(__name__)

ResearchFn = Callable[[ArticleRequest], ResearchResult]
WriteFn = Callable[[ArticleRequest, list[Fact], bool], WriteResult]
ReviewFn = Callable[[ArticleRequest, list[Fact], Story], ReviewResult]


def failed_service(e: Exception) -> str | None:
    """Which service's error this is, or None if it isn't a service error."""
    if getattr(e, "service", None):
        return e.service
    if isinstance(e, anthropic.APIError):
        return "Anthropic"
    return None


def key_problem_reason(e: Exception) -> str | None:
    """Plain-English reason if a service refused us because of its API key,
    billing or quota, else None (outages and short rate limits pass)."""
    status = getattr(e, "status_code", None)
    service = failed_service(e) or "AI service"
    text = str(e).lower()
    if status == 401 or (status == 400 and ("api key not valid" in text or "api_key_invalid" in text)):
        return f"the {service} API key was rejected (wrong, disabled or deleted)"
    if status == 403:
        return f"the {service} API key isn't allowed to do this (no access to this model or feature)"
    if status in (400, 402) and ("credit balance" in text or "billing" in text or "prepay" in text):
        return f"the {service} account is out of credit or over its spending limit"
    if status == 429 and ("quota" in text or "resource_exhausted" in text):
        return f"the {service} quota is used up (for a free tier, usually the daily limit)"
    return None


def _failed(settings, usage: Usage, why: str) -> GenerationResult:
    return GenerationResult(
        status="failed", model=settings.story_model, usage=usage, error=why[:500],
    )


def run_pipeline(
    req: ArticleRequest, research: ResearchFn, write: WriteFn, review: ReviewFn,
) -> GenerationResult:
    settings = get_settings()
    usage = Usage()
    try:
        found = research(req)
        usage += found.usage
        facts = validate_facts(
            found.research.facts,
            seen_urls=found.seen_urls,
            allowed_domains=settings.story_link_domains,
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
                model=settings.story_model,
                usage=usage,
            )

        written = write(req, facts, found.research.moved_with_market)
        usage += written.usage
        story = written.story

        # Safety layer 1: code-level language check.
        problems = shared_problems(story, facts)
        if problems:
            log.warning("Story %s rejected: %s", req.symbol, "; ".join(problems))
            return _failed(settings, usage, "rejected: " + "; ".join(problems))
        versions, seen = [], set()
        for v in story.versions:
            if v.age_group in seen:
                continue
            seen.add(v.age_group)
            bad = version_problems(v)
            if bad:
                log.warning("Story %s ages %s dropped: %s", req.symbol, v.age_group, "; ".join(bad))
            else:
                versions.append(v)
        if not versions:
            return _failed(settings, usage, "every age version failed the language check")
        story = story.model_copy(update={"versions": versions})
        used = [f for f in facts if f.id in set(story.fact_ids)]

        # Safety layer 2: independent AI reviewer. Errors fall through to the
        # except below, so a story that couldn't be reviewed is never sent.
        reviewed = review(req, used, story)
        usage += reviewed.usage
        verdict = reviewed.review
        if verdict.problems:
            log.warning("Story %s reviewer: %s", req.symbol, "; ".join(verdict.problems))
        if not verdict.grown_up_ok:
            return _failed(settings, usage, "reviewer rejected the grown-up summary")
        approved = set(verdict.approved_age_groups)
        versions = [v for v in story.versions if v.age_group in approved]
        if not versions:
            return _failed(settings, usage, "reviewer rejected every age version")
        story = story.model_copy(update={"versions": versions})

        return GenerationResult(
            status="ready",
            story=story,
            facts=used,
            moved_with_market=found.research.moved_with_market,
            model=settings.story_model,
            usage=usage,
        )
    except Exception as e:  # noqa: BLE001 — never let a story break an alert
        log.warning("Story generation failed for %s: %s", req.symbol, e)
        return GenerationResult(
            status="failed", model=settings.story_model, usage=usage,
            error=str(e)[:500], key_problem=key_problem_reason(e),
            failed_service=failed_service(e),
        )
