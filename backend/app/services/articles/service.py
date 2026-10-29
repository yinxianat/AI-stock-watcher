"""Stories for alert emails: create, wait, attach, follow up.

Used by the intraday job:

1. `ensure_articles` — for each stock whose day-over-day alert fired, reuse
   today's story or start generating one in the background (biggest moves
   first, capped per day, at most AI_ARTICLE_MAX_CONCURRENT at once).
2. `wait_for` — give them up to AI_ARTICLE_WAIT_SECONDS.
3. `build_alert_email` — alert + story if ready; otherwise a "still looking"
   note, and the caller records an ArticleFollowup.
4. When a story finishes after the alert went out, `send_waiting_followups`
   emails it as a reply to the alert. `sweep` (start of every intraday run)
   catches anything missed, e.g. after a restart.

Nothing here raises into the alert path: failures become a plain alert.
If the API key is missing, rejected or out of credit, stories pause for
AI_KEY_PROBLEM_PAUSE_MINUTES and only the admin is emailed (again at most
every few hours while it lasts, and once more when it works again).
"""

from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import Executor, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.utils import make_msgid, parseaddr
from typing import Callable
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DBSession

from app.core.settings import get_settings
from app.db.database import get_session_factory
from app.models import ArticleFollowup, DailyClose, StockArticle, Ticker, User, utcnow
from app.services import email_prefs
from app.services.alerts import AlertChannel, notify_admin
from app.services.articles import render
from app.services.articles.schemas import (
    ArticleRequest,
    Fact,
    GenerationResult,
    Story,
)
from app.services.emailer import send_email

log = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")
_TREND_DAYS = 30
# While the key stays broken, remind the admin this often (not every run).
_KEY_ALERT_REPEAT = timedelta(hours=6)


def _not_configured(req: ArticleRequest) -> GenerationResult:
    # Replaced by the Claude research + write stages.
    return GenerationResult(status="failed", error="AI story generation is not configured")


# Swappable in tests.
generate_article: Callable[[ArticleRequest], GenerationResult] = _not_configured


def _default_market_move() -> float | None:  # pragma: no cover (network)
    from app.services.stock_data import get_current_price, get_previous_close

    symbol = get_settings().ai_market_symbol
    price, prev = get_current_price(symbol), get_previous_close(symbol)
    if not price or not prev:
        return None
    return (price - prev) / prev * 100.0


market_move_pct: Callable[[], float | None] = _default_market_move


# ---------------------------------------------------------------------------
# Background generation
# ---------------------------------------------------------------------------


class InlineExecutor(Executor):
    """Runs work immediately. Used in tests, where the in-memory DB can't be
    shared across threads."""

    def submit(self, fn, /, *args, **kwargs):
        fut: Future = Future()
        try:
            fut.set_result(fn(*args, **kwargs))
        except Exception as e:  # noqa: BLE001
            fut.set_exception(e)
        return fut


_executor: Executor | None = None
_futures: dict[int, Future] = {}
_lock = threading.Lock()


_paused_until: datetime | None = None
_key_alerted_at: datetime | None = None


def stories_paused() -> bool:
    with _lock:
        return _paused_until is not None and utcnow() < _paused_until


def report_key_problem(reason: str) -> None:
    """Pause stories and tell the admin. Users aren't affected: their alerts
    go out as plain alerts. Never raises."""
    global _paused_until, _key_alerted_at
    settings = get_settings()
    now = utcnow()
    minutes = settings.ai_key_problem_pause_minutes
    with _lock:
        _paused_until = now + timedelta(minutes=minutes)
        due = _key_alerted_at is None or now - _key_alerted_at >= _KEY_ALERT_REPEAT
        if due:
            _key_alerted_at = now
    log.warning("AI stories paused for %d min: %s", minutes, reason)
    if not due:
        return
    notify_admin(
        "ai_key_problem",
        "AI stories paused: Anthropic API key problem",
        f"Problem: {reason}.\n\n"
        "What users see: price alerts are still sent as usual, just without "
        "the \"why it moved\" story. Nobody is told about the problem.\n\n"
        "To fix: check the key at console.anthropic.com (API Keys) and the "
        "account's credit and spending limit (Billing / Limits). If you make a "
        "new key, update ANTHROPIC_API_KEY in Railway Variables (or backend/.env "
        "locally); Railway restarts the service by itself.\n\n"
        f"Stories retry automatically every {minutes} minutes. You'll get a "
        "reminder every 6 hours while the problem lasts, and an email when "
        "stories work again.",
        channel=AlertChannel.ERROR,
    )


def _key_works() -> None:
    """The API answered, so any earlier key problem is fixed."""
    global _paused_until, _key_alerted_at
    with _lock:
        was_broken = _key_alerted_at is not None
        _paused_until = _key_alerted_at = None
    if was_broken:
        log.warning("AI stories working again")
        notify_admin(
            "ai_key_recovered",
            "AI stories working again",
            "The Anthropic API accepted the key again, so new price alerts "
            "include stories again.",
        )


def check_config() -> None:
    """At startup: stories on but no key → tell the admin straight away."""
    settings = get_settings()
    if settings.ai_articles_enabled and not settings.anthropic_api_key.strip():
        report_key_problem("ANTHROPIC_API_KEY is not set")


def _get_executor() -> Executor:
    global _executor
    if _executor is None:
        settings = get_settings()
        _executor = (
            InlineExecutor()
            if settings.app_env == "test"
            else ThreadPoolExecutor(
                max_workers=max(1, settings.ai_article_max_concurrent),
                thread_name_prefix="story",
            )
        )
    return _executor


def _submit(article_id: int, req: ArticleRequest) -> None:
    fut = _get_executor().submit(_generate_and_store, article_id, req)
    with _lock:
        _futures[article_id] = fut

    def _forget(_f: Future) -> None:
        with _lock:
            _futures.pop(article_id, None)

    fut.add_done_callback(_forget)


def _is_running(article_id: int) -> bool:
    with _lock:
        fut = _futures.get(article_id)
    return fut is not None and not fut.done()


def _generate_and_store(article_id: int, req: ArticleRequest) -> None:
    """Runs in a worker thread with its own DB session. Never raises."""
    db = get_session_factory()()
    try:
        try:
            result = generate_article(req)
        except Exception as e:  # noqa: BLE001
            log.warning("Story generation crashed for %s: %s", req.symbol, e)
            result = GenerationResult(status="failed", error=str(e)[:500])
        row = db.get(StockArticle, article_id)
        if row is None:
            return
        row.status = result.status
        row.model = result.model
        row.input_tokens = result.usage.input_tokens
        row.output_tokens = result.usage.output_tokens
        row.error = result.error
        row.completed_at = utcnow()
        if result.status == "ready" and result.story is not None:
            row.content = json.dumps({
                "story": result.story.model_dump(),
                "facts": [f.model_dump(mode="json") for f in result.facts],
                "moved_with_market": result.moved_with_market,
            })
        db.commit()
        log.info("Story %s %s: %s", req.symbol, req.direction, result.status)
        if result.key_problem:
            report_key_problem(result.key_problem)
        elif result.usage.input_tokens > 0:
            _key_works()
        send_waiting_followups(db, row)
    except Exception as e:  # noqa: BLE001
        log.warning("Storing story %d failed: %s", article_id, e)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Called by the intraday job
# ---------------------------------------------------------------------------


@dataclass
class Candidate:
    """A stock whose day-over-day alert fired this run."""

    ticker: Ticker
    move_pct: float
    price: float
    prev_close: float


def trading_date(now_utc: datetime | None = None) -> date:
    now_utc = now_utc or utcnow()
    return now_utc.replace(tzinfo=timezone.utc).astimezone(_ET).date()


def _direction(move_pct: float) -> str:
    return "up" if move_pct >= 0 else "down"


def _find(db: DBSession, ticker_id: int, day: date, direction: str) -> StockArticle | None:
    return db.execute(
        select(StockArticle).where(
            StockArticle.ticker_id == ticker_id,
            StockArticle.trading_date == day,
            StockArticle.direction == direction,
        )
    ).scalar_one_or_none()


def ensure_articles(db: DBSession, candidates: list[Candidate]) -> dict[int, StockArticle]:
    """Story per candidate stock, keyed by ticker id: today's existing one, or
    a new `pending` one being generated. Stocks over the daily cap get none.

    Commits `db` first, so the caller's pending work isn't lost if creating a
    story row hits a race with another run.
    """
    settings = get_settings()
    if not settings.ai_articles_enabled or not candidates:
        return {}
    if not settings.anthropic_api_key.strip():
        report_key_problem("ANTHROPIC_API_KEY is not set")
        return {}
    # While paused, today's finished stories are still used; no new ones.
    paused = stories_paused()
    db.commit()
    day = trading_date()
    created_today = db.execute(
        select(func.count()).select_from(StockArticle).where(StockArticle.trading_date == day)
    ).scalar_one()
    market_pct: float | None = None
    market_checked = False
    out: dict[int, StockArticle] = {}

    for c in sorted(candidates, key=lambda c: abs(c.move_pct), reverse=True):
        direction = _direction(c.move_pct)
        row = _find(db, c.ticker.id, day, direction)
        if row is None:
            if paused:
                continue
            if created_today >= settings.ai_article_max_per_day:
                log.info("Story cap reached (%d today); none for %s",
                         settings.ai_article_max_per_day, c.ticker.symbol)
                continue
            if not market_checked:
                market_checked = True
                try:
                    market_pct = market_move_pct()
                except Exception as e:  # noqa: BLE001
                    log.warning("Market move lookup failed: %s", e)
            row = StockArticle(
                ticker_id=c.ticker.id, trading_date=day, direction=direction,
                status="pending", move_pct=c.move_pct, price=c.price,
                prev_close=c.prev_close, market_pct=market_pct,
            )
            db.add(row)
            try:
                db.commit()
            except IntegrityError:  # another run created it first
                db.rollback()
                row = _find(db, c.ticker.id, day, direction)
                if row is None:
                    continue
            else:
                created_today += 1
                _submit(row.id, ArticleRequest(
                    symbol=c.ticker.symbol, company=c.ticker.name, trading_date=day,
                    direction=direction, move_pct=c.move_pct, price=c.price,
                    prev_close=c.prev_close, market_pct=market_pct,
                ))
        out[c.ticker.id] = row
    return out


def wait_for(db: DBSession, articles: list[StockArticle], timeout_s: float) -> None:
    """Block until these stories finish or `timeout_s` passes, then reload them."""
    with _lock:
        running = [_futures[a.id] for a in articles if a.id in _futures]
    if running:
        wait(running, timeout=max(0.0, timeout_s))
    for a in articles:
        db.refresh(a)


# ---------------------------------------------------------------------------
# Email content
# ---------------------------------------------------------------------------


def new_message_id() -> str:
    domain = parseaddr(get_settings().smtp_from)[1].rpartition("@")[2] or None
    return make_msgid(domain=domain)


def _view(db: DBSession, article: StockArticle) -> render.StoryView | None:
    if article.status != "ready" or not article.content:
        return None
    try:
        data = json.loads(article.content)
        story = Story.model_validate(data["story"])
        facts = [Fact.model_validate(f) for f in data["facts"]]
    except Exception as e:  # noqa: BLE001
        log.warning("Stored story %d unreadable: %s", article.id, e)
        return None
    closes = list(reversed(db.execute(
        select(DailyClose.close)
        .where(DailyClose.ticker_id == article.ticker_id, DailyClose.date < article.trading_date)
        .order_by(DailyClose.date.desc())
        .limit(_TREND_DAYS - 1)
    ).scalars().all()))
    as_of = (article.completed_at or article.created_at).replace(
        tzinfo=timezone.utc).astimezone(_ET)
    ticker = db.get(Ticker, article.ticker_id)
    return render.StoryView(
        symbol=ticker.symbol if ticker else "",
        company=ticker.name if ticker else "",
        move_pct=article.move_pct,
        market_pct=article.market_pct,
        as_of=as_of.strftime("%-I:%M %p ET"),
        story=story,
        facts=facts,
        closes=closes + [article.price],
        allowed_domains=get_settings().ai_news_domain_list,
    )


def _symbol(db: DBSession, article: StockArticle) -> str:
    ticker = db.get(Ticker, article.ticker_id)
    return ticker.symbol if ticker else "this stock"


def _footer_text(user: User) -> str:
    return (
        "You're receiving this because you set a price-change rule for this ticker.\n"
        f"{email_prefs.alert_email_footer(user)}"
    )


def _html(intro: str, block: str, user: User) -> str:
    return render.email_html(
        intro, block, get_settings().frontend_base_url, email_prefs.unsubscribe_url(user),
    )


def build_alert_email(
    db: DBSession, user: User, summary: str, article: StockArticle | None,
) -> tuple[str, str | None, bool]:
    """(text, html, still_pending) for one alert. `still_pending` means the
    email promised a follow-up, so the caller must record one."""
    if article is not None:
        view = _view(db, article)
        if view is not None:
            text = f"{summary}\n\n{render.story_text(view)}\n\n{_footer_text(user)}"
            return text, _html(summary, render.story_html(view), user), False
        if article.status == "pending":
            note = render.still_looking_text(_symbol(db, article))
            text = f"{summary}\n\n{note}\n\n{_footer_text(user)}"
            return text, _html(summary, f"<p>{note}</p>", user), True
    return f"{summary}\n\n{_footer_text(user)}", None, False


# ---------------------------------------------------------------------------
# Follow-ups
# ---------------------------------------------------------------------------


def add_followup(
    db: DBSession, article: StockArticle, user: User, message_id: str, subject: str,
) -> None:
    db.add(ArticleFollowup(
        article_id=article.id, user_id=user.id, in_reply_to=message_id, subject=subject,
    ))


def _claim(db: DBSession, followup_id: int, new_status: str) -> bool:
    """Atomically move one follow-up out of `waiting`. False if someone else
    (another thread or run) already did, so nothing is ever sent twice."""
    res = db.execute(
        update(ArticleFollowup)
        .where(ArticleFollowup.id == followup_id, ArticleFollowup.status == "waiting")
        .values(status=new_status, sent_at=utcnow() if new_status == "sent" else None)
    )
    db.commit()
    return res.rowcount == 1


def send_waiting_followups(db: DBSession, article: StockArticle) -> int:
    """Email the finished story to everyone told "still looking". Users get
    nothing if no story was found (their alert already said "if we find it")."""
    if article.status == "pending":
        return 0
    waiting = db.execute(
        select(ArticleFollowup).where(
            ArticleFollowup.article_id == article.id, ArticleFollowup.status == "waiting",
        )
    ).scalars().all()
    if not waiting:
        return 0
    view = _view(db, article)
    unsubscribed = email_prefs.opted_out_user_ids(db)
    sent = 0
    for f in waiting:
        user = db.get(User, f.user_id)
        deliverable = (
            view is not None and user is not None
            and user.notify_email_confirmed and user.id not in unsubscribed
        )
        if not deliverable:
            _claim(db, f.id, "abandoned")
            continue
        if not _claim(db, f.id, "sent"):
            continue
        intro = f"Here's the story behind {view.symbol}'s move today."
        headers = {
            **email_prefs.alert_email_headers(user),
            "Message-ID": new_message_id(),
            "In-Reply-To": f.in_reply_to,
            "References": f.in_reply_to,
        }
        send_email(
            user.notify_email,
            f.subject if f.subject.startswith("Re: ") else f"Re: {f.subject}",
            f"{intro}\n\n{render.story_text(view)}\n\n{_footer_text(user)}",
            body_html=_html(intro, render.story_html(view), user),
            headers=headers,
        )
        sent += 1
    return sent


def sweep(db: DBSession) -> None:
    """Finish what an earlier run started. Gives up on stories stuck in
    `pending` (e.g. lost in a restart), and sends any follow-ups whose story
    finished while nobody was there to send them. Never raises."""
    settings = get_settings()
    if not settings.ai_articles_enabled:
        return
    try:
        cutoff = utcnow() - timedelta(minutes=settings.ai_article_followup_give_up_minutes)
        stuck = db.execute(
            select(StockArticle).where(
                StockArticle.status == "pending", StockArticle.created_at < cutoff,
            )
        ).scalars().all()
        for a in stuck:
            if not _is_running(a.id):
                a.status, a.error, a.completed_at = "failed", "gave up waiting", utcnow()
        db.commit()
        article_ids = {
            aid for (aid,) in db.execute(
                select(ArticleFollowup.article_id).where(ArticleFollowup.status == "waiting")
            ).all()
        }
        for aid in article_ids:
            article = db.get(StockArticle, aid)
            if article is not None:
                send_waiting_followups(db, article)
    except Exception as e:  # noqa: BLE001
        log.warning("Story follow-up sweep failed: %s", e)
        db.rollback()
