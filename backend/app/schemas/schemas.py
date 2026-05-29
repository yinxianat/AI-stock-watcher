"""Pydantic request/response schemas.

Schemas live separately from ORM models so we can shape the public API
independently from the database (e.g. hide internal IDs, rename fields,
validate inputs).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator

from app.models import NotificationEventType, TickerType


class _ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------- Auth ----------


class LoginRequest(BaseModel):
    email: EmailStr


class MagicLinkVerify(BaseModel):
    token: str = Field(min_length=20, max_length=2000)


class SessionResponse(BaseModel):
    session_token: str
    expires_at: datetime
    user: UserOut


# ---------- User ----------


class UserOut(_ORM):
    id: int
    email: EmailStr
    notify_email: EmailStr
    notify_email_confirmed: bool


class UpdateNotifyEmailRequest(BaseModel):
    new_email: EmailStr


# ---------- Ticker ----------


class TickerOut(_ORM):
    id: int
    symbol: str
    name: str
    type: TickerType
    exchange: str | None = None


# ---------- Watchlist ----------


class AddWatchRequest(BaseModel):
    ticker_id: int | None = None
    symbol: str | None = Field(default=None, max_length=20)

    @model_validator(mode="after")
    def _require_one(self) -> AddWatchRequest:
        if self.ticker_id is None and not self.symbol:
            raise ValueError("Either ticker_id or symbol is required.")
        return self


class WatchlistItemOut(_ORM):
    id: int
    ticker: TickerOut


# ---------- Notification rules ----------


class RuleIn(BaseModel):
    ticker_id: int
    event_type: NotificationEventType
    pct_low: float | None = Field(default=None, ge=-100, le=100)
    pct_high: float | None = Field(default=None, ge=-100, le=100)
    enabled: bool = True

    @model_validator(mode="after")
    def _range_sanity(self) -> RuleIn:
        if self.event_type == NotificationEventType.PRICE_CHANGE_RANGE:
            if self.pct_low is None or self.pct_high is None:
                raise ValueError("pct_low and pct_high are required for PRICE_CHANGE_RANGE.")
            if self.pct_low >= self.pct_high:
                raise ValueError("pct_low must be strictly less than pct_high.")
        return self


class RuleOut(_ORM):
    id: int
    ticker_id: int
    event_type: NotificationEventType
    pct_low: float | None
    pct_high: float | None
    enabled: bool


# ---------- Snapshots / trends (read-only) ----------


class PriceSnapshotOut(_ORM):
    ticker_id: int
    price: float
    previous_price: float | None
    week_low: float | None
    week_high: float | None
    month_low: float | None
    month_high: float | None
    quarter_low: float | None
    quarter_high: float | None
    year_low: float | None
    year_high: float | None
    captured_at: datetime


class TrendOut(_ORM):
    ticker_id: int
    price: float
    pct_change: float
    is_week_low: bool
    is_week_high: bool
    is_month_low: bool
    is_month_high: bool
    is_quarter_low: bool
    is_quarter_high: bool
    is_year_low: bool
    is_year_high: bool
    computed_at: datetime


# Resolve forward refs
SessionResponse.model_rebuild()
