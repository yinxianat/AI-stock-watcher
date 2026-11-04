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
from time import monotonic
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

# The most goes the writer gets at one story, whatever the setting says.
# Each retry is told what the reviewer objected to, so a high ceiling is
# adversarial pressure on the safety gate rather than just a bigger bill.
MAX_WRITE_ATTEMPTS = 4

ResearchFn = Callable[[ArticleRequest], ResearchResult]
# `rejection` is why the previous attempt was thrown out, passed only on a
# retry. Keyword-only and optional, so a first attempt calls a writer
# exactly as it always did.
WriteFn = Callable[..., WriteResult]
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


def _failed(settings, usage: Usage, why: str, diagnostics: dict | None = None) -> GenerationResult:
    return GenerationResult(
        status="failed", model=settings.story_model, usage=usage, error=why[:500],
        diagnostics=diagnostics or {},
    )


def run_pipeline(
    req: ArticleRequest, research: ResearchFn, write: WriteFn, review: ReviewFn,
    deadline: float | None = None,
) -> GenerationResult:
    """Research → validate → write → check → review, returning the result.

    `deadline` is a `time.monotonic()` value past which a retry must not be
    started — normally now plus the window the alert email is waiting on.
    Without one, retries are only bounded by the attempt count, which is
    fine for a recap or a backfill and wrong for an alert: a rewrite that
    overruns the wait turns "no story" into "we're still looking" and a
    promised follow-up, and a promise that does not arrive is worse for the
    family than a plain alert.
    """
    settings = get_settings()
    usage = Usage()
    diag: dict = {}
    try:
        found = research(req)
        usage += found.usage
        check = validate_facts(
            found.research.facts,
            seen_urls=found.seen_urls,
            allowed_domains=settings.story_link_domains,
            today=req.trading_date,
            max_age_days=settings.ai_news_max_age_days,
        )
        facts = check.kept
        # Recorded whatever happens next: an empty story and a good one are
        # told apart by these numbers, and so are the two completely
        # different causes of an empty one — research finding nothing, and
        # research finding things that failed the checks.
        diag = {
            "cause_found": found.research.cause_found,
            "facts_found": len(found.research.facts),
            "facts_kept": len(facts),
            "urls_seen": len(found.seen_urls),
            "dropped_by_rule": check.rule_counts(),
            "dropped": [
                {"fact_id": d.fact_id, "rule": d.rule, "detail": d.detail}
                for d in check.dropped[:10]
            ],
            # Both, because one without the other says nothing: "2" is only
            # meaningful next to the ceiling it stopped under. Searches are
            # billed per use, so `searches_used` is the real cost and
            # `max_searches` is only what it was allowed to reach.
            "searches_used": found.searches_used,
            "max_searches": settings.ai_article_max_searches,
        }
        if check.dropped:
            log.info(
                "Story %s: dropped %d unverifiable fact(s): %s",
                req.symbol, len(check.dropped), check.rule_counts(),
            )
        if not found.research.cause_found or not facts:
            return GenerationResult(
                status="no_clear_cause",
                moved_with_market=found.research.moved_with_market,
                model=settings.story_model,
                usage=usage,
                diagnostics=diag,
            )

        # Writing, checking and reviewing, with up to
        # AI_ARTICLE_MAX_WRITE_ATTEMPTS goes at it.
        #
        # What gets another go is the WRITER, never the reviewer. Re-running
        # the same reviewer over the same text is the same model reading the
        # same words, so it would bill us for the verdict we already have.
        # The reviewer stays single and stays strict — a second opinion
        # exists to overturn a rejection, and a story one reviewer called
        # unsuitable for a child is not something to ship because another
        # model disagreed. No story is better than a wrong story, so the
        # gate does not move; only the number of chances to clear it does.
        #
        # Each retry is told what was wrong with the last attempt. A blind
        # reroll is a coin toss on the same prompt; "your last version was
        # rejected for frightening wording for ages 6-9" is a different
        # proposition. That feedback is also the reason the ceiling is low:
        # a writer being coached past its own reviewer is adversarial
        # pressure on a safety gate, negligible at two attempts and not
        # something to raise casually.
        # Bounded at both ends. 0 or a negative must not mean "never write
        # a story", and a typo like 20 must not mean twenty write-and-review
        # rounds per story — the feedback loop makes a high ceiling a
        # writer being coached past its own safety check, so the ceiling is
        # a safety limit and not only a spend one.
        attempts_allowed = min(MAX_WRITE_ATTEMPTS,
                               max(1, settings.ai_article_max_write_attempts))
        if settings.ai_article_max_write_attempts > MAX_WRITE_ATTEMPTS:
            log.warning(
                "AI_ARTICLE_MAX_WRITE_ATTEMPTS is %d; capped at %d",
                settings.ai_article_max_write_attempts, MAX_WRITE_ATTEMPTS,
            )
        rejections: list[dict] = []
        feedback: list[str] = []
        story: Story | None = None
        used: list[Fact] = []

        for attempt in range(1, attempts_allowed + 1):
            attempt_started = monotonic()
            written = write(
                req, facts, found.research.moved_with_market,
                # Passed only when there is something to say, so a first
                # attempt calls the writer exactly as it always did.
                **({"rejection": feedback} if feedback else {}),
            )
            usage += written.usage
            candidate = written.story
            rejected_by: str | None = None
            problems: list[str] = []

            # Safety layer 1: code-level language check.
            shared = shared_problems(candidate, facts)
            if shared:
                log.warning("Story %s rejected: %s", req.symbol, "; ".join(shared))
                diag["shared_problems"] = shared
                rejected_by, problems = "language_check_shared", shared
            else:
                versions, seen = [], set()
                dropped_versions: dict[str, list[str]] = {}
                for v in candidate.versions:
                    if v.age_group in seen:
                        continue
                    seen.add(v.age_group)
                    bad = version_problems(v)
                    if bad:
                        log.warning("Story %s ages %s dropped: %s", req.symbol, v.age_group, "; ".join(bad))
                        dropped_versions[v.age_group] = bad
                    else:
                        versions.append(v)
                diag["versions_written"] = sorted(seen)
                if dropped_versions:
                    # Which age group failed which check. An age group that
                    # keeps failing is a prompt problem for that age, not a
                    # safety problem.
                    diag["versions_dropped_by_language_check"] = dropped_versions
                if not versions:
                    rejected_by = "language_check_all_versions"
                    problems = sorted({p for ps in dropped_versions.values() for p in ps})
                else:
                    candidate = candidate.model_copy(update={"versions": versions})
                    used = [f for f in facts if f.id in set(candidate.fact_ids)]

                    # Safety layer 2: independent AI reviewer. Errors fall
                    # through to the except below, so a story that couldn't
                    # be reviewed is never sent.
                    reviewed = review(req, used, candidate)
                    usage += reviewed.usage
                    verdict = reviewed.review
                    if verdict.problems:
                        log.warning("Story %s reviewer: %s", req.symbol, "; ".join(verdict.problems))
                        diag["reviewer_problems"] = verdict.problems
                    if not verdict.grown_up_ok:
                        rejected_by = "reviewer_grown_up_summary"
                        problems = list(verdict.problems)
                    else:
                        approved = set(verdict.approved_age_groups)
                        rejected = [v.age_group for v in candidate.versions
                                    if v.age_group not in approved]
                        if rejected:
                            diag["versions_rejected_by_reviewer"] = sorted(rejected)
                        kept = [v for v in candidate.versions if v.age_group in approved]
                        if not kept:
                            rejected_by = "reviewer_all_versions"
                            problems = list(verdict.problems)
                        else:
                            # Some age group survived, so a family gets a
                            # story. Not retried: the parent sees their age
                            # or the nearest one, and paying to rewrite a
                            # story that is already going out buys nothing.
                            story = candidate.model_copy(update={"versions": kept})
                            break

            rejections.append({
                "attempt": attempt, "rejected_by": rejected_by, "problems": problems[:5],
            })
            if attempt >= attempts_allowed:
                break
            # A retry that overruns the window the alert is waiting on turns
            # "no story" into "we're still looking" plus a follow-up that
            # may never arrive — a promise broken is worse than a plain
            # alert. The previous attempt's own duration is the estimate,
            # so this calibrates itself instead of needing a tuned constant.
            spent = monotonic() - attempt_started
            if deadline is not None and monotonic() + spent * 1.2 > deadline:
                diag["retry_skipped"] = "not enough time left in the alert's wait budget"
                log.info(
                    "Story %s: skipping retry, %.1fs left and the last attempt took %.1fs",
                    req.symbol, deadline - monotonic(), spent,
                )
                break
            feedback = problems
            log.info(
                "Story %s rejected by %s; writing it again (attempt %d of %d)",
                req.symbol, rejected_by, attempt + 1, attempts_allowed,
            )

        # How many goes it took, and what each earlier one got wrong. A
        # story that needed two attempts is worth seeing even though it
        # went out fine: a rising retry rate is a prompt problem arriving.
        diag["write_attempts"] = len(rejections) + (1 if story else 0)
        if rejections:
            diag["rejections"] = rejections
        if story is None:
            last = rejections[-1]
            diag["rejected_by"] = last["rejected_by"]
            why = {
                "language_check_shared": "rejected: " + "; ".join(last["problems"]),
                "language_check_all_versions": "every age version failed the language check",
                "reviewer_grown_up_summary": "reviewer rejected the grown-up summary",
                "reviewer_all_versions": "reviewer rejected every age version",
            }.get(last["rejected_by"], "the story was rejected")
            if len(rejections) > 1:
                why += f" ({len(rejections)} attempts)"
            return _failed(settings, usage, why, diag)

        diag["versions_sent"] = sorted(v.age_group for v in story.versions)

        return GenerationResult(
            status="ready",
            story=story,
            facts=used,
            moved_with_market=found.research.moved_with_market,
            model=settings.story_model,
            usage=usage,
            diagnostics=diag,
        )
    except Exception as e:  # noqa: BLE001 — never let a story break an alert
        log.warning("Story generation failed for %s: %s", req.symbol, e)
        diag["rejected_by"] = "exception"
        diag["exception_type"] = type(e).__name__
        return GenerationResult(
            status="failed", model=settings.story_model, usage=usage,
            error=str(e)[:500], key_problem=key_problem_reason(e),
            failed_service=failed_service(e), diagnostics=diag,
        )
