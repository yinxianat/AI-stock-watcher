"""Plain-Python checks between the AI stages. No AI here, on purpose.

Facts must cite a page the web search really returned, on an allowed news
site, published recently. Stories must stick to those facts and must not
contain numbers, links or investment advice (numbers and links are added by
our own template, from our own data).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from urllib.parse import urlsplit

from app.services.articles.language import (
    language_problems,
    readability_problems,
    word_count,
)
from app.services.articles.schemas import AGE_GROUPS, Fact, KidVersion, Story

_VERSION_FIELDS = ("headline", "story", "lesson_title", "lesson", "talk_together")


def normalize_url(url: str) -> str:
    """Compare URLs without fragments, trailing slashes or host case."""
    parts = urlsplit(url.strip())
    path = parts.path.rstrip("/")
    query = f"?{parts.query}" if parts.query else ""
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}{path}{query}"


def is_allowed_url(url: str, allowed_domains: list[str]) -> bool:
    """https only, on an allowed site or one of its subdomains."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host:
        return False
    return any(host == d or host.endswith("." + d) for d in allowed_domains)


def site_root_url(url: str) -> str:
    """Publisher homepage for a given article URL (`https://cnbc.com/foo` →
    `https://www.cnbc.com`). We link to the publisher's homepage rather
    than the exact article so a reader never hits a 404 for a page that
    was moved or removed between when the AI found it and when the email
    is opened."""
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    return f"https://{host}" if host else url


@dataclass(frozen=True)
class DroppedFact:
    """One fact that failed a check, and which check it was.

    This used to be a bare `continue`. The monitoring dashboard was then
    left telling an admin that validation "may have" dropped the facts,
    offering three possibilities, when this function knew exactly which
    rule fired and on what. Carrying the rule out costs nothing and turns a
    guess into a fact.
    """

    fact_id: str
    rule: str
    detail: str


@dataclass
class FactCheck:
    """The surviving facts, and why each of the others went."""

    kept: list[Fact]
    dropped: list[DroppedFact]

    def rule_counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for d in self.dropped:
            out[d.rule] = out.get(d.rule, 0) + 1
        return out


def validate_facts(
    facts: list[Fact],
    *,
    seen_urls: list[str],
    allowed_domains: list[str],
    today: date,
    max_age_days: int,
) -> FactCheck:
    """Keep only facts that can be trusted to be real, relevant and recent.

    Returns the kept facts and a record of every rejection, so a story that
    ends up with nothing can say which check emptied it.
    """
    seen = {normalize_url(u) for u in seen_urls}
    oldest = today - timedelta(days=max_age_days)
    kept: list[Fact] = []
    dropped: list[DroppedFact] = []

    def drop(fact: Fact, rule: str, detail: str) -> None:
        dropped.append(DroppedFact(fact_id=fact.id, rule=rule, detail=detail))

    for fact in facts:
        if not fact.claim.strip() or not fact.quote.strip():
            drop(fact, "empty_claim_or_quote", "the claim or the supporting quote was blank")
            continue
        if not is_allowed_url(fact.source_url, allowed_domains):
            host = urlsplit(fact.source_url.strip()).hostname or fact.source_url
            drop(fact, "domain_not_allowed",
                 f"{host} is not in AI_NEWS_DOMAINS (or the link was not https)")
            continue
        if normalize_url(fact.source_url) not in seen:
            drop(fact, "url_not_in_search_results",
                 f"{fact.source_url} was never returned by the search — the model "
                 "supplied the link itself")
            continue
        # Undated pages can't be shown to be about today's move.
        if fact.published is None:
            drop(fact, "undated",
                 f"{fact.source_name} gave no publication date, so it cannot be tied "
                 "to today's move")
            continue
        if not (oldest <= fact.published <= today + timedelta(days=1)):
            drop(fact, "outside_date_window",
                 f"published {fact.published}, outside the {max_age_days}-day window "
                 f"ending {today}")
            continue
        kept.append(fact)
    return FactCheck(kept=kept, dropped=dropped)


def shared_problems(story: Story, facts: list[Fact]) -> list[str]:
    """Problems that sink the whole story (every version shares these)."""
    problems: list[str] = []
    fact_ids = {f.id for f in facts}
    if not story.fact_ids:
        problems.append("no facts cited")
    unknown = [i for i in story.fact_ids if i not in fact_ids]
    if unknown:
        problems.append(f"cites unknown facts {unknown}")
    summary = story.grown_up_summary
    if not summary.strip():
        problems.append("grown_up_summary is empty")
    if word_count(summary) > 80:
        problems.append("grown_up_summary too long")
    problems += [f"grown_up_summary {p}" for p in language_problems(summary)]
    if not any(v.age_group in AGE_GROUPS for v in story.versions):
        problems.append("no age-group versions")
    return problems


def version_problems(v: KidVersion) -> list[str]:
    """Problems with one age group's version. Only that version is dropped."""
    problems: list[str] = []
    for name in _VERSION_FIELDS:
        text = getattr(v, name)
        if not text.strip():
            problems.append(f"{name} is empty")
        problems += [f"{name} {p}" for p in language_problems(text, v.age_group)]
    if len(v.headline) > 90:
        problems.append("headline too long")
    if word_count(v.lesson_title) > 8:
        problems.append("lesson_title too long")
    if word_count(v.lesson) > 60:
        problems.append("lesson too long")
    if word_count(v.talk_together) > 40:
        problems.append("talk_together too long")
    problems += readability_problems(v.story, v.age_group)
    return problems
