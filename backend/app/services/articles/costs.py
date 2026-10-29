"""Cost estimates for AI stories, so the admin can keep an eye on billing.

Prices are USD per 1M tokens (input, output), checked 2026-10-29 against
anthropic.com / ai.google.dev pricing. Update `PRICES` when they change; a
model missing here shows as "unknown price" rather than a wrong number.
Gemini's free tier costs nothing (AI_GEMINI_FREE_TIER=true).
"""

from __future__ import annotations

from app.core.settings import Settings, get_settings

PRICES: dict[str, tuple[float, float]] = {
    "claude-haiku-5-5": (0.10, 0.50),  # prompts up to 100K tokens (ours are ~10-30K)
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-3.1-flash-lite": (0.25, 1.50),
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-3.6-flash": (0.75, 3.75),  # doubles on 2027-01-01
    "gemini-3.8-flash": (0.75, 3.75),  # doubles on 2027-01-01
}
WEB_SEARCH_USD = 0.01  # Claude web search, per search
TRADING_DAYS_PER_MONTH = 21

# Typical tokens for one story (research + 4 age versions + review),
# including thinking. Web search pulls whole result pages into the prompt.
_TYPICAL = {"finnhub": (12_000, 6_000), "web_search": (30_000, 6_000)}


def is_free(model: str, settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    return model.startswith("gemini-") and settings.ai_gemini_free_tier


def token_cost(model: str, input_tokens: int, output_tokens: int,
               settings: Settings | None = None) -> float | None:
    """USD for these tokens, or None if the model's price isn't known."""
    if is_free(model, settings):
        return 0.0
    price = PRICES.get(model)
    if price is None:
        return None
    return input_tokens / 1e6 * price[0] + output_tokens / 1e6 * price[1]


def story_estimate(provider: str, settings: Settings | None = None) -> float | None:
    """Typical USD for one story written by this provider."""
    settings = settings or get_settings()
    source = settings.news_source_for(provider)
    tokens_in, tokens_out = _TYPICAL[source]
    cost = token_cost(settings.model_for(provider), tokens_in, tokens_out, settings)
    if cost is None:
        return None
    if source == "web_search":
        cost += settings.ai_article_max_searches * WEB_SEARCH_USD
    return cost


def max_stories_per_day(settings: Settings | None = None) -> int:
    settings = settings or get_settings()
    cap = settings.ai_article_max_per_day
    return cap * 2 if settings.ai_article_always_pct > 0 else cap


def max_daily_cost(settings: Settings | None = None) -> float | None:
    """Worst case for a day: every allowed story, each on the priciest
    provider in use (e.g. all on the backup while the main one is down)."""
    settings = settings or get_settings()
    per_story = [story_estimate(p, settings) for p in settings.provider_chain]
    if any(c is None for c in per_story):
        return None
    return max_stories_per_day(settings) * max(per_story)


def money(usd: float | None) -> str:
    if usd is None:
        return "unknown price"
    if usd == 0:
        return "$0 (free)"
    if usd < 0.01:
        return f"${usd:.4f}"
    return f"${usd:.3f}" if usd < 0.10 else f"${usd:.2f}"
