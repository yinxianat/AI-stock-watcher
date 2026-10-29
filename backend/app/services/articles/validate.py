"""Plain-Python checks between the AI stages. No AI here, on purpose.

Facts must cite a page the web search really returned, on an allowed news
site, published recently. Stories must stick to those facts and must not
contain numbers, links or investment advice (numbers and links are added by
our own template, from our own data).
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from urllib.parse import urlsplit

from app.services.articles.schemas import Fact, Story

_MONEY_OR_PERCENT = re.compile(r"[$%€£]|\bpercent\b", re.IGNORECASE)
# Not ".com": company names like "Amazon.com" are fine in a story.
_LINK = re.compile(r"https?://|www\.", re.IGNORECASE)
_ADVICE = re.compile(
    r"\b(you should|time to|go) (buy|sell|invest)|\b(buy|sell) (now|today)\b"
    r"|\bprice target\b|\bguaranteed?\b|\bcan'?t lose\b|\brisk[- ]free\b",
    re.IGNORECASE,
)
_STORY_TEXT_FIELDS = (
    "headline", "story", "lesson_title", "lesson", "talk_together", "grown_up_summary",
)
_MAX_LEN = {
    "headline": 90, "story": 600, "lesson_title": 60, "lesson": 300,
    "talk_together": 200, "grown_up_summary": 400,
}


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


def validate_facts(
    facts: list[Fact],
    *,
    seen_urls: list[str],
    allowed_domains: list[str],
    today: date,
    max_age_days: int,
) -> list[Fact]:
    """Keep only facts that can be trusted to be real, relevant and recent."""
    seen = {normalize_url(u) for u in seen_urls}
    oldest = today - timedelta(days=max_age_days)
    kept: list[Fact] = []
    for fact in facts:
        if not fact.claim.strip() or not fact.quote.strip():
            continue
        if not is_allowed_url(fact.source_url, allowed_domains):
            continue
        if normalize_url(fact.source_url) not in seen:
            continue
        # Undated pages can't be shown to be about today's move.
        if fact.published is None or not (oldest <= fact.published <= today + timedelta(days=1)):
            continue
        kept.append(fact)
    return kept


def story_problems(story: Story, facts: list[Fact]) -> list[str]:
    """Reasons the story can't be sent. Empty list means it's fine."""
    problems: list[str] = []
    fact_ids = {f.id for f in facts}
    if not story.fact_ids:
        problems.append("no facts cited")
    unknown = [i for i in story.fact_ids if i not in fact_ids]
    if unknown:
        problems.append(f"cites unknown facts {unknown}")
    for name in _STORY_TEXT_FIELDS:
        text = getattr(story, name)
        if not text.strip():
            problems.append(f"{name} is empty")
        if len(text) > _MAX_LEN[name]:
            problems.append(f"{name} too long")
        if _MONEY_OR_PERCENT.search(text):
            problems.append(f"{name} contains a number about money")
        if _LINK.search(text):
            problems.append(f"{name} contains a link")
        if _ADVICE.search(text):
            problems.append(f"{name} sounds like investment advice")
    return problems
