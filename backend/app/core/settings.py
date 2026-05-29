"""Application settings loaded from environment variables.

All secrets and environment-specific values flow through this module. Code
elsewhere imports `get_settings()` — never `os.environ` directly.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import EmailStr, Field
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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached Settings instance.

    Tests can call `get_settings.cache_clear()` after monkeypatching env vars
    to force a reload.
    """
    return Settings()
