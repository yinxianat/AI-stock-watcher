"""Shapes passed between the story pipeline stages."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

Category = Literal[
    "earnings", "outlook", "analyst", "product", "deal", "legal",
    "leadership", "economy", "market_wide", "other",
]
Status = Literal["ready", "no_clear_cause", "failed"]


class Fact(BaseModel):
    """One sourced reason for the move, as reported by the research stage."""

    id: str
    claim: str  # one plain sentence
    quote: str  # short exact text from the source that supports the claim
    source_url: str
    source_name: str
    published: date | None = None


class Research(BaseModel):
    cause_found: bool
    moved_with_market: bool = False
    facts: list[Fact] = Field(default_factory=list)


AgeGroup = Literal["3-5", "6-9", "10-12", "13-17"]
AGE_GROUPS: tuple[str, ...] = ("3-5", "6-9", "10-12", "13-17")
DEFAULT_AGE_GROUPS: tuple[str, ...] = ("6-9",)


class KidVersion(BaseModel):
    """The story told for one age group."""

    age_group: AgeGroup
    headline: str
    story: str
    lesson_title: str
    lesson: str  # one investing idea
    talk_together: str  # a question the parent can ask


class Story(BaseModel):
    """What the writer stage returns. Text only: no numbers, no links.

    One version per age group (all four are written at once, so any parent
    gets their choice whenever their alert fires); the grown-up part, tone
    and sources are shared.
    """

    versions: list[KidVersion]
    grown_up_summary: str
    tone: Literal["playful", "gentle"]
    category: Category
    fact_ids: list[str]

    def version(self, age_group: str) -> KidVersion | None:
        return next((v for v in self.versions if v.age_group == age_group), None)


class Review(BaseModel):
    """The independent AI reviewer's verdict."""

    grown_up_ok: bool
    approved_age_groups: list[AgeGroup]
    problems: list[str] = Field(default_factory=list)


@dataclass
class ArticleRequest:
    """Everything the AI is told about the move. Numbers come from our data."""

    symbol: str
    company: str
    trading_date: date
    direction: Literal["up", "down"]
    move_pct: float
    price: float
    prev_close: float
    market_pct: float | None = None


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
        )


@dataclass
class ResearchResult:
    research: Research
    # Every URL the web search actually returned. A fact citing any other
    # URL was made up (or injected) and is dropped.
    seen_urls: list[str] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)


@dataclass
class WriteResult:
    story: Story
    usage: Usage = field(default_factory=Usage)


@dataclass
class ReviewResult:
    review: Review
    usage: Usage = field(default_factory=Usage)


@dataclass
class GenerationResult:
    status: Status
    story: Story | None = None
    facts: list[Fact] = field(default_factory=list)
    moved_with_market: bool = False
    model: str | None = None
    usage: Usage = field(default_factory=Usage)
    error: str | None = None
    # Set when the AI service refused us because of the key or billing (not
    # a passing outage). Only the admin is told; users just get no story.
    key_problem: str | None = None
    # The service whose error failed this story ("Gemini", "Anthropic",
    # "Finnhub"), so a backup provider can take over. None for content
    # failures (no clear cause, safety checks).
    failed_service: str | None = None
