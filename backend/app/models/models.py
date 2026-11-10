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
                  30 min) during US market hours. The day-over-day
                  PRICE_CHANGE_RANGE alerts run on each tick. Retention bounded by
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
RecapOptOut    — user who turned the daily watchlist recap off.
HoldingsOptIn  — user who turned purchase-price comparison ON (off by default).
Holding        — what one user paid for one ticker, and optionally how many
                  shares. Financial data about the user: never logged, never
                  sent to the AI pipeline, hidden from Data Lens unless
                  DATA_LENS_SHOW_HOLDINGS says otherwise.
RecapSend      — one row per (user, trading day) recap sent; stops repeats.
MoneyIdeaSend  — which Money Idea of the Day each user got, per trading day.
ContentFeedback — parent like/neutral/dislike on AI stories and Money Ideas.
DeliveryOutcome — what one user received in one email and why, recorded by
                  the sending job rather than inferred later.
"""

from __future__ import annotations

import enum
from datetime import date as date_, datetime, timezone

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
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class UTCDateTime(TypeDecorator):
    """A datetime column that always hands back an aware UTC value.

    Timestamps are STORED exactly as before — naive, in UTC — so the column
    type on disk is unchanged and no migration is needed. What changes is
    what Python sees: every read carries `tzinfo=UTC`, so a timestamp can no
    longer leave this application without saying which timezone it is in.
    That matters most for the log and monitoring tables, where the reader is
    a human at 10am wondering whether "15:02" was three hours ago or five.

    Why not `DateTime(timezone=True)`, the obvious answer? On Postgres it
    gives a real `timestamptz`, but on SQLite the offset is silently
    discarded and the value comes back naive *and shifted*, so tests would
    exercise different semantics from production — the worst of both. This
    type behaves identically on both backends.

    Binding is deliberately forgiving: an aware value is converted to UTC, a
    naive one is trusted as UTC already. That keeps the thousands of
    existing naive comparisons in queries working untouched while new code
    can pass aware values.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value  # already naive UTC, by the convention above
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


def utcnow() -> datetime:
    """Timezone-aware UTC timestamp. The only clock this app should use.

    Aware, not naive: a bare datetime that has to be *documented* as UTC
    gets misread eventually, and the one that bit us was a monitoring
    timestamp nobody could place. `UTCDateTime` stores these as naive UTC,
    so the storage convention is unchanged — only the values carry their
    timezone now.

    Everything in this codebase is UTC until it is deliberately converted
    for display; US/Eastern appears only in the market-hours logic and in
    what a human is shown.
    """
    return datetime.now(timezone.utc)


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
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

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
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class ConfirmationToken(Base):
    """Pending notify-email change requiring user confirmation."""

    __tablename__ = "confirmation_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    new_email: Mapped[str] = mapped_column(String(320), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


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
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

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
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow
    )

    user: Mapped[User] = relationship(back_populates="rules")
    ticker: Mapped[Ticker] = relationship()


class EmailOptOut(Base):
    """A user who unsubscribed from stock-alert emails.

    A row's presence means "don't send alerts"; deleting it resubscribes.
    Sign-in links and email-confirmation emails are transactional and are
    still sent. Kept as its own table (not a `users` column) because startup
    only runs `create_all`, which creates missing tables but never adds
    columns to existing ones.
    """

    __tablename__ = "email_opt_outs"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class RecapOptOut(Base):
    """A user who turned the daily watchlist recap email off.

    No row means the recap is on (the default). Own table for the same
    reason as `EmailOptOut`. Turning all alert emails off (EmailOptOut)
    stops the recap too.
    """

    __tablename__ = "recap_opt_outs"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class HoldingsOptIn(Base):
    """A user who turned purchase-price comparison on.

    A row's presence means "show me what I'm up or down"; no row means off,
    which is the default — nobody is asked for a purchase price until they
    ask for the feature. The inverse of `EmailOptOut`/`RecapOptOut` for that
    reason, and its own table for the same reason as those: startup only runs
    `create_all`, which creates missing tables but never adds columns to
    existing ones.

    Turning it off hides the comparison but keeps the `Holding` rows, so a
    user who switches it back on does not have to type their prices again.
    """

    __tablename__ = "holdings_opt_ins"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Holding(Base):
    """What one user paid for one ticker.

    `shares` is optional on purpose: the percentage and the per-share
    difference need only a price, and a family that just wants "am I up or
    down" should not have to disclose the size of their position to get it.
    Given shares, the recap and the watchlist can also show the total.

    This is financial data about a real person, so it is treated like
    `notify_email` rather than like a watchlist row: it is never written to a
    log line, never passed to the AI story pipeline, and Data Lens hides
    `purchase_price` and `shares` unless DATA_LENS_SHOW_HOLDINGS is on.

    Keyed by (user, ticker) rather than by watchlist item so a price survives
    a rules edit, and deleted with the watchlist row so re-adding a stock
    never resurrects a price from months ago.
    """

    __tablename__ = "holdings"
    __table_args__ = (
        UniqueConstraint("user_id", "ticker_id", name="uq_holding_user_ticker"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    ticker_id: Mapped[int] = mapped_column(
        ForeignKey("tickers.id", ondelete="CASCADE"), index=True
    )
    # Per share, in the ticker's currency. Always positive — validated at the
    # schema boundary, because a zero would make the percentage meaningless
    # (a division by zero) rather than merely wrong.
    purchase_price: Mapped[float] = mapped_column(Float, nullable=False)
    # None means "I didn't say", not zero. Zero shares would report a $0
    # gain on a real position, which is worse than reporting nothing.
    shares: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow
    )

    ticker: Mapped[Ticker] = relationship()


class RecapSend(Base):
    """A daily watchlist recap that went out. Durable; unique per (user,
    trading day), so a re-run or restart never sends the same day twice."""

    __tablename__ = "recap_sends"
    __table_args__ = (
        UniqueConstraint("user_id", "trading_date", name="uq_recap_user_day"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    trading_date: Mapped[date_] = mapped_column(Date, nullable=False)
    sent_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class MoneyIdeaSend(Base):
    """A Money Idea of the Day sent to a user in their daily recap.

    Durable history: drives each user's progress through the curriculum
    (`app.services.money_ideas`) and the "Money ideas" page where parents
    re-read past ideas with their kids. One per user per trading day.
    """

    __tablename__ = "money_idea_sends"
    __table_args__ = (
        UniqueConstraint("user_id", "trading_date", name="uq_money_idea_user_day"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    idea_key: Mapped[str] = mapped_column(String(40), nullable=False)
    trading_date: Mapped[date_] = mapped_column(Date, nullable=False)
    sent_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class ContentFeedback(Base):
    """Parent feedback on AI stories and Money Ideas.

    One rating per (user, content_type, content_id). The latest rating
    wins; the user can change their mind from the frontend.

    RAG-ready fields:
    * ``age_group`` — the age group the content was rendered for (enables
      filtering in a future retrieval pipeline).
    * ``context_json`` — structured metadata at rating time (ticker,
      move_pct, direction for articles; idea_key, position for curriculum).
      Stored as a JSON string so the RAG pipeline can enrich its
      embeddings without re-joining multiple tables.
    """

    __tablename__ = "content_feedback"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "content_type", "content_id",
            name="uq_feedback_user_content",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    content_type: Mapped[str] = mapped_column(String(20), nullable=False)
    content_id: Mapped[str] = mapped_column(String(80), nullable=False)
    rating: Mapped[str] = mapped_column(String(10), nullable=False)
    age_group: Mapped[str | None] = mapped_column(String(10), nullable=True)
    context_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


# ---------- Batch outputs ----------


class IntradayPrice(Base):
    """One row per (ticker, capture timestamp) intraday tick.

    Written every INTRADAY_TICK_MINUTES (default 30) during US market hours
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
        UTCDateTime, default=utcnow, nullable=False
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
        UTCDateTime, default=utcnow, nullable=False
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
    captured_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

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
    computed_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


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
        UTCDateTime, default=utcnow, index=True
    )



class StockArticle(Base):
    """AI-written "why it moved" story for one stock's move on one day.

    Shared by every user whose alert fired for that move, so each story is
    generated once: unique on (ticker, trading day, direction). Durable,
    unlike the per-batch tables. `status` is pending → ready |
    no_clear_cause | failed. `content` holds the checked facts and the
    story as JSON; the numbers shown in emails come from the price columns
    here, never from the AI.
    """

    __tablename__ = "stock_articles"
    __table_args__ = (
        UniqueConstraint(
            "ticker_id", "trading_date", "direction", name="uq_article_ticker_day_dir"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker_id: Mapped[int] = mapped_column(
        ForeignKey("tickers.id", ondelete="CASCADE"), index=True
    )
    trading_date: Mapped[date_] = mapped_column(Date, nullable=False, index=True)
    direction: Mapped[str] = mapped_column(String(4), nullable=False)  # "up" | "down"
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    move_pct: Mapped[float] = mapped_column(Float, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    prev_close: Mapped[float] = mapped_column(Float, nullable=False)
    market_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    model: Mapped[str | None] = mapped_column(String(60), nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    ticker: Mapped[Ticker] = relationship()


class StoryPreference(Base):
    """Which age groups' stories a parent wants in their alerts.

    No row means the default (ages 6-9). Only the group is stored, never a
    child's age, name or other details.
    """

    __tablename__ = "story_preferences"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    age_groups: Mapped[str] = mapped_column(String(40), nullable=False)  # e.g. "3-5,13-17"
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow
    )


class ArticleFollowup(Base):
    """A user whose alert said "we're still looking", waiting for the story.

    Sent as a reply to the original alert (`in_reply_to` is its Message-ID)
    so mail apps show both in one thread. `status` is waiting → sent |
    abandoned (no story found, or gave up).
    """

    __tablename__ = "article_followups"
    __table_args__ = (
        UniqueConstraint("article_id", "user_id", name="uq_followup_article_user"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    article_id: Mapped[int] = mapped_column(
        ForeignKey("stock_articles.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    in_reply_to: Mapped[str] = mapped_column(String(255), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="waiting")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


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
        UTCDateTime, default=utcnow, nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime, nullable=True
    )
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    result_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    tables_updated: Mapped[str | None] = mapped_column(String(200), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class InfoLog(Base):
    """INFO-level application log row.

    Captured by `InfoLogHandler` for INFO+ records from `app.jobs.*` and
    `app.services.*` loggers only — excludes uvicorn, APScheduler, and other
    framework noise. Pruned by the cleanup job per `INFO_LOG_LIFETIME`
    (default 7 days).
    """

    __tablename__ = "info_logs"
    __table_args__ = (
        Index("ix_info_logs_created_logger", "created_at", "logger"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, nullable=False
    )
    level: Mapped[str] = mapped_column(String(10), nullable=False)
    logger: Mapped[str] = mapped_column(String(160), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)


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
        UTCDateTime, default=utcnow, nullable=False
    )
    level: Mapped[str] = mapped_column(String(10), nullable=False)
    logger: Mapped[str] = mapped_column(String(160), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    exc_info: Mapped[str | None] = mapped_column(Text, nullable=True)
    context: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON


class AdminRole(Base):
    """Grants one user access to the /admin diagnostics and dashboard.

    A row here is additive to `ADMIN_EMAILS`: the env var is the bootstrap
    (it works even before that person has ever signed in, and means losing
    DB access can't lock everyone out), while this table lets a second
    operator be granted access with one INSERT and no deploy.

    `user_id` is the primary key, so one role per user. The `role` column
    exists so a read-only "viewer" can be added later without a migration.
    """

    __tablename__ = "admin_roles"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="admin")
    granted_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class ServiceSession(Base):
    """A short-lived token issued to the monitoring agent.

    The agent exchanges `ADMIN_API_TOKEN` for one of these once per run, so
    the long-lived secret crosses the wire rarely instead of on every
    request — anything that captures a header (a proxy log, an error
    tracker) then holds a credential that dies in minutes rather than one
    valid until rotated.

    Only the SHA-256 digest is stored, matching `sessions` and
    `login_tokens`: a DB dump can't be replayed. `client_net` keeps a /24
    rather than a full IP, which is enough to notice a login from somewhere
    new without storing personal data.
    """

    __tablename__ = "service_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    client_net: Mapped[str] = mapped_column(String(64), nullable=False, default="unknown")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class DeliveryOutcome(Base):
    """What one user actually received in one email, and why.

    Written at the moment the decision is made, by the job that sends the
    email — not inferred afterwards from the surrounding tables. That is the
    whole point: "this alert carried no story because the research found no
    cause it could verify" is a fact at send time, and by the next morning
    it is a guess. `reason` is a code from
    `app.services.delivery.REASONS`; `detail` is the sentence with this
    send's real numbers in it.

    One row per (email, ticker), so an alert naming one stock makes one row
    and a recap makes one row for the mover whose story it carried. Durable:
    there is no retention setting, because the table grows with emails sent
    rather than with time, and it is the only record of what a family saw.
    """

    __tablename__ = "delivery_outcomes"
    __table_args__ = (
        Index("ix_delivery_outcomes_day_reason", "trading_date", "reason"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    # Null for the starter recap, which names no stock.
    ticker_id: Mapped[int | None] = mapped_column(
        ForeignKey("tickers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # SET NULL, not CASCADE: this row is the audit trail. If a story is ever
    # deleted the record that someone received it must survive.
    article_id: Mapped[int | None] = mapped_column(
        ForeignKey("stock_articles.id", ondelete="SET NULL"), nullable=True, index=True
    )
    trading_date: Mapped[date_] = mapped_column(Date, nullable=False, index=True)
    # "alert" | "recap" | "followup" — which email this was.
    channel: Mapped[str] = mapped_column(String(10), nullable=False)
    # What the family saw: "story" | "small_move_note" | "market_day_note"
    # | "pending_followup" | "none".
    story_state: Mapped[str] = mapped_column(String(20), nullable=False)
    reason: Mapped[str] = mapped_column(String(30), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The parent's chosen age group, and the one actually sent. They differ
    # when the chosen group's version failed a safety check and the nearest
    # age was substituted — invisible to the parent, and worth seeing here.
    age_group: Mapped[str | None] = mapped_column(String(10), nullable=True)
    age_group_sent: Mapped[str | None] = mapped_column(String(10), nullable=True)
    sent_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, index=True
    )
class FindingState(Base):
    """An admin's triage decision about one recurring monitoring finding,
    plus how often that finding has been seen.

    Findings are *derived*: `diagnostics.findings()` recomputes them from
    the database on every page load, so there is no row to act on and
    nothing for "I have seen this" to attach to. This table supplies the
    missing identity — one row per `fingerprint`, which is the finding's
    `code` plus the one job or table it names, and is therefore stable
    across recomputation and across days.

    Durable on purpose. It is not named in `app.jobs.cleanup`, so a
    decision outlives the evidence that prompted it.

    Recurrence lives on the same row rather than in a second table.
    `days_seen` answers "is this day 1 or day 14?", which is what decides
    whether a finding is noise or a trend, and keeping it here means there
    is no growing history table to retain. The cost is that there is no
    per-day timeline: "seen on 7 days" is available, "which 7" is not.

    `state` is the admin's last *instruction*, never the effective state.
    An acknowledgement past its `snooze_until`, and a `resolved` row seen
    again afterwards, both read as new again — computed on read in
    `app.services.triage`, so no finding can be permanently silenced by a
    row nobody ever looks at again. That is the difference between a
    dashboard you trust and one you learn to ignore.
    """

    __tablename__ = "finding_states"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # "code" for a finding about the whole system, "code:subject" for one
    # that names a single job or table. Unique: this IS the identity.
    fingerprint: Mapped[str] = mapped_column(String(160), unique=True, nullable=False)
    code: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    subject: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # "new" (observed, never triaged) | "ack" | "working" | "backlog" |
    # "resolved". A row exists as soon as a finding is seen, triaged or
    # not, because recurrence is most worth knowing about the thing nobody
    # has got to yet. Only `app.services.triage.STATES` may be set by a
    # caller; "new" is storage-only.
    state: Mapped[str] = mapped_column(String(12), nullable=False)
    # Worth more than the state itself. "Railway free tier sleeps
    # overnight, known" tells the next reader why it was parked; "ack"
    # alone tells them nothing.
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Eastern calendar days, like every other date column here: these are
    # days on a schedule, not instants. Never compare them to a timestamp.
    snooze_until: Mapped[date_ | None] = mapped_column(Date, nullable=True)
    resolved_on: Mapped[date_ | None] = mapped_column(Date, nullable=True)

    first_seen_date: Mapped[date_] = mapped_column(Date, nullable=False)
    last_seen_date: Mapped[date_] = mapped_column(Date, nullable=False)
    # Distinct days on which this finding was observed, not observations:
    # opening the dashboard five times in a morning must not read as five
    # days of breakage.
    days_seen: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    # The last severity and one-line detail seen, so a triage list can be
    # rendered without recomputing every day's report.
    last_severity: Mapped[str | None] = mapped_column(String(10), nullable=True)
    last_detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    # The user id, not their address: an admin's email is personal data,
    # and this table is served to the monitoring agent.
    updated_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
