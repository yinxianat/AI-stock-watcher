"""Why a story came out empty, and what to do about it.

The dashboard used to show `no_clear_cause | 4` and stop. The pipeline
records plenty about every attempt — facts found, facts kept, which rule
dropped each rejected one, searches used against the ceiling they stopped
under — and none of it reached the screen, so the only question the count
raises had no answer on the page.

Two of these tests are about a decision rather than a display: whether
raising `AI_ARTICLE_MAX_SEARCHES` would help. Stories that used their whole
budget and stories that stopped under it look identical on the card and
have opposite fixes, so the verdict has to come from the data.
"""

from __future__ import annotations

from app.services import diagnostics


def _item(status="no_clear_cause", **diag):
    return {"status": status, "diagnostics": diag}


# ---------------------------------------------------------------------------
# The two names spelled the same
# ---------------------------------------------------------------------------


def test_the_article_status_is_labelled_apart_from_the_delivery_reason():
    """`stock_articles.status` and `delivery_outcomes.reason` both have a
    value spelled `no_clear_cause` and they mean unrelated things: the
    status is the ordinary quiet-day outcome, the reason means we failed to
    record why. Reading one against the other sends you hunting a bug that
    is not there."""
    from app.services.delivery import REASONS

    status_label = diagnostics.STORY_STATUS_LABELS["no_clear_cause"]
    reason_label = REASONS["no_clear_cause"].label
    assert status_label != reason_label
    assert "no cause found" in status_label.lower()
    assert "not recorded" in reason_label.lower()


def test_every_article_status_has_a_label():
    """The statuses the pipeline can write, per the StockArticle docstring."""
    for status in ("pending", "ready", "no_clear_cause", "failed"):
        assert diagnostics.STORY_STATUS_LABELS[status]


# ---------------------------------------------------------------------------
# Why one story came out empty
# ---------------------------------------------------------------------------


def test_a_row_with_no_diagnostics_says_so_rather_than_guessing():
    why = diagnostics._story_why("no_clear_cause", {}, -3.1)
    assert "predates diagnostics" in why


def test_research_finding_nothing_reports_the_budget_it_used():
    why = diagnostics._story_why(
        "no_clear_cause",
        {"cause_found": False, "facts_found": 0, "facts_kept": 0,
         "urls_seen": 18, "searches_used": 2, "max_searches": 2},
        -3.4,
    )
    assert "18 result" in why
    assert "2 of 2 searches" in why
    assert "worth a look" in why, "a 3.4% move with no cause is not routine"


def test_a_small_move_with_no_cause_is_called_routine():
    why = diagnostics._story_why(
        "no_clear_cause",
        {"cause_found": False, "facts_found": 0, "urls_seen": 6,
         "searches_used": 1, "max_searches": 3},
        0.9,
    )
    assert "Nothing to fix" in why


def test_facts_all_dropped_is_named_as_our_problem():
    """The distinction that matters most on this card: research finding
    nothing is usually the market being quiet, while research finding
    plenty that then fails our own checks is a bug we can fix."""
    why = diagnostics._story_why(
        "no_clear_cause",
        {"cause_found": True, "facts_found": 4, "facts_kept": 0,
         "searches_used": 2, "max_searches": 2,
         "dropped_by_rule": {"domain_not_allowed": 3, "quote_not_verbatim": 1}},
        -2.2,
    )
    assert "4 fact" in why
    assert "3× domain_not_allowed" in why
    assert "our problem" in why


def test_a_rejected_story_names_the_layer_that_rejected_it():
    why = diagnostics._story_why(
        "failed",
        {"rejected_by": "reviewer_all_versions",
         "reviewer_problems": ["scary wording for ages 6-9"]},
        -4.0,
    )
    assert "AI reviewer rejected every age version" in why
    assert "scary wording" in why


def test_a_crash_names_the_exception():
    why = diagnostics._story_why(
        "failed", {"rejected_by": "exception", "exception_type": "TimeoutError"}, 1.5,
    )
    assert "TimeoutError" in why


# ---------------------------------------------------------------------------
# The search-budget verdict
# ---------------------------------------------------------------------------


def test_budget_bound_stories_recommend_raising_the_ceiling():
    out = diagnostics._search_budget_verdict([
        _item(cause_found=False, searches_used=2, max_searches=2),
        _item(cause_found=False, searches_used=2, max_searches=2),
        _item(cause_found=False, searches_used=1, max_searches=2),
    ])
    assert out["at_ceiling"] == 2
    assert "raising AI_ARTICLE_MAX_SEARCHES is likely" in out["verdict"]


def test_stories_stopping_early_say_more_budget_buys_nothing():
    """The expensive mistake this prevents: paying for a higher ceiling
    that nothing was reaching anyway."""
    out = diagnostics._search_budget_verdict([
        _item(cause_found=False, searches_used=1, max_searches=6),
        _item(cause_found=False, searches_used=2, max_searches=6),
        _item(cause_found=False, searches_used=1, max_searches=6),
    ])
    assert out["at_ceiling"] == 0
    assert "would buy nothing" in out["verdict"]
    assert "AI_NEWS_DOMAINS" in out["verdict"], "name the thing to look at instead"


def test_only_causeless_stories_are_rated():
    """A story that found its cause in one search is not evidence about
    the ceiling, and counting it would dilute the verdict."""
    out = diagnostics._search_budget_verdict([
        _item("ready", cause_found=True, searches_used=1, max_searches=4),
        _item(cause_found=False, searches_used=4, max_searches=4),
    ])
    assert out["rated"] == 1
    assert out["at_ceiling"] == 1


def test_no_verdict_without_evidence():
    """Pre-diagnostics rows carry no search counts. Saying nothing beats
    inventing a recommendation from an empty set."""
    assert diagnostics._search_budget_verdict([])["verdict"] is None
    assert diagnostics._search_budget_verdict([_item()])["verdict"] is None


def test_the_report_carries_the_verdict_and_the_rule_histogram(db_session):
    """End to end through the real report, so the wiring is covered and
    not just the helpers."""
    import json
    from datetime import date

    from app.models import StockArticle, Ticker, TickerType

    day = date(2026, 11, 3)
    t = db_session.query(Ticker).filter_by(symbol="ZZZT").one_or_none()
    if t is None:
        t = Ticker(symbol="ZZZT", name="Test Co", type=TickerType.STOCK)
        db_session.add(t)
        db_session.flush()
    db_session.add(StockArticle(
        ticker_id=t.id, trading_date=day, direction="down",
        status="no_clear_cause", move_pct=-3.2, price=10.0, prev_close=10.33,
        content=json.dumps({"diagnostics": {
            "cause_found": True, "facts_found": 3, "facts_kept": 0,
            "searches_used": 2, "max_searches": 2,
            "dropped_by_rule": {"domain_not_allowed": 3},
        }}),
    ))
    db_session.commit()

    rep = diagnostics.stories_report(day, db=db_session)
    assert rep["dropped_by_rule"] == {"domain_not_allowed": 3}
    assert rep["status_labels"]["no_clear_cause"] == "Finished, no cause found"
    assert "raising AI_ARTICLE_MAX_SEARCHES" in rep["search_budget"]["verdict"]
    [item] = rep["items"]
    assert "3× domain_not_allowed" in item["why"]
    assert item["diagnostics"]["facts_found"] == 3
