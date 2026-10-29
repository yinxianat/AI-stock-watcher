"""Research from Finnhub's free company news (AI_NEWS_SOURCE=finnhub).

We fetch the articles ourselves and hand the AI a closed list (headline +
summary each), so no paid web search is needed. The AI only picks which
articles explain the move and quotes them; the code then checks every quote
word for word against its article, and takes the link, publisher and date
from Finnhub's data, never from the AI.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel

from app.core.settings import get_settings
from app.services.articles import prompts
from app.services.articles.providers import AIServiceError, JSONGenerator, strict_schema
from app.services.articles.schemas import ArticleRequest, Fact, Research, ResearchResult, Usage

log = logging.getLogger(__name__)

_URL = "https://finnhub.io/api/v1/company-news"
_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=10.0, pool=10.0)
_ET = ZoneInfo("America/New_York")
_MAX_ARTICLES = 15  # newest first; keeps the prompt small
_SUMMARY_CHARS = 500
_PUBLISHER_NAMES = {"yahoo": "Yahoo Finance", "cnbc": "CNBC"}


@dataclass
class NewsItem:
    id: str
    source: str
    published: date
    headline: str
    summary: str
    url: str


class _NewsFact(BaseModel):
    article_id: str
    claim: str
    quote: str


class NewsFindings(BaseModel):
    cause_found: bool
    moved_with_market: bool
    facts: list[_NewsFact]


FINDINGS_SCHEMA = strict_schema(NewsFindings.model_json_schema())


def fetch_news(symbol: str, day: date) -> list[NewsItem]:
    """Recent articles from the allowed publishers, newest first."""
    settings = get_settings()
    key = settings.finnhub_api_key.strip()
    if not key:
        raise AIServiceError("Finnhub", 401, "FINNHUB_API_KEY is not set")
    start = day - timedelta(days=settings.ai_news_max_age_days)
    try:
        r = httpx.get(
            _URL,
            params={"symbol": symbol, "from": start.isoformat(), "to": day.isoformat(), "token": key},
            timeout=_TIMEOUT,
        )
    except httpx.HTTPError as e:
        raise AIServiceError("Finnhub", None, str(e)) from e
    if r.status_code != 200:
        raise AIServiceError("Finnhub", r.status_code, r.text[:200])

    allowed = settings.ai_finnhub_source_list
    rows = [a for a in r.json() if isinstance(a, dict)]
    rows = [a for a in rows if str(a.get("source", "")).lower() in allowed]
    rows.sort(key=lambda a: a.get("datetime") or 0, reverse=True)
    items: list[NewsItem] = []
    for a in rows:
        if not a.get("headline") or not str(a.get("url", "")).startswith("https://"):
            continue
        published = datetime.fromtimestamp(a.get("datetime") or 0, tz=timezone.utc).astimezone(_ET).date()
        source = str(a["source"])
        items.append(NewsItem(
            id=f"a{len(items) + 1}",
            source=_PUBLISHER_NAMES.get(source.lower(), source),
            published=published,
            headline=str(a["headline"]).strip(),
            summary=str(a.get("summary") or "").strip()[:_SUMMARY_CHARS],
            url=str(a["url"]),
        ))
        if len(items) >= _MAX_ARTICLES:
            break
    return items


def _norm(text: str) -> str:
    text = text.lower().replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", text).strip(" .")


def quote_matches(quote: str, item: NewsItem) -> bool:
    """The quote must appear word for word in the article's headline or summary."""
    q = _norm(quote)
    return bool(q) and (q in _norm(item.headline) or q in _norm(item.summary))


def research_from_news(gen: JSONGenerator, req: ArticleRequest) -> ResearchResult:
    items = fetch_news(req.symbol, req.trading_date)
    if not items:
        # Nothing to read: no AI call, no cost.
        return ResearchResult(Research(cause_found=False), seen_urls=[], usage=Usage())

    articles = [
        {"id": i.id, "source": i.source, "date": i.published.isoformat(),
         "headline": i.headline, "summary": i.summary}
        for i in items
    ]
    text, usage = gen.generate(
        prompts.NEWS_RESEARCH_SYSTEM, prompts.news_research_prompt(req, articles), FINDINGS_SCHEMA,
    )
    findings = NewsFindings.model_validate_json(text)
    by_id = {i.id: i for i in items}
    facts: list[Fact] = []
    for f in findings.facts:
        item = by_id.get(f.article_id)
        if item is None or not quote_matches(f.quote, item):
            log.info("Story %s: dropped a fact whose quote isn't in its article", req.symbol)
            continue
        facts.append(Fact(
            id=f"f{len(facts) + 1}", claim=f.claim, quote=f.quote,
            source_url=item.url, source_name=item.source, published=item.published,
        ))
    return ResearchResult(
        Research(cause_found=findings.cause_found,
                 moved_with_market=findings.moved_with_market, facts=facts),
        seen_urls=[i.url for i in items],
        usage=usage,
    )
