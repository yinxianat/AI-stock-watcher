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

import dataclasses
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
from app.services.articles.preferences import get_age_groups
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


def _run_with(provider: str, req: ArticleRequest) -> GenerationResult:
    """Research → write → review with one provider and its news source."""
    from functools import partial

    from app.services.articles import claude, news, stages
    from app.services.articles.pipeline import run_pipeline
    from app.services.articles.providers import get_generator

    gen = get_generator(provider)
    research = (
        claude.research if get_settings().news_source_for(provider) == "web_search"
        else partial(news.research_from_news, gen)
    )
    return run_pipeline(req, research, partial(stages.write, gen), partial(stages.review, gen))


def _generate_with_ai(req: ArticleRequest) -> GenerationResult:
    """Main provider first; if it's down, out of quota or its key is
    rejected, the backup (AI_FALLBACK_PROVIDER) writes the story and the
    admin is told. A story the AI rejects on content (no clear cause, safety
    checks) isn't retried elsewhere: that's the right answer, not a failure."""
    settings = get_settings()
    result = GenerationResult(status="failed", error="no AI provider available (missing key or paused)")
    for i, provider in enumerate(settings.provider_chain):
        if not settings.key_for(provider) or _is_paused(provider):
            continue
        result = _run_with(provider, req)
        result.model = settings.model_for(provider)
        failed = (result.failed_service or "").lower()
        if failed != provider:
            if failed == "finnhub" and result.key_problem:
                report_key_problem("finnhub", result.key_problem)
            elif result.usage.input_tokens > 0:
                _service_works(provider)
            return result
        # This provider is the problem; tell the admin, then try the backup.
        report_key_problem(provider, result.key_problem or f"unavailable ({result.error})",
                           pause=bool(result.key_problem))
    return result


# Swappable in tests.
generate_article: Callable[[ArticleRequest], GenerationResult] = _generate_with_ai


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


# Per-service problem state: "gemini", "anthropic" or "finnhub".
_paused_until: dict[str, datetime] = {}
_alerted_at: dict[str, datetime] = {}
_NAMES = {"gemini": "Gemini", "anthropic": "Anthropic", "finnhub": "Finnhub"}
_FIX = {
    "gemini": ("aistudio.google.com/apikey (quota: aistudio.google.com/rate-limit)", "GEMINI_API_KEY"),
    "anthropic": ("console.anthropic.com (API Keys, Billing / Limits)", "ANTHROPIC_API_KEY"),
    "finnhub": ("finnhub.io (Dashboard → API key)", "FINNHUB_API_KEY"),
}


def _is_paused(service: str) -> bool:
    with _lock:
        until = _paused_until.get(service)
    return until is not None and utcnow() < until


def _usable_providers() -> list[str]:
    settings = get_settings()
    return [p for p in settings.provider_chain if settings.key_for(p) and not _is_paused(p)]


def stories_paused() -> bool:
    """No provider can run, or the news they need is unavailable."""
    settings = get_settings()
    usable = _usable_providers()
    needs_finnhub = any(settings.news_source_for(p) == "finnhub" for p in usable)
    finnhub_ok = bool(settings.finnhub_api_key.strip()) and not _is_paused("finnhub")
    return not usable or (needs_finnhub and not finnhub_ok)


def report_key_problem(service: str, reason: str, pause: bool = True) -> None:
    """Tell the admin (only the admin) that a service has a problem, and,
    for key/quota problems, stop using it for AI_KEY_PROBLEM_PAUSE_MINUTES.
    If a backup provider can take over, stories keep going on it. At most
    one email per service every few hours. Never raises."""
    settings = get_settings()
    now = utcnow()
    minutes = settings.ai_key_problem_pause_minutes
    with _lock:
        if pause:
            _paused_until[service] = now + timedelta(minutes=minutes)
        last = _alerted_at.get(service)
        due = last is None or now - last >= _KEY_ALERT_REPEAT
        if due:
            _alerted_at[service] = now
    name = _NAMES.get(service, service)
    backup = next(
        (p for p in _usable_providers() if p != service), None,
    ) if service in ("gemini", "anthropic") else None
    log.warning("AI stories: %s problem (%s); backup=%s", name, reason, backup or "none")
    if not due:
        return
    where, var = _FIX.get(service, ("the provider's dashboard", "its API key"))
    if backup:
        from app.services.articles import costs

        per_story = costs.story_estimate(backup, settings)
        per_day = None if per_story is None else per_story * costs.max_stories_per_day(settings)
        subject = f"AI stories: {name} problem, using {_NAMES[backup]} backup"
        impact = (f"Stories keep working on the backup ({_NAMES[backup]}, "
                  f"{settings.model_for(backup)}). Users notice nothing.\n"
                  f"Cost while the backup is in use: about {costs.money(per_story)} per story, "
                  f"at most {costs.money(per_day)} per day.")
    else:
        subject = f"AI stories paused: {name} problem"
        impact = ("Price alerts are still sent as usual, just without the "
                  "\"why it moved\" story. Nobody is told about the problem.")
    retry = (f"{name} is retried automatically after {minutes} minutes" if pause
             else f"{name} is tried again for the next story")
    notify_admin(
        "ai_key_problem",
        subject,
        f"Problem: {name}: {reason}.\n\n"
        f"What users see: {impact}\n\n"
        f"To fix: check {where}. If you make a new key, update {var} in Railway "
        "Variables (or backend/.env locally); Railway restarts the service by itself.\n\n"
        f"{retry}. You'll get a reminder every 6 hours while the problem lasts, "
        f"and an email when {name} works again.",
        channel=AlertChannel.ERROR,
    )


def _service_works(service: str) -> None:
    """The service answered, so any earlier problem with it is fixed."""
    with _lock:
        was_broken = _alerted_at.pop(service, None) is not None
        _paused_until.pop(service, None)
    if was_broken:
        name = _NAMES.get(service, service)
        log.warning("AI stories: %s working again", name)
        notify_admin(
            "ai_key_recovered",
            f"AI stories: {name} working again",
            f"{name} is answering again, so new stories use it as before.",
        )


def _missing_keys() -> list[tuple[str, str]]:
    """(service, reason) for every key the configured setup needs but lacks."""
    settings = get_settings()
    missing = []
    for p in settings.provider_chain:
        if not settings.key_for(p):
            role = "main" if p == settings.ai_provider else "backup"
            missing.append((p, f"{_FIX[p][1]} is not set ({role} AI provider)"))
    needs_finnhub = any(settings.news_source_for(p) == "finnhub" for p in settings.provider_chain)
    if needs_finnhub and not settings.finnhub_api_key.strip():
        missing.append(("finnhub", "FINNHUB_API_KEY is not set (needed for news)"))
    return missing


def check_config() -> None:
    """At startup: test every key (problems are emailed at once) and email
    the setup and its cost if anything changed. See `health`."""
    from app.services.articles import health

    if get_settings().app_env == "test":
        health.startup_check()
    else:
        # Network checks: don't hold up startup (Railway's health check).
        threading.Thread(target=health.startup_check, name="ai-setup-check", daemon=True).start()


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
    for service, reason in _missing_keys():
        report_key_problem(service, reason)
    if stories_paused():
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
            cap = settings.ai_article_max_per_day
            if abs(c.move_pct) >= settings.ai_article_always_pct:
                cap *= 2  # big moves: allowed past the cap, up to twice it
            if created_today >= cap:
                log.info("Story cap reached (%d today); none for %s",
                         created_today, c.ticker.symbol)
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


def _view(
    db: DBSession, article: StockArticle, age_groups: list[str],
) -> render.StoryView | None:
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
        allowed_domains=get_settings().story_link_domains,
        age_groups=age_groups,
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
        view = _view(db, article, get_age_groups(db, user.id))
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
    view = _view(db, article, [])
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
        mine = dataclasses.replace(view, age_groups=get_age_groups(db, user.id))
        intro = f"Here's the story behind {mine.symbol}'s move today."
        headers = {
            **email_prefs.alert_email_headers(user),
            "Message-ID": new_message_id(),
            "In-Reply-To": f.in_reply_to,
            "References": f.in_reply_to,
        }
        send_email(
            user.notify_email,
            f.subject if f.subject.startswith("Re: ") else f"Re: {f.subject}",
            f"{intro}\n\n{render.story_text(mine)}\n\n{_footer_text(user)}",
            body_html=_html(intro, render.story_html(mine), user),
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
