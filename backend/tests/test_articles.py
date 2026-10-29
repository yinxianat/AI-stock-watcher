"""AI "why it moved" stories: checks, rendering, and the intraday flow.

The AI itself is replaced by fakes; nothing here touches the network.
"""

from __future__ import annotations

from concurrent.futures import Executor, Future
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app.core.settings import get_settings
from app.db.database import get_session_factory
from app.jobs.intraday import run_intraday_capture
from app.models import ArticleFollowup, NotificationLog, StockArticle, utcnow
from app.services.articles import render, service
from app.services.articles.pipeline import run_pipeline
from app.services.articles.schemas import (
    ArticleRequest,
    Fact,
    GenerationResult,
    Research,
    ResearchResult,
    Story,
    Usage,
    WriteResult,
)
from app.services.articles.validate import story_problems, validate_facts
from tests.test_intraday import _seed_prior_day_close, _setup_user_with_range_rule

TODAY = date(2026, 10, 29)
DOMAINS = ["finance.yahoo.com", "reuters.com"]
GOOD_URL = "https://www.reuters.com/technology/apple-iphone-sales-2026-10-29/"


def _fact(**kw) -> Fact:
    base = dict(
        id="f1", claim="Apple sold more iPhones than expected.",
        quote="iPhone revenue beat analyst estimates", source_url=GOOD_URL,
        source_name="Reuters", published=TODAY,
    )
    return Fact(**{**base, **kw})


def _story(**kw) -> Story:
    base = dict(
        headline="Apple's big iPhone day 🍎",
        story="Apple sold way more iPhones than people guessed. Lots of people wanted "
              "to own a tiny piece of Apple, so the price went up!",
        lesson_title="Why prices wiggle",
        lesson="When a company does better than people expected, more people want its shares.",
        talk_together="If you ran a toy shop, what would make more kids want to visit?",
        grown_up_summary="Quarterly iPhone sales came in above analyst expectations.",
        tone="playful", category="earnings", fact_ids=["f1"],
    )
    return Story(**{**base, **kw})


def _request(**kw) -> ArticleRequest:
    base = dict(
        symbol="AAPL", company="Apple Inc.", trading_date=TODAY, direction="up",
        move_pct=6.4, price=187.4, prev_close=176.1, market_pct=0.3,
    )
    return ArticleRequest(**{**base, **kw})


# ---------------------------------------------------------------------------
# Fact checks
# ---------------------------------------------------------------------------


def test_facts_must_be_real_allowed_and_recent():
    facts = [
        _fact(id="ok", source_url=GOOD_URL.rstrip("/")),  # same page, no slash
        _fact(id="other_site", source_url="https://randomblog.example/apple"),
        _fact(id="http", source_url=GOOD_URL.replace("https", "http")),
        _fact(id="not_searched", source_url="https://www.reuters.com/made-up"),
        _fact(id="stale", published=TODAY - timedelta(days=5)),
        _fact(id="undated", published=None),
        _fact(id="lookalike", source_url="https://reuters.com.evil.example/x"),
    ]
    seen = [GOOD_URL, "https://randomblog.example/apple", "https://reuters.com.evil.example/x",
            GOOD_URL.replace("https", "http")]
    kept = validate_facts(facts, seen_urls=seen, allowed_domains=DOMAINS,
                          today=TODAY, max_age_days=2)
    assert [f.id for f in kept] == ["ok"]


def test_clean_story_passes():
    assert story_problems(_story(), [_fact()]) == []


def test_company_names_with_dot_com_are_fine():
    story = _story(grown_up_summary="Amazon.com reported strong holiday sales.")
    assert story_problems(story, [_fact()]) == []


@pytest.mark.parametrize("field,text,problem", [
    ("story", "The price went up 6% today!", "number about money"),
    ("story", "Each share now costs $187.", "number about money"),
    ("lesson", "Read more at https://evil.example", "link"),
    ("talk_together", "Should we buy now before it's too late?", "advice"),
    ("grown_up_summary", "You should buy more shares.", "advice"),
    ("headline", "", "empty"),
])
def test_story_rules(field, text, problem):
    problems = story_problems(_story(**{field: text}), [_fact()])
    assert any(problem in p for p in problems), problems


def test_story_must_cite_known_facts():
    assert story_problems(_story(fact_ids=["f9"]), [_fact()])
    assert story_problems(_story(fact_ids=[]), [_fact()])


# ---------------------------------------------------------------------------
# Pipeline (research → validate → write)
# ---------------------------------------------------------------------------


def _research(*facts: Fact, cause_found=True, seen=(GOOD_URL,)):
    return lambda req: ResearchResult(
        Research(cause_found=cause_found, facts=list(facts)), seen_urls=list(seen),
    )


def _writer(story: Story, calls: list | None = None):
    def write(req, facts, moved_with_market):
        if calls is not None:
            calls.append([f.id for f in facts])
        return WriteResult(story)
    return write


def test_pipeline_ready_uses_only_checked_facts():
    calls: list = []
    result = run_pipeline(
        _request(),
        _research(_fact(), _fact(id="f2", source_url="https://www.reuters.com/made-up")),
        _writer(_story(), calls),
    )
    assert result.status == "ready"
    assert calls == [["f1"]]  # the writer never saw the unverifiable fact
    assert [f.id for f in result.facts] == ["f1"]


def test_pipeline_no_cause_skips_the_writer():
    calls: list = []
    result = run_pipeline(_request(), _research(cause_found=False), _writer(_story(), calls))
    assert result.status == "no_clear_cause"
    assert calls == []


def test_pipeline_all_facts_rejected_means_no_clear_cause():
    result = run_pipeline(_request(), _research(_fact(), seen=()), _writer(_story()))
    assert result.status == "no_clear_cause"


def test_pipeline_rejects_a_story_that_breaks_the_rules():
    result = run_pipeline(_request(), _research(_fact()), _writer(_story(story="Up 6%!")))
    assert result.status == "failed"
    assert "number about money" in result.error


def test_pipeline_never_raises():
    def boom(req):
        raise RuntimeError("API down")
    result = run_pipeline(_request(), boom, _writer(_story()))
    assert result.status == "failed"
    assert "API down" in result.error


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _view(**kw) -> render.StoryView:
    base = dict(
        symbol="AAPL", company="Apple Inc.", move_pct=6.4, market_pct=0.3,
        as_of="12:00 PM ET", story=_story(), facts=[_fact()],
        closes=[170, 172, 171, 175, 187.4], allowed_domains=DOMAINS,
    )
    return render.StoryView(**{**base, **kw})


def test_html_escapes_everything_the_ai_wrote():
    evil = _story(
        headline='<script>alert(1)</script>',
        story='<a href="https://phish.example">Claim your prize</a>',
    )
    html = render.story_html(_view(story=evil))
    assert "<script>" not in html
    assert '<a href="https://phish.example"' not in html
    assert "&lt;script&gt;" in html


def test_only_allowed_source_links_are_shown():
    facts = [_fact(), _fact(id="f2", source_url="https://phish.example/x", source_name="Phish")]
    v = _view(facts=facts)
    assert "phish.example" not in render.story_html(v)
    assert "phish.example" not in render.story_text(v)
    assert GOOD_URL in render.story_text(v)


def test_numbers_and_emoji_come_from_our_data():
    text = render.story_text(_view())
    assert "AAPL +6.40% today · S&P 500 +0.30%" in text
    assert "🚀" not in text and "📈" in text  # +6.4% is "up", not "rocket"
    assert render.mood_emoji(-9, "playful") == "🎢"
    gentle = render.story_text(_view(story=_story(tone="gentle")))
    assert "📌" in gentle and "📈" not in gentle


def test_sparkline():
    assert render.sparkline([1, 2, 3, 4, 5, 6, 7, 8]) == "▁▂▃▄▅▆▇█"
    assert render.sparkline([5]) == ""
    assert len(render.sparkline([3, 3, 3])) == 3


# ---------------------------------------------------------------------------
# Intraday flow
# ---------------------------------------------------------------------------


class _Later(Executor):
    """Holds generation jobs until the test runs them, like a slow AI."""

    def __init__(self):
        self.jobs: list = []

    def submit(self, fn, /, *args, **kwargs):
        fut: Future = Future()
        self.jobs.append((fut, fn, args, kwargs))
        return fut

    def run_all(self):
        for fut, fn, args, kwargs in self.jobs:
            fut.set_result(fn(*args, **kwargs))


def _ready(req: ArticleRequest) -> GenerationResult:
    return GenerationResult(status="ready", story=_story(), facts=[_fact()], model="fake")


@pytest.fixture
def stories_on(client, monkeypatch):
    monkeypatch.setenv("AI_ARTICLES_ENABLED", "true")
    monkeypatch.setenv("AI_ARTICLE_WAIT_SECONDS", "0")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    get_settings.cache_clear()
    monkeypatch.setattr(service, "_paused_until", None)
    monkeypatch.setattr(service, "_key_alerted_at", None)
    monkeypatch.setattr(service, "_executor", service.InlineExecutor())
    monkeypatch.setattr(service, "market_move_pct", lambda: 0.3)
    calls: list[str] = []

    def generate(req):
        calls.append(req.symbol)
        return _ready(req)

    monkeypatch.setattr(service, "generate_article", generate)
    return calls


def _rows(model):
    db = get_session_factory()()
    rows = db.execute(select(model)).scalars().all()
    db.close()
    return rows


def _fetch(prices: dict[str, float]):
    return lambda symbol: prices[symbol]


def test_alert_and_story_arrive_in_one_email(client, stories_on):
    _, ticker_id = _setup_user_with_range_rule("AAPL", -5.0, 5.0)
    _seed_prior_day_close(ticker_id, close=100.0)

    sent = run_intraday_capture(fetcher=_fetch({"AAPL": 106.4}), force=True)

    assert sent == 1
    email = client.sent_emails[0]
    assert "STOCK STORY" in email["body_text"]
    assert "Ask your kid" in email["body_text"]
    assert "6.40%" in email["body_text"]
    assert "Ask your kid" in email["body_html"]
    assert "Message-ID" in email["headers"]
    assert [a.status for a in _rows(StockArticle)] == ["ready"]
    assert _rows(ArticleFollowup) == []


def test_one_story_is_shared_by_everyone_watching(client, stories_on):
    _, ticker_id = _setup_user_with_range_rule("AAPL", -5.0, 5.0, email="a@example.com")
    _setup_user_with_range_rule("AAPL", -3.0, 3.0, email="b@example.com")
    _seed_prior_day_close(ticker_id, close=100.0)

    run_intraday_capture(fetcher=_fetch({"AAPL": 106.4}), force=True)

    assert stories_on == ["AAPL"]
    assert len(client.sent_emails) == 2
    assert all("STOCK STORY" in e["body_text"] for e in client.sent_emails)


def test_ten_minute_spike_alone_gets_no_story(client, stories_on):
    _setup_user_with_range_rule("AAPL", -1.0, 1.0)  # no prior close: tick signal only
    run_intraday_capture(fetcher=_fetch({"AAPL": 100.0}), force=True)
    run_intraday_capture(fetcher=_fetch({"AAPL": 105.0}), force=True)

    assert len(client.sent_emails) == 1
    assert stories_on == []
    assert "STOCK STORY" not in client.sent_emails[0]["body_text"]


def test_no_clear_cause_sends_the_plain_alert(client, stories_on, monkeypatch):
    monkeypatch.setattr(service, "generate_article",
                        lambda req: GenerationResult(status="no_clear_cause"))
    _, ticker_id = _setup_user_with_range_rule("AAPL", -5.0, 5.0)
    _seed_prior_day_close(ticker_id, close=100.0)

    run_intraday_capture(fetcher=_fetch({"AAPL": 106.4}), force=True)

    email = client.sent_emails[0]
    assert "STOCK STORY" not in email["body_text"]
    assert "still looking" not in email["body_text"]
    assert email["body_html"] is None
    assert _rows(ArticleFollowup) == []


def test_slow_story_follows_up_in_the_same_thread(client, stories_on, monkeypatch):
    later = _Later()
    monkeypatch.setattr(service, "_executor", later)
    _, ticker_id = _setup_user_with_range_rule("AAPL", -5.0, 5.0)
    _seed_prior_day_close(ticker_id, close=100.0)

    run_intraday_capture(fetcher=_fetch({"AAPL": 106.4}), force=True)

    alert = client.sent_emails[0]
    assert "still looking into why AAPL moved" in alert["body_text"]
    assert [f.status for f in _rows(ArticleFollowup)] == ["waiting"]

    later.run_all()  # the story finishes after the alert went out

    assert len(client.sent_emails) == 2
    follow = client.sent_emails[1]
    assert follow["subject"] == f"Re: {alert['subject']}"
    assert follow["headers"]["In-Reply-To"] == alert["headers"]["Message-ID"]
    assert "STOCK STORY" in follow["body_text"]
    assert [f.status for f in _rows(ArticleFollowup)] == ["sent"]

    # Nothing is sent twice, even if the sweep runs afterwards.
    db = get_session_factory()()
    service.sweep(db)
    db.close()
    assert len(client.sent_emails) == 2


def test_sweep_gives_up_on_a_lost_story(client, stories_on, monkeypatch):
    later = _Later()
    monkeypatch.setattr(service, "_executor", later)
    _, ticker_id = _setup_user_with_range_rule("AAPL", -5.0, 5.0)
    _seed_prior_day_close(ticker_id, close=100.0)
    run_intraday_capture(fetcher=_fetch({"AAPL": 106.4}), force=True)

    # Simulate a restart: the job is gone and the row is old.
    service._futures.clear()
    db = get_session_factory()()
    row = db.execute(select(StockArticle)).scalar_one()
    row.created_at = utcnow() - timedelta(minutes=30)
    db.commit()
    service.sweep(db)
    db.close()

    assert [a.status for a in _rows(StockArticle)] == ["failed"]
    assert [f.status for f in _rows(ArticleFollowup)] == ["abandoned"]
    assert len(client.sent_emails) == 1  # just the original alert


def test_daily_cap_keeps_the_biggest_move(client, stories_on, monkeypatch):
    monkeypatch.setenv("AI_ARTICLE_MAX_PER_DAY", "1")
    get_settings.cache_clear()
    _, aapl = _setup_user_with_range_rule("AAPL", -5.0, 5.0, email="a@example.com")
    db = get_session_factory()()
    from app.models import NotificationEventType, NotificationRule, Ticker, User, WatchlistItem
    user = db.execute(select(User).where(User.email == "a@example.com")).scalar_one()
    msft = db.execute(select(Ticker).where(Ticker.symbol == "MSFT")).scalar_one()
    db.add(WatchlistItem(user_id=user.id, ticker_id=msft.id))
    db.add(NotificationRule(user_id=user.id, ticker_id=msft.id,
                            event_type=NotificationEventType.PRICE_CHANGE_RANGE,
                            pct_low=-5.0, pct_high=5.0, enabled=True))
    db.commit()
    msft_id = msft.id
    db.close()
    _seed_prior_day_close(aapl, close=100.0)
    _seed_prior_day_close(msft_id, close=100.0)

    run_intraday_capture(fetcher=_fetch({"AAPL": 106.0, "MSFT": 91.0}), force=True)

    assert stories_on == ["MSFT"]  # -9% beats +6%
    assert len(_rows(NotificationLog)) == 2  # both alerts still sent


def test_stories_off_never_calls_the_ai(client, monkeypatch):
    calls: list = []
    monkeypatch.setattr(service, "generate_article", lambda req: calls.append(req))
    _, ticker_id = _setup_user_with_range_rule("AAPL", -5.0, 5.0)
    _seed_prior_day_close(ticker_id, close=100.0)

    run_intraday_capture(fetcher=_fetch({"AAPL": 106.4}), force=True)

    assert calls == []
    assert _rows(StockArticle) == []
    assert "STOCK STORY" not in client.sent_emails[0]["body_text"]


# ---------------------------------------------------------------------------
# API key problems: users unaffected, only the admin is told
# ---------------------------------------------------------------------------


class _ApiError(Exception):
    def __init__(self, status_code: int, message: str = "error"):
        super().__init__(message)
        self.status_code = status_code


@pytest.mark.parametrize("err,expected", [
    (_ApiError(401, "invalid x-api-key"), "rejected"),
    (_ApiError(403, "permission denied"), "isn't allowed"),
    (_ApiError(400, "Your credit balance is too low"), "out of credit"),
    (_ApiError(400, "messages: field required"), None),  # our bug, not the key
    (_ApiError(429, "rate limited"), None),  # passes by itself
    (_ApiError(529, "overloaded"), None),
    (RuntimeError("connection reset"), None),
])
def test_key_problems_are_told_apart_from_outages(err, expected):
    from app.services.articles.pipeline import key_problem_reason

    reason = key_problem_reason(err)
    assert (reason is None) if expected is None else (expected in reason)


@pytest.fixture
def admin_emails(monkeypatch):
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(service, "notify_admin",
                        lambda event, subject, body, **kw: sent.append((event, body)))
    return sent


def _key_rejected(req):
    return GenerationResult(status="failed", error="401 invalid x-api-key",
                            key_problem="the API key was rejected (wrong, disabled or deleted)")


def test_rejected_key_sends_user_a_plain_alert_and_tells_only_admin(
        client, stories_on, admin_emails, monkeypatch):
    calls: list = []
    monkeypatch.setattr(service, "generate_article", lambda req: calls.append(req) or _key_rejected(req))
    _, ticker_id = _setup_user_with_range_rule("AAPL", -5.0, 5.0)
    _seed_prior_day_close(ticker_id, close=100.0)

    run_intraday_capture(fetcher=_fetch({"AAPL": 106.4}), force=True)

    email = client.sent_emails[0]
    assert email["to"] == "alerts@example.com"
    assert "6.40%" in email["body_text"]
    for word in ("STOCK STORY", "still looking", "API", "key"):
        assert word not in email["body_text"]
    assert [e for e, _ in admin_emails] == ["ai_key_problem"]
    assert "price alerts are still sent" in admin_emails[0][1]

    # Paused: the next alert doesn't call the AI and doesn't email the admin again.
    db = get_session_factory()()
    db.query(NotificationLog).delete()
    db.commit()
    db.close()
    run_intraday_capture(fetcher=_fetch({"AAPL": 107.0}), force=True)
    assert len(calls) == 1
    assert len(client.sent_emails) == 2
    assert len(admin_emails) == 1


def test_missing_key_never_calls_the_ai(client, stories_on, admin_emails, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    get_settings.cache_clear()
    _, ticker_id = _setup_user_with_range_rule("AAPL", -5.0, 5.0)
    _seed_prior_day_close(ticker_id, close=100.0)

    run_intraday_capture(fetcher=_fetch({"AAPL": 106.4}), force=True)

    assert stories_on == []
    assert _rows(StockArticle) == []
    assert "STOCK STORY" not in client.sent_emails[0]["body_text"]
    assert [e for e, _ in admin_emails] == ["ai_key_problem"]
    assert "ANTHROPIC_API_KEY is not set" in admin_emails[0][1]


def test_admin_hears_when_stories_work_again(client, stories_on, admin_emails, monkeypatch):
    service.report_key_problem("the API key was rejected")
    monkeypatch.setattr(service, "_paused_until", None)  # pause is over
    monkeypatch.setattr(service, "generate_article", lambda req: GenerationResult(
        status="ready", story=_story(), facts=[_fact()], model="fake",
        usage=Usage(1000, 200),  # the API answered
    ))
    _, ticker_id = _setup_user_with_range_rule("AAPL", -5.0, 5.0)
    _seed_prior_day_close(ticker_id, close=100.0)

    run_intraday_capture(fetcher=_fetch({"AAPL": 106.4}), force=True)

    assert [e for e, _ in admin_emails] == ["ai_key_problem", "ai_key_recovered"]
    assert "STOCK STORY" in client.sent_emails[0]["body_text"]


def test_reminder_at_most_every_six_hours(client, stories_on, admin_emails, monkeypatch):
    service.report_key_problem("x")
    service.report_key_problem("x")
    assert len(admin_emails) == 1
    monkeypatch.setattr(service, "_key_alerted_at", utcnow() - timedelta(hours=7))
    service.report_key_problem("x")
    assert len(admin_emails) == 2
