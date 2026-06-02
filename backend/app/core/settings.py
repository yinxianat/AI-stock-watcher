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
    def effective_alert_receiver(self) -> str:
        """Where admin alerts get sent. Falls back to the SMTP username."""
        return (self.alert_receiver or self.smtp_username).strip()

    @property
    def daily_summary_time(self) -> tuple[int, int]:
        h, m = self.daily_summary_time_et.split(":")
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

