"""Daily watchlist recap: one email per user after the close.

Every trading day at RECAP_TIME_ET (default 16:30, after the 16:05 batch
has saved today's closes) each user gets their watchlist's day: the whole
market, every stock's move (biggest first), new highs/lows, and a kids'
story about their biggest mover. So users whose alert rules didn't fire
still hear from us, and parents get something to talk about with their
kids every day.

- Market holidays are skipped: no watched stock has a close for today.
- The biggest mover gets an AI story if it moved at least the story bar
  (AI_ARTICLE_MIN_PCT_STOCK / _FUND), otherwise the fixed small-move note.
  Stories are shared with alerts, so one written for an alert today is
  reused. Stories get RECAP_STORY_WAIT_SECONDS; one still being written
  follows as a reply, like alerts.
- Users whose price-change rules stayed quiet today (the move was inside a
  range wider than DEFAULT_RULE_BAND_PCT) get a short "your alert didn't
  go off" section: their range,
  today's move, why a smaller range helps, and a link to change it.
- Not sent to users who turned the recap off, turned all alert emails off,
  or haven't confirmed their notify email. At most one per user per
  trading day (`recap_sends`).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from html import escape
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DBSession

from app.core.settings import get_settings
from app.db.database import get_session_factory
from app.models import (
    ArticleFollowup,
    DailyClose,
    NotificationEventType,
    NotificationLog,
    NotificationRule,
    RecapSend,
    StockArticle,
    Ticker,
    User,
    WatchlistItem,
    utcnow,
)
from app.services import email_prefs
from app.services.articles import render
from app.services.articles import service as articles
from app.services.emailer import send_email
from app.services.price_history import derive_snapshot_for_ticker

log = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")
_TREND_DAYS = 30
# Biggest window first, so a stock at a new 52-week high isn't called a
# new 1-week high. Calendar days, matching price_history._WINDOWS.
_RECORDS = [("year", 365, "52-week"), ("quarter", 91, "3-month"),
            ("month", 30, "1-month"), ("week", 7, "1-week")]
_UP, _DOWN, _MUTED = "#1a7f37", "#cf222e", "#6e7781"


@dataclass
class Move:
    """One stock's day."""

    ticker: Ticker
    price: float
    prev_close: float
    pct: float
    closes: list[float]  # last _TREND_DAYS closes, oldest first, ending today
    record: str | None  # e.g. "new 52-week high"


def _record(db: DBSession, ticker_id: int, snap, day: date) -> str | None:
    first = db.execute(
        select(DailyClose.date).where(DailyClose.ticker_id == ticker_id)
        .order_by(DailyClose.date).limit(1)
    ).scalar_one()
    for window, days, label in _RECORDS:
        # Only claim a record when our history covers the whole window
        # (a few days' slack for weekends and holidays).
        if first > day - timedelta(days=days - 4):
            continue
        low, high = getattr(snap, f"{window}_low"), getattr(snap, f"{window}_high")
        if high is not None and snap.price > high:
            return f"new {label} high"
        if low is not None and snap.price < low:
            return f"new {label} low"
    return None


def _move(db: DBSession, ticker: Ticker, day: date) -> Move | None:
    """Today's move, or None if there's no close for today yet."""
    snap = derive_snapshot_for_ticker(db, ticker.id)
    if snap is None or snap.as_of != day or not snap.previous_price:
        return None
    closes = db.execute(
        select(DailyClose.close).where(DailyClose.ticker_id == ticker.id)
        .order_by(DailyClose.date.desc()).limit(_TREND_DAYS)
    ).scalars().all()
    return Move(
        ticker=ticker, price=snap.price, prev_close=snap.previous_price,
        pct=(snap.price - snap.previous_price) / snap.previous_price * 100.0,
        closes=list(reversed(closes)), record=_record(db, ticker.id, snap, day),
    )


def _market_pct(db: DBSession, day: date) -> float | None:
    symbol = get_settings().ai_market_symbol
    ticker = db.execute(select(Ticker).where(Ticker.symbol == symbol)).scalar_one_or_none()
    move = _move(db, ticker, day) if ticker is not None else None
    if move is not None:
        return move.pct
    try:
        return articles.market_move_pct()
    except Exception as e:  # noqa: BLE001
        log.warning("Recap: market move lookup failed: %s", e)
        return None


def _day_start_utc(day: date) -> datetime:
    """Midnight ET of `day`, as naive UTC (how we store datetimes)."""
    start = datetime(day.year, day.month, day.day, tzinfo=_ET)
    return start.astimezone(timezone.utc).replace(tzinfo=None)


def _alerted_today(db: DBSession, user_id: int, day: date) -> set[int]:
    return set(db.execute(
        select(NotificationLog.ticker_id).where(
            NotificationLog.user_id == user_id,
            NotificationLog.sent_at >= _day_start_utc(day),
        )
    ).scalars().all())


@dataclass
class QuietRule:
    """A price-change rule that didn't fire today: the move stayed inside it."""

    move: Move
    low: float
    high: float


def _quiet_rules(db: DBSession, user_id: int, moves: dict[int, Move],
                 alerted: set[int]) -> list[QuietRule]:
    rules = db.execute(
        select(NotificationRule).where(
            NotificationRule.user_id == user_id,
            NotificationRule.event_type == NotificationEventType.PRICE_CHANGE_RANGE,
            NotificationRule.enabled.is_(True),
        )
    ).scalars().all()
    # Only ranges wider than the one we'd suggest are worth a nudge.
    band = get_settings().default_rule_band_pct
    quiet = [
        QuietRule(moves[r.ticker_id], r.pct_low, r.pct_high)
        for r in rules
        if r.ticker_id in moves and r.ticker_id not in alerted
        and r.pct_low is not None and r.pct_high is not None
        and r.pct_low <= moves[r.ticker_id].pct <= r.pct_high
        and (r.pct_low < -band or r.pct_high > band)
    ]
    return sorted(quiet, key=lambda q: abs(q.move.pct), reverse=True)


def _recipients(db: DBSession, day: date) -> list[User]:
    off = email_prefs.opted_out_user_ids(db) | email_prefs.recap_opted_out_user_ids(db)
    done = set(db.execute(
        select(RecapSend.user_id).where(RecapSend.trading_date == day)
    ).scalars().all())
    users = db.execute(
        select(User).where(User.notify_email_confirmed.is_(True))
        .where(User.id.in_(select(WatchlistItem.user_id)))
    ).scalars().all()
    return [u for u in users if u.id not in off and u.id not in done]


def _stories(db: DBSession, movers: list[Move]) -> dict[int, StockArticle]:
    """Stories (by ticker id) for the biggest movers past the story bar,
    after waiting up to RECAP_STORY_WAIT_SECONDS. Empty when stories are
    off or anything goes wrong; the recap then has the stock list only."""
    settings = get_settings()
    candidates = {
        m.ticker.id: articles.Candidate(m.ticker, m.pct, m.price, m.prev_close)
        for m in movers if articles.needs_story(m.ticker, m.pct)
    }
    if not settings.ai_articles_enabled or not candidates:
        return {}
    try:
        stories = articles.ensure_articles(db, list(candidates.values()))
        articles.wait_for(db, list(stories.values()), settings.recap_story_wait_seconds)
        return stories
    except Exception as e:  # noqa: BLE001
        log.warning("Recap: stories unavailable: %s", e)
        db.rollback()
        return {}


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------


def _tags(m: Move, alerted: set[int]) -> list[str]:
    tags = []
    if m.record:
        tags.append(("🏔️ " if m.record.endswith("high") else "🕳️ ") + m.record)
    if m.ticker.id in alerted:
        tags.append("🔔 alert sent today")
    return tags


def _market_line(market_pct: float | None) -> str:
    return f"S&P 500 {market_pct:+.2f}% today" if market_pct is not None else ""


def _text(moves: list[Move], market_pct: float | None, alerted: set[int],
          kids: str | None, day: date) -> str:
    top = moves[0]
    lines = [f"📊 YOUR WATCHLIST TODAY · {day:%a %b} {day.day}"]
    if market_pct is not None:
        lines.append(_market_line(market_pct))
    lines += ["", f"Biggest mover: {top.ticker.symbol} {top.pct:+.2f}% (${top.price:,.2f})",
              "", "All your stocks:"]
    for m in moves:
        tags = " · ".join(_tags(m, alerted))
        lines.append(
            f"  {m.ticker.symbol:<6} {m.pct:+6.2f}%  ${m.price:,.2f}  "
            f"{render.sparkline(m.closes)}{'  ' + tags if tags else ''}"
        )
    if kids:
        lines += ["", f"Talk about it together: {top.ticker.symbol} was your biggest mover today.",
                  kids]
    return "\n".join(lines)


def _html(moves: list[Move], market_pct: float | None, alerted: set[int],
          kids: str | None) -> str:
    top = moves[0]
    rows = []
    for m in moves:
        color = _UP if m.pct >= 0 else _DOWN
        tags = " · ".join(escape(t) for t in _tags(m, alerted))
        rows.append(
            '<tr style="border-top:1px solid #eaeef2">'
            f'<td style="padding:6px 8px 6px 0"><strong>{escape(m.ticker.symbol)}</strong>'
            f'<br><span style="font-size:12px;color:{_MUTED}">{escape(m.ticker.name)}</span></td>'
            f'<td style="padding:6px 8px;color:{color};white-space:nowrap"><strong>{m.pct:+.2f}%</strong></td>'
            f'<td style="padding:6px 8px;white-space:nowrap">${m.price:,.2f}</td>'
            f'<td style="padding:6px 0;color:{color};white-space:nowrap">{render.sparkline(m.closes)}</td>'
            "</tr>"
            + (f'<tr><td colspan="4" style="padding:0 0 6px;font-size:12px;color:{_MUTED}">{tags}</td></tr>'
               if tags else "")
        )
    market = (f'<p style="margin:0 0 8px;color:{_MUTED}">{escape(_market_line(market_pct))}</p>'
              if market_pct is not None else "")
    color = _UP if top.pct >= 0 else _DOWN
    kids_html = (
        f'<p style="margin:20px 0 0;font-size:15px"><strong>Talk about it together:</strong> '
        f"{escape(top.ticker.symbol)} was your biggest mover today.</p>{kids}"
        if kids else ""
    )
    return f"""\
<div style="border:1px solid #d0d7de;border-radius:10px;padding:16px;margin:16px 0">
<p style="margin:0 0 8px;font-size:12px;letter-spacing:.04em;color:{_MUTED}">📊 YOUR WATCHLIST TODAY</p>
{market}
<p style="margin:0 0 12px;font-size:17px">Biggest mover: <strong>{escape(top.ticker.symbol)}</strong> <strong style="color:{color}">{top.pct:+.2f}%</strong></p>
<table role="presentation" cellpadding="0" cellspacing="0" style="width:100%;border-collapse:collapse;font-size:14px">{''.join(rows)}</table>
</div>
{kids_html}"""


def _range(q: QuietRule) -> str:
    return f"{q.low:g}% to +{q.high:g}%" if q.high >= 0 else f"{q.low:g}% to {q.high:g}%"


def _quiet_why() -> str:
    band = get_settings().default_rule_band_pct
    return (
        "Why change it: most stocks move less than 2% on a normal day, and funds "
        "even less, so a wide range may only email you a few times a year. A "
        f"smaller range, like -{band:g}% to +{band:g}%, means more alerts, and more "
        "chances to talk with your kids about why prices move. Keep it wide if "
        "you'd rather only hear about big moves."
    )


def _quiet_how() -> str:
    band = get_settings().default_rule_band_pct
    return (
        "How: open the stock's rules (link below, or the Rules button on your "
        "dashboard), choose \"Price change outside band (%)\", enter a smaller "
        f"Lower bound % and Upper bound % (for example -{band:g} and {band:g}), and "
        "click Save rule. It replaces the old range."
    )


def _rules_url(q: QuietRule) -> str:
    return f"{get_settings().frontend_base_url}/rules/{q.move.ticker.id}"


def _quiet_text(quiet: list[QuietRule]) -> str:
    lines = ["🔕 Your price alerts that didn't go off today"]
    for q in quiet:
        lines.append(f"  {q.move.ticker.symbol}: your range is {_range(q)}; "
                     f"today it moved {q.move.pct:+.2f}%.")
    lines += [_quiet_why(), _quiet_how()]
    lines += [f"  {q.move.ticker.symbol} rules: {_rules_url(q)}" for q in quiet]
    return "\n".join(lines)


def _quiet_html(quiet: list[QuietRule]) -> str:
    items = "".join(
        f'<li style="margin:2px 0"><strong>{escape(q.move.ticker.symbol)}</strong>: your range is '
        f"{escape(_range(q))}; today it moved {q.move.pct:+.2f}%. "
        f'<a href="{escape(_rules_url(q), quote=True)}">Change {escape(q.move.ticker.symbol)}\'s range ↗</a></li>'
        for q in quiet
    )
    return f"""\
<div style="border:1px solid #d0d7de;border-radius:10px;padding:16px;margin:16px 0;background:#f6f8fa">
<p style="margin:0 0 8px"><strong>🔕 Your price alerts that didn't go off today</strong></p>
<ul style="margin:0 0 8px;padding-left:20px">{items}</ul>
<p style="margin:0 0 8px;font-size:14px">{escape(_quiet_why())}</p>
<p style="margin:0;font-size:14px">{escape(_quiet_how())}</p>
</div>"""


def _footer_text(user: User) -> str:
    base = get_settings().frontend_base_url
    return (
        "You're receiving this daily recap because you have stocks on your watchlist.\n"
        f"Turn the recap off in Settings: {base}/settings\n"
        f"Stop all alert emails: {email_prefs.unsubscribe_url(user)}"
    )


def _send_recap(db: DBSession, user: User, moves: list[Move], market_pct: float | None,
                stories: dict[int, StockArticle], day: date, all_moves: dict[int, Move]) -> bool:
    top = moves[0]
    small = None if articles.needs_story(top.ticker, top.pct) else (top.ticker, top.pct)
    story = stories.get(top.ticker.id)
    block = None
    if get_settings().ai_articles_enabled:
        block = articles.kid_block(db, user, story, small)
    kids_text, kids_html, pending = block if block else (None, None, False)
    alerted = _alerted_today(db, user.id, day)
    quiet = _quiet_rules(db, user.id, all_moves, alerted)

    subject = f"[AI Stock Watcher] Your watchlist today: {top.ticker.symbol} {top.pct:+.1f}%"
    intro = f"Here's how your {len(moves)} stock{'s' if len(moves) != 1 else ''} did today."
    headers = email_prefs.alert_email_headers(user)
    message_id = None
    if pending:
        message_id = articles.new_message_id()
        headers = {**headers, "Message-ID": message_id}
    quiet_text = f"{_quiet_text(quiet)}\n\n" if quiet else ""
    body = (f"{intro}\n\n{_text(moves, market_pct, alerted, kids_text, day)}\n\n"
            f"{quiet_text}{_footer_text(user)}")
    html = render.email_html(
        intro, _html(moves, market_pct, alerted, kids_html) + (_quiet_html(quiet) if quiet else ""),
        f"{get_settings().frontend_base_url}/settings", email_prefs.unsubscribe_url(user),
        reason="You're receiving this daily recap because you have stocks on your watchlist.",
    )
    try:
        send_email(user.notify_email, subject, body, body_html=html, headers=headers)
    except Exception as e:  # pragma: no cover
        log.warning("Recap email failed for user=%d: %s", user.id, e)
        return False
    db.add(RecapSend(user_id=user.id, trading_date=day))
    if pending and story is not None:
        already = db.execute(
            select(ArticleFollowup).where(
                ArticleFollowup.article_id == story.id, ArticleFollowup.user_id == user.id,
            )
        ).scalar_one_or_none()
        if already is None:  # an alert today may already be waiting for it
            articles.add_followup(db, story, user, message_id, subject)
    try:
        db.commit()
    except IntegrityError:  # another run sent it first
        db.rollback()
    return True


# ---------------------------------------------------------------------------
# Job
# ---------------------------------------------------------------------------


def run_watchlist_recap(db: DBSession | None = None) -> int:
    """Send today's recap to everyone due one. Returns # of emails sent."""
    started = utcnow()
    t0 = time.monotonic()
    own = db is None
    if own:
        db = get_session_factory()()
    try:
        day = articles.trading_date()
        tickers = db.execute(
            select(Ticker).where(Ticker.id.in_(select(WatchlistItem.ticker_id)))
        ).scalars().all()
        moves = {t.id: m for t in tickers if (m := _move(db, t, day)) is not None}
        if not moves:
            log.info("Recap skipped: no closes for %s (market holiday?)", day)
            return 0

        users = _recipients(db, day)
        watch: dict[int, list[Move]] = {}
        for user_id, ticker_id in db.execute(
            select(WatchlistItem.user_id, WatchlistItem.ticker_id)
        ).all():
            if ticker_id in moves:
                watch.setdefault(user_id, []).append(moves[ticker_id])
        for user_moves in watch.values():
            user_moves.sort(key=lambda m: abs(m.pct), reverse=True)

        users = [u for u in users if watch.get(u.id)]
        tops = {watch[u.id][0].ticker.id: watch[u.id][0] for u in users}
        stories = _stories(db, list(tops.values()))
        market_pct = _market_pct(db, day)

        sent = 0
        for user in users:
            if _send_recap(db, user, watch[user.id], market_pct, stories, day, moves):
                sent += 1
        # A story that finished just after the wait found no follow-ups to
        # send (they were committed above), so send them now.
        for story in stories.values():
            articles.send_waiting_followups(db, story)

        elapsed = time.monotonic() - t0
        summary = (f"day={day}, recipients={len(users)}, emails_sent={sent}, "
                   f"stocks={len(moves)}, stories={len(stories)}")
        log.info("Watchlist recap complete (%.2fs): %s", elapsed, summary)
        from app.jobs.audit import record_job_run

        record_job_run(
            "watchlist_recap", "SUCCESS", started, elapsed,
            result_summary=summary, tables_updated=["recap_sends"] if sent else None,
        )
        return sent
    finally:
        if own:
            db.close()
