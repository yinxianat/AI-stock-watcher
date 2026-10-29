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
    default_rule_band_pct: float = 5.0

    # SMTP
    smtp_host: str = "localhost"
    smtp_port: int = 1025
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = "AI Stock Watcher <no-reply@example.com>"
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
    anthropic_api_key: str = ""
    ai_article_model: str = "claude-opus-5-5"
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
    # Cap on new stories per trading day; the biggest moves are done first.
    ai_article_max_per_day: int = 30
    # Comma-separated. News is searched only on these sites, and only their
    # links appear in emails.
    ai_news_domains: str = "finance.yahoo.com,reuters.com"
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
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def ai_news_domain_list(self) -> list[str]:
        return [d.strip().lower() for d in self.ai_news_domains.split(",") if d.strip()]

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

