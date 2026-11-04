"""The write and review stages, for whichever provider is configured."""

from __future__ import annotations

from app.services.articles import prompts
from app.services.articles.providers import JSONGenerator, strict_schema
from app.services.articles.schemas import (
    ArticleRequest,
    Fact,
    Review,
    ReviewResult,
    Story,
    WriteResult,
)

STORY_SCHEMA = strict_schema(Story.model_json_schema())
REVIEW_SCHEMA = strict_schema(Review.model_json_schema())


def write(gen: JSONGenerator, req: ArticleRequest, facts: list[Fact],
          moved_with_market: bool, rejection: list[str] | None = None) -> WriteResult:
    """Write the story. `rejection` is why the last attempt was thrown out,
    passed only on a retry so the rewrite is aimed rather than blind."""
    text, usage = gen.generate(
        prompts.WRITER_SYSTEM,
        prompts.writer_prompt(req, facts, moved_with_market, rejection),
        STORY_SCHEMA,
    )
    return WriteResult(Story.model_validate_json(text), usage)


def review(gen: JSONGenerator, req: ArticleRequest, facts: list[Fact], story: Story) -> ReviewResult:
    text, usage = gen.generate(
        prompts.REVIEW_SYSTEM, prompts.review_prompt(req, facts, story), REVIEW_SCHEMA,
    )
    return ReviewResult(Review.model_validate_json(text), usage)
