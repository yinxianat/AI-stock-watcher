"""SQLAlchemy ORM models.

Schema overview
---------------
User           — registered/login record. Email is the identity.
LoginToken     — single-use magic-link token (hash stored, plain emailed).
Session        — server-side session row keyed by hashed token.
ConfirmationToken — pending email-address change confirmation.
Ticker         — catalog of symbols we know about (seeded + user-added).
WatchlistItem  — many-to-many of users -> tickers.
NotificationRule — per (user, ticker) rule(s) defining when to alert.
DailyClose     — durable per-day closing price per ticker. SOURCE OF TRUTH
                  for daily history; retention bounded by PRICE_HISTORY_LIFETIME
                  (default 365 days). Pruned daily by `app.jobs.cleanup`.
IntradayPrice  — high-frequency price ticks (every INTRADAY_TICK_MINUTES, default
                  10 min) during US market hours. Drives the tick-over-tick
                  PRICE_CHANGE_RANGE notification path. Retention bounded by
                  INTRADAY_RETENTION (default 7 days).
PriceSnapshot  — derived "current view" per ticker, replaced each batch run.
                  `previous_price` is the prior TRADING DAY's close (sourced
                  from DailyClose), so `pct_change` is meaningful day-over-day.
                  `week_low`/`month_low`/... are the prior-window EXTREMES
                  EXCLUDING today — so `today.price < snapshot.week_low` means
                  today's close set a *new* weekly low (strict).
TrendAnalysis  — per (user, ticker) booleans derived from PriceSnapshot.
                  `is_X_low/high` = strict "today set a new X-period extreme".
                  Replaced each compute run.
NotificationLog — audit trail of sent notifications (used to suppress dupes).
LogEntry       — durable application log (WARNING+), auto-pruned per LOG_LIFETIME.
"""

from __future__ import annotations

import enum
from datetime import date as date_, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    """Naive-UTC timestamp.

    We intentionally use naive datetimes app-wide because SQLite cannot store
    tz info, and mixing naive/aware datetimes leads to comparison errors. Treat
    every datetime in this codebase as UTC.
    """
    return datetime.utcnow()


class Base(DeclarativeBase):
    pass


# ---------- Enums ----------


class TickerType(str, enum.Enum):
    STOCK = "stock"
    ETF = "etf"
    INDEX = "index"


class NotificationEventType(str, enum.Enum):
    """Kinds of trigger the user can opt into per ticker."""

    # Price moved outside [pct_low, pct_high] vs. previous snapshot.
    PRICE_CHANGE_RANGE = "price_change_range"
    # Today's price made a new low/high for the period.
    WEEK_LOW = "week_low"
    WEEK_HIGH = "week_high"
    MONTH_LOW = "month_low"
    MONTH_HIGH = "month_high"
    QUARTER_LOW = "quarter_low"
    QUARTER_HIGH = "quarter_high"
    YEAR_LOW = "year_low"
    YEAR_HIGH = "year_high"


# ---------- Identity ----------


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False, index=True)
    # `notify_email` is what we send alerts TO. May differ from login email
    # but only after the user confirms it via ConfirmationToken.
    notify_email: Mapped[str] = mapped_column(String(320), nullable=False)
    notify_email_confirmed: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)

    watchlist: Mapped[list[WatchlistItem]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    rules: Mapped[list[NotificationRule]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class LoginToken(Base):
    """Single-use, time-limited magic-link token.

    We store only the HASH of the token, never the plaintext — even a full
    DB dump cannot be replayed against the auth endpoint.
    """

    __tablename__ = "login_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)


class ConfirmationToken(Base):
    """Pending notify-email change requiring user confirmation."""

    __tablename__ = "confirmation_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    new_email: Mapped[str] = mapped_column(String(320), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)


# ---------- Catalog ----------


class Ticker(Base):
    __tablename__ = "tickers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    type: Mapped[TickerType] = mapped_column(Enum(TickerType), nullable=False)
    exchange: Mapped[str | None] = mapped_column(String(40), nullable=True)
    is_seeded: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    __table_args__ = (
        Index("ix_tickers_symbol_lower", "symbol"),
    )


# ---------- User preferences ----------


class WatchlistItem(Base):
    __tablename__ = "watchlist_items"
    __table_args__ = (UniqueConstraint("user_id", "ticker_id", name="uq_watchlist_user_ticker"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    ticker_id: Mapped[int] = mapped_column(
        ForeignKey("tickers.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=utcnow)

    user: Mapped[User] = relationship(back_populates="watchlist")
    ticker: Mapped[Ticker] = relationship()


class NotificationRule(Base):
    """A trigger rule scoped to (user, ticker, event_type).

    For PRICE_CHANGE_RANGE events, `pct_low` and `pct_high` define the band
    in percent vs. the previous snapshot. Movement outside the band fires.
    Other event types ignore those fields.
    """

    __tablename__ = "notification_rules"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "ticker_id", "event_type", name="uq_rule_user_ticker_event"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    ticker_id: Mapped[int] = mapped_column(
        ForeignKey("tickers.id", ondelete="CASCADE"), index=True
    )
    event_type: Mapped[NotificationEventType] = mapped_column(
        Enum(NotificationEventType), nullable=False
    )
    pct_low: Mapped[float | None] = mapped_column(Float, nullable=True)
    pct_high: Mapped[float | None] = mapped_column(Float, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=utcnow, onupdate=utcnow
    )

    user: Mapped[User] = relationship(back_populates="rules")
    ticker: Mapped[Ticker] = relationship()


# ---------- Batch outputs ----------


class IntradayPrice(Base):
    """One row per (ticker, capture timestamp) intraday tick.

    Written every INTRADAY_TICK_MINUTES (default 10) during US market hours
    by `app.jobs.intraday.run_intraday_capture`. The notification path for
    PRICE_CHANGE_RANGE rules compares each new tick against the prior tick
    for the same ticker — tick-over-tick % change.

    Retention is bounded by `INTRADAY_RETENTION` (default 7 days); the
    cleanup job prunes older rows.
    """

    __tablename__ = "intraday_prices"
    __table_args__ = (
        Index("ix_intraday_prices_ticker_captured", "ticker_id", "captured_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker_id: Mapped[int] = mapped_column(
        ForeignKey("tickers.id", ondelete="CASCADE"), nullable=False
    )
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=utcnow, nullable=False
    )
    price: Mapped[float] = mapped_column(Float, nullable=False)

    ticker: Mapped[Ticker] = relationship()


class DailyClose(Base):
    """One row per (ticker, trading day) — durable price history.

    This is the source of truth. `PriceSnapshot` is derived from it. Retention
    is bounded by the `PRICE_HISTORY_LIFETIME` setting (default 365 days);
    `app.jobs.cleanup` prunes older rows daily.

    Idempotent upsert key: (ticker_id, date). Ingest writes ON CONFLICT UPDATE
    so corporate-action adjustments (splits/dividends) from yfinance correctly
    rewrite historical values on re-fetch.
    """

    __tablename__ = "daily_closes"
    __table_args__ = (
        UniqueConstraint("ticker_id", "date", name="uq_daily_close_ticker_date"),
        Index("ix_daily_close_ticker_date", "ticker_id", "date"),
        Index("ix_daily_close_date", "date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker_id: Mapped[int] = mapped_column(
        ForeignKey("tickers.id", ondelete="CASCADE"), nullable=False
    )
    date: Mapped[date_] = mapped_column(Date, nullable=False)
    close: Mapped[float] = mapped_column(Float, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=utcnow, nullable=False
    )

    ticker: Mapped[Ticker] = relationship()


class PriceSnapshot(Base):
    """Derived current-view per ticker. Replaced each batch run.

    Field semantics (changed in the Option-B redesign):

    * `price` — most recent close from yfinance (may be today's partial bar
      during market hours; finalised at the 16:05 batch).
    * `previous_price` — the prior TRADING DAY's close, sourced from
      `daily_closes`. This makes `pct_change` mean "day over day", not
      "since the last 3-hour batch tick".
    * `week_low`/`week_high`/`month_low`/.../`year_high` — min/max close
      over the prior window EXCLUDING today. So `price < week_low` means
      today's close is *strictly* lower than every close in the prior 7 days
      = a new weekly low.
    """

    __tablename__ = "price_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker_id: Mapped[int] = mapped_column(
        ForeignKey("tickers.id", ondelete="CASCADE"), unique=True, index=True
    )
    price: Mapped[float] = mapped_column(Float, nullable=False)
    previous_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    week_low: Mapped[float | None] = mapped_column(Float, nullable=True)
    week_high: Mapped[float | None] = mapped_column(Float, nullable=True)
    month_low: Mapped[float | None] = mapped_column(Float, nullable=True)
    month_high: Mapped[float | None] = mapped_column(Float, nullable=True)
    quarter_low: Mapped[float | None] = mapped_column(Float, nullable=True)
    quarter_high: Mapped[float | None] = mapped_column(Float, nullable=True)
    year_low: Mapped[float | None] = mapped_column(Float, nullable=True)
    year_high: Mapped[float | None] = mapped_column(Float, nullable=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=utcnow)

    ticker: Mapped[Ticker] = relationship()


class TrendAnalysis(Base):
    """Per-user, per-ticker trend metrics. Cleared & re-populated each run."""

    __tablename__ = "trend_analyses"
    __table_args__ = (
        UniqueConstraint("user_id", "ticker_id", name="uq_trend_user_ticker"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    ticker_id: Mapped[int] = mapped_column(
        ForeignKey("tickers.id", ondelete="CASCADE"), index=True
    )
    price: Mapped[float] = mapped_column(Float, nullable=False)
    pct_change: Mapped[float] = mapped_column(Float, nullable=False)
    is_week_low: Mapped[bool] = mapped_column(Boolean, default=False)
    is_week_high: Mapped[bool] = mapped_column(Boolean, default=False)
    is_month_low: Mapped[bool] = mapped_column(Boolean, default=False)
    is_month_high: Mapped[bool] = mapped_column(Boolean, default=False)
    is_quarter_low: Mapped[bool] = mapped_column(Boolean, default=False)
    is_quarter_high: Mapped[bool] = mapped_column(Boolean, default=False)
    is_year_low: Mapped[bool] = mapped_column(Boolean, default=False)
    is_year_high: Mapped[bool] = mapped_column(Boolean, default=False)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=utcnow)


class NotificationLog(Base):
    """Audit log of notifications actually dispatched.

    Used to suppress duplicate sends within the same batch window.
    """

    __tablename__ = "notification_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    ticker_id: Mapped[int] = mapped_column(
        ForeignKey("tickers.id", ondelete="CASCADE"), index=True
    )
    event_type: Mapped[NotificationEventType] = mapped_column(
        Enum(NotificationEventType), nullable=False
    )
    sent_to: Mapped[str] = mapped_column(String(320), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=utcnow, index=True
    )


# ---------- Observability ----------


class JobRun(Base):
    """Structured audit record for every scheduled job execution.

    One row per job invocation. Replaces the [AUDIT] WARNING-level log hack
    with queryable, structured data. Pruned by the cleanup job (30d default,
    configurable via JOB_RUNS_RETENTION).

    `result_summary` is a short human-readable string (e.g.
    "captured=4/4, emails_sent=1"). `tables_updated` is a comma-separated
    list of DB tables modified during this run.
    """

    __tablename__ = "job_runs"
    __table_args__ = (
        Index("ix_job_runs_job_name_started", "job_name", "started_at"),
        Index("ix_job_runs_started_at", "started_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_name: Mapped[str] = mapped_column(String(60), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)  # SUCCESS / FAILED / SKIPPED
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=utcnow, nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=False), nullable=True
    )
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    result_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    tables_updated: Mapped[str | None] = mapped_column(String(200), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class LogEntry(Base):
    """Durable application log row.

    Persisted by `DBLogHandler` for WARNING+ records. Pruned by the daily
    cleanup job per `LOG_LIFETIME`. Queryable from the admin tooling.
    """

    __tablename__ = "log_entries"
    __table_args__ = (
        Index("ix_log_entries_created_level", "created_at", "level"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), default=utcnow, nullable=False
    )
    level: Mapped[str] = mapped_column(String(10), nullable=False)
    logger: Mapped[str] = mapped_column(String(160), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    exc_info: Mapped[str | None] = mapped_column(Text, nullable=True)
    context: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON
