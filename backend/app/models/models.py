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
PriceSnapshot  — most-recent batch ingest row. Replaced each batch run.
TrendAnalysis  — computed trend metrics per watched (user, ticker). Replaced
                  each compute run.
NotificationLog — audit trail of sent notifications (used to suppress dupes).
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    Boolean,
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


class PriceSnapshot(Base):
    """Most-recent batch ingest. Cleared & re-populated every run."""

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
