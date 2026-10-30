"""Application settings loaded from environment variables.

All secrets and environment-specific values flow through this module. Code
elsewhere imports `get_settings()` — never `os.environ` directly.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Strongly-typed environment configuration.

    Loaded once at startup via `get_settings()` (LRU-cached).
    """

    # App
    app_env: Literal["development", "production", "test"] = "development"
    app_debug: bool = False
    secret_key: str = Field(default="dev-only-insecure-secret-change-me", min_length=16)
    frontend_base_url: str = "http://localhost:5173"

    # Database
    database_url: str = "sqlite:///./dev.db"

    # Auth
    magic_link_ttl_minutes: int = 15
    email_confirm_ttl_minutes: int = 60
    session_ttl_days: int = 30
    # Limits on user-triggered emails (sign-in links, address confirmations),
    # per address/user: a short wait between sends, plus a cap on bursts.
    # Defaults: 30s between emails, at most 5 per 10 minutes, so the longest
    # wait anyone sees is under 10 minutes.
    email_resend_cooldown_seconds: int = 30
    email_burst_limit: int = 5
    email_burst_window_minutes: int = 10

    # Unsubscribe links live in old emails, so they stay valid for a long time.
    unsubscribe_link_ttl_days: int = 365

    # Rule created automatically when a stock is added to a watchlist, so new
    # stocks alert on big moves without the user setting anything up. Emails
    # when the move is below -N% or above +N%. Set to 0 to disable.
    default_rule_band_pct: float = 1.5

    # Daily watchlist recap: one email per user each trading day after the
    # close, with every watched stock's move and a kids' story about the
    # biggest mover. Users can turn it off in Settings.
    recap_enabled: bool = True
    recap_time_et: str = "16:30"  # after the 16:05 batch saves today's closes
    # Seconds the recap waits for new stories (it's after the close, so it
    # can wait longer than an alert). Late ones follow as a reply.
    recap_story_wait_seconds: int = 300

    # SMTP
    smtp_host: str = "localhost"
    smtp_port: int = 1025
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = "Little Money Monkey <no-reply@example.com>"
    smtp_use_tls: bool = False

    # Batch jobs
    batch_job_times_et: str = "09:35,12:30,16:05"
    batch_jobs_enabled: bool = True

    # CORS — comma-separated list of origins
    cors_origins: str = "http://localhost:5173"

    # Logging & alerting
    alert_receiver: str = ""  # blank → falls back to smtp_username
    alerts_enabled: bool = True
    log_db_persistence: bool = True  # disabled in tests to avoid noise
    log_lifetime: str = "30d"  # "24h" | "7d" | "30d" | "1m" | "90d" | "1y"
    daily_summary_time_et: str = "17:30"  # HH:MM in US/Eastern
    daily_summary_enabled: bool = True

    # Price-history retention — how long to keep DailyClose rows. Pruned daily
    # by `app.jobs.cleanup`. Same units as LOG_LIFETIME (see parse_lifetime).
    price_history_lifetime: str = "365d"

    # ---- Intraday capture ----------------------------------------------------
    # Every INTRADAY_TICK_MINUTES, during US-market hours, we capture the
    # latest price for every watched ticker into `intraday_prices`. The
    # PRICE_CHANGE_RANGE notification rule fires on BOTH the tick-over-tick
    # % AND the % vs. previous trading day's close.
    intraday_ingest_enabled: bool = True
    intraday_tick_minutes: int = 10
    intraday_retention: str = "7d"
    job_runs_retention: str = "30d"
    info_log_lifetime: str = "7d"
    intraday_market_open_et: str = "09:30"   # NYSE regular open
    intraday_market_close_et: str = "16:00"  # NYSE regular close

    # ---- Stock-data provider -------------------------------------------------
    # "yfinance" (default) | "finnhub". Set FINNHUB_API_KEY when using finnhub.
    # Swap providers without code changes if Yahoo blocks scraping again.
    stock_data_provider: str = "yfinance"
    finnhub_api_key: str = ""
    # When using STOCK_DATA_PROVIDER=finnhub the live /quote endpoint is
    # served by Finnhub on the free tier, but Finnhub's /stock/candle
    # endpoint is paid-only since 2024. Daily history therefore routes
    # through Twelve Data — sign up at https://twelvedata.com (free tier:
    # 800 req/day, 8 req/min — ample for ~50 watched tickers).
    twelvedata_api_key: str = ""

    # ---- AI "why it moved" stories ---------------------------------------
    # When a day-over-day PRICE_CHANGE_RANGE alert fires, an AI-written story
    # (for parents to share with their kids) explains the move, based on
    # news from AI_NEWS_DOMAINS. One story per (ticker, day, direction),
    # shared by every user who gets that alert.
    ai_articles_enabled: bool = False
    # Which AI writes the stories: "anthropic" (Claude) or "gemini" (Google).
    # The backup is used for a story when the main one is down, out of quota
    # or its key is rejected (the admin is emailed). Blank = no backup.
    # Note: Gemini's API terms bar services likely to be used by under-18s,
    # so Claude is the default for these kids' stories.
    ai_provider: Literal["gemini", "anthropic"] = "anthropic"
    ai_fallback_provider: Literal["", "gemini", "anthropic"] = ""
    gemini_api_key: str = ""
    anthropic_api_key: str = ""
    # Older single-key setting: used as the main provider's key when that
    # provider's own key above is blank.
    ai_api_key: str = ""
    # Models, comma-separated, at most one per provider (e.g.
    # "claude-haiku-5-5"). A provider with none listed uses its cheapest.
    ai_article_model: str = ""
    # True while your Gemini project has no billing account (free tier), so
    # cost estimates show $0 for Gemini. Set false if you enable billing.
    ai_gemini_free_tier: bool = True
    # Where news comes from: "web_search" (Claude's web search on
    # AI_NEWS_DOMAINS, $0.01 per search; Anthropic only) or "finnhub" (free,
    # uses FINNHUB_API_KEY, but its free plan is licensed for personal use).
    ai_news_source: Literal["finnhub", "web_search"] = "web_search"
    # Finnhub news publishers whose articles count (comma-separated).
    ai_finnhub_sources: str = "Yahoo,CNBC"
    # Web searches per story when AI_NEWS_SOURCE=web_search.
    ai_article_max_searches: int = 2
    # How long the alert waits for its story. If it isn't ready by then the
    # alert goes out with a "still looking" note and the story follows in a
    # reply email when it's done.
    ai_article_wait_seconds: int = 120
    # Promised follow-ups older than this are given up on (e.g. the server
    # restarted mid-generation).
    ai_article_followup_give_up_minutes: int = 20
    ai_article_max_concurrent: int = 4
    # After the API rejects the key (or the account is out of credit), stop
    # calling it for this long; alerts keep going out without stories.
    ai_key_problem_pause_minutes: int = 30
    # Smallest day-over-day move (either way, in %) that gets an AI story;
    # smaller moves get a fixed "small move" note instead (no AI, no cost).
    # Funds (ETFs and indexes) usually move less than single stocks.
    ai_article_min_pct_stock: float = 1.0
    ai_article_min_pct_fund: float = 0.5
    # Cap on new stories per trading day; the biggest moves are done first.
    ai_article_max_per_day: int = 30
    # Big moves (at least this % either way) still get a story after the
    # daily cap is reached, up to twice the cap.
    ai_article_always_pct: float = 5.0
    # Comma-separated. With web_search, news is searched only on these sites,
    # and only their links appear in emails. All free to read and reachable
    # by Claude's web search (checked 2026-10-30; AP, Reuters, MarketWatch,
    # WSJ, NYT, BBC, Barron's and Investopedia block it). Stock-picking sites
    # are left out on purpose: their "buy/sell" advice doesn't belong in
    # kids' stories.
    ai_news_domains: str = "finance.yahoo.com,cnbc.com,cnn.com,axios.com,npr.org,techcrunch.com"
    # News older than this many days doesn't count as today's reason.
    ai_news_max_age_days: int = 2
    # Whole-market benchmark, so a story can say "most stocks fell today".
    ai_market_symbol: str = "SPY"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @property
    def recap_time(self) -> tuple[int, int]:
        h, m = self.recap_time_et.split(":")
        return int(h), int(m)

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def ai_news_domain_list(self) -> list[str]:
        return [d.strip().lower() for d in self.ai_news_domains.split(",") if d.strip()]

    @property
    def provider_chain(self) -> list[str]:
        """Main provider first, then the backup (if set and different)."""
        chain = [self.ai_provider]
        if self.ai_fallback_provider and self.ai_fallback_provider != self.ai_provider:
            chain.append(self.ai_fallback_provider)
        return chain

    def key_for(self, provider: str) -> str:
        own = self.gemini_api_key if provider == "gemini" else self.anthropic_api_key
        if not own.strip() and provider == self.ai_provider:
            own = self.ai_api_key
        return own.strip()

    def model_for(self, provider: str) -> str:
        default, prefix = {
            "anthropic": ("claude-haiku-5-5", "claude-"),
            "gemini": ("gemini-3.1-flash-lite", "gemini-"),
        }[provider]
        listed = [m.strip() for m in self.ai_article_model.split(",")]
        return next((m for m in listed if m.startswith(prefix)), default)

    def news_source_for(self, provider: str) -> str:
        """Web search is a Claude tool, so Gemini always uses Finnhub news."""
        return "web_search" if self.ai_news_source == "web_search" and provider == "anthropic" else "finnhub"

    @property
    def story_model(self) -> str:
        return self.model_for(self.ai_provider)

    @property
    def story_link_domains(self) -> list[str]:
        """Sites a story may cite and link to, for any provider in use.
        Finnhub links go through its redirect (finnhub.io/api/news?id=...)."""
        sources = {self.news_source_for(p) for p in self.provider_chain}
        domains = ["finnhub.io"] if "finnhub" in sources else []
        return domains + (self.ai_news_domain_list if "web_search" in sources else [])

    @property
    def ai_finnhub_source_list(self) -> list[str]:
        return [x.strip().lower() for x in self.ai_finnhub_sources.split(",") if x.strip()]

    @property
    def effective_alert_receiver(self) -> str:
        """Where admin alerts get sent. Falls back to the SMTP username."""
        return (self.alert_receiver or self.smtp_username).strip()

    @property
    def daily_summary_time(self) -> tuple[int, int]:
        h, m = self.daily_summary_time_et.split(":")
        return int(h), int(m)

    @property
    def intraday_market_open(self) -> tuple[int, int]:
        h, m = self.intraday_market_open_et.split(":")
        return int(h), int(m)

    @property
    def intraday_market_close(self) -> tuple[int, int]:
        h, m = self.intraday_market_close_et.split(":")
        return int(h), int(m)

    @property
    def batch_times(self) -> list[tuple[int, int]]:
        """Parse BATCH_JOB_TIMES_ET into [(hour, minute), ...]."""
        out: list[tuple[int, int]] = []
        for raw in self.batch_job_times_et.split(","):
            raw = raw.strip()
            if not raw:
                continue
            h, m = raw.split(":")
            out.append((int(h), int(m)))
        return out

    def get_database_url(self) -> str:
        """Return database URL with psycopg driver for PostgreSQL."""
        url = self.database_url
        # Convert postgresql:// to postgresql+psycopg:// for psycopg v3
        if url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+psycopg://", 1)
        return url


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached Settings instance.

    Tests can call `get_settings.cache_clear()` after monkeypatching env vars
    to force a reload.
    """
    return Settings()

