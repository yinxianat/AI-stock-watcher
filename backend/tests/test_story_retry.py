"""A rejected story gets another go at being written.

A story our language check or the AI reviewer throws out used to be the end
of it: the alert went out with nothing for the children. Research is the
expensive part of a story and it has already been paid for by then, so
rewriting from the same facts costs a fraction of the attempt that failed.

Three things are guarded, and the first is the one that matters:

1. **The gate does not move.** There is still exactly one reviewer, it is
   never re-run over the same text, and every attempt has to clear it. A
   retry buys more chances, not an easier bar. No story is better than a
   wrong story.
2. The retry is **aimed**: the writer is told what was wrong.
3. It is **bounded** — by the attempt count, and by the time the alert is
   waiting, because a rewrite that overruns the wait turns "no story" into
   a promise that may not be kept.
"""

from __future__ import annotations

from time import monotonic

import pytest

from app.core.settings import get_settings
from app.services.articles.pipeline import run_pipeline
from app.services.articles.schemas import Review, ReviewResult
from tests.test_articles import (
    AGE_GROUPS,
    # Autouse in its own module, so importing it here is what pins this
    # module to the same Claude + web-search settings. Without it the
    # fixture doubles' reuters.com fact is dropped as a disallowed domain
    # and every story comes back `no_clear_cause` before the writer runs.
    _claude_web_search_setup,  # noqa: F401
    _fact,
    _request,
    _research,
    _reviewer,
    _reviewer_sequence,
    _story,
    _writer,
    _writer_sequence,
)


@pytest.fixture
def attempts(monkeypatch):
    """Set AI_ARTICLE_MAX_WRITE_ATTEMPTS for one test."""
    def _set(n):
        monkeypatch.setenv("AI_ARTICLE_MAX_WRITE_ATTEMPTS", str(n))
        get_settings.cache_clear()
    yield _set
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# The gate does not move
# ---------------------------------------------------------------------------


def test_the_reviewer_is_never_re_run_over_the_same_text(attempts):
    """A second opinion on unchanged text is the same model reading the same
    words. It would bill us for a verdict we already have, and if it ever
    disagreed it would be overturning a rejection — which is the one thing
    a safety gate must not let a retry do."""
    attempts(3)
    reviewed: list = []
    result = run_pipeline(
        _request(), _research(_fact()),
        _writer(_story()),
        _reviewer(approve=[], problems=["too scary"], calls=reviewed),
    )
    assert result.status == "failed"
    # Three writes, three reviews — never two reviews of one draft.
    assert len(reviewed) == 3
    assert result.diagnostics["write_attempts"] == 3


def test_a_retry_still_has_to_clear_the_reviewer(attempts):
    """The headline guarantee: a story rejected every time is not sent."""
    attempts(3)
    result = run_pipeline(
        _request(), _research(_fact()), _writer(_story()),
        _reviewer(approve=[], problems=["unsupported claim"]),
    )
    assert result.status == "failed"
    assert result.story is None
    assert result.diagnostics["rejected_by"] == "reviewer_all_versions"
    assert len(result.diagnostics["rejections"]) == 3


def test_a_rejected_grown_up_summary_is_still_fatal_if_it_never_improves(attempts):
    attempts(2)
    result = run_pipeline(
        _request(), _research(_fact()), _writer(_story()),
        _reviewer(grown_up_ok=False, problems=["predicts the price"]),
    )
    assert result.status == "failed"
    assert result.diagnostics["rejected_by"] == "reviewer_grown_up_summary"


def test_one_attempt_is_exactly_the_old_behaviour(attempts):
    """The setting ships inert: 1 must do what the pipeline did before."""
    attempts(1)
    reviewed: list = []
    result = run_pipeline(
        _request(), _research(_fact()), _writer(_story()),
        _reviewer(approve=[], problems=["too scary"], calls=reviewed),
    )
    assert result.status == "failed"
    assert len(reviewed) == 1
    assert result.diagnostics["write_attempts"] == 1
    assert result.diagnostics["rejected_by"] == "reviewer_all_versions"


def test_a_nonsense_attempt_count_cannot_disable_the_writer(attempts):
    """0 or a negative must mean one attempt, not zero stories for ever."""
    for n in (0, -5):
        attempts(n)
        result = run_pipeline(
            _request(), _research(_fact()), _writer(_story()), _reviewer(),
        )
        assert result.status == "ready", n


# ---------------------------------------------------------------------------
# The retry works, and is aimed
# ---------------------------------------------------------------------------


def test_a_second_attempt_can_save_the_story(attempts):
    """The whole point: the family gets a story instead of a bare alert."""
    attempts(2)
    result = run_pipeline(
        _request(), _research(_fact()), _writer(_story()),
        _reviewer_sequence([
            {"approve": [], "problems": ["frightening wording for ages 6-9"]},
            {"approve": AGE_GROUPS},
        ]),
    )
    assert result.status == "ready"
    assert result.story is not None
    assert result.diagnostics["write_attempts"] == 2
    # The failed first go is kept even though the story went out fine: a
    # rising retry rate is a prompt problem arriving.
    assert result.diagnostics["rejections"][0]["rejected_by"] == "reviewer_all_versions"
    assert "rejected_by" not in result.diagnostics, (
        "a story that was sent must not be recorded as rejected"
    )


def test_the_writer_is_told_what_was_wrong(attempts):
    """A blind reroll is a coin toss on the same prompt. This is what makes
    the second attempt worth paying for."""
    attempts(2)
    given: list = []
    run_pipeline(
        _request(), _research(_fact()),
        _writer_sequence([_story()], rejections=given),
        _reviewer_sequence([
            {"approve": [], "problems": ["says the price will go up", "too long for ages 6-9"]},
            {"approve": AGE_GROUPS},
        ]),
    )
    assert given[0] is None, "the first attempt is written exactly as before"
    assert given[1] == ["says the price will go up", "too long for ages 6-9"]


def test_the_complaint_reaches_the_prompt():
    """The feedback has to arrive as an instruction to the writer, and must
    not be mistakable for a fact about the company."""
    from app.services.articles import prompts

    plain = prompts.writer_prompt(_request(), [_fact()], False)
    assert "previous_attempt_rejected" not in plain

    retry = prompts.writer_prompt(
        _request(), [_fact()], False, ["frightening wording for ages 6-9"],
    )
    assert "<previous_attempt_rejected>" in retry
    assert "frightening wording for ages 6-9" in retry
    assert "Do not mention this note" in retry
    # The complaint sits outside <facts>, or the writer could cite it.
    assert retry.index("</facts>") < retry.index("<previous_attempt_rejected>")


def test_a_language_check_rejection_is_retried_too(attempts):
    """Our own deterministic rules are the best retry candidates: the
    complaint is specific and finding it costs no AI at all."""
    attempts(2)
    # Trips `grown_up_summary too long`. Note the field name: `_story`
    # passes unknown keywords straight to a Pydantic model that ignores
    # them, so a near-miss like `grown_up=` builds a perfectly valid story
    # and the test quietly asserts nothing.
    bad = _story(grown_up_summary=" ".join(["word"] * 100))
    given: list = []
    result = run_pipeline(
        _request(), _research(_fact()),
        _writer_sequence([bad, _story()], rejections=given),
        _reviewer(),
    )
    assert result.status == "ready"
    assert result.diagnostics["write_attempts"] == 2
    assert given[1], "the writer was told what the language check objected to"


def test_a_partly_approved_story_is_not_retried(attempts):
    """Some age group survived, so a family gets a story. The parent sees
    their age or the nearest one, and paying to rewrite something already
    going out buys nothing."""
    attempts(3)
    reviewed: list = []
    result = run_pipeline(
        _request(), _research(_fact()), _writer(_story()),
        _reviewer(approve=[AGE_GROUPS[0]], calls=reviewed),
    )
    assert result.status == "ready"
    assert len(reviewed) == 1, "no second attempt for a story that shipped"
    assert result.diagnostics["write_attempts"] == 1


def test_research_finding_nothing_never_reaches_the_writer(attempts):
    """There is nothing to rewrite from. Retrying here would bill us for
    certainty — the facts are the problem, not the prose."""
    attempts(3)
    calls: list = []
    result = run_pipeline(
        _request(), _research(cause_found=False), _writer(_story(), calls), _reviewer(),
    )
    assert result.status == "no_clear_cause"
    assert calls == []


# ---------------------------------------------------------------------------
# Bounded by the clock as well as the count
# ---------------------------------------------------------------------------


def test_a_retry_is_skipped_when_the_alert_cannot_wait(attempts):
    """Overrunning the wait turns "no story" into "we're still looking" plus
    a follow-up that may never arrive, and a broken promise is worse for a
    family than a plain alert."""
    attempts(3)
    reviewed: list = []
    result = run_pipeline(
        _request(), _research(_fact()), _writer(_story()),
        _reviewer(approve=[], problems=["too scary"], calls=reviewed),
        deadline=monotonic() - 1,  # already past
    )
    assert result.status == "failed"
    assert len(reviewed) == 1, "no retry started against an expired budget"
    assert "not enough time" in result.diagnostics["retry_skipped"]


def test_a_generous_deadline_allows_the_retry(attempts):
    attempts(2)
    reviewed: list = []
    result = run_pipeline(
        _request(), _research(_fact()), _writer(_story()),
        _reviewer(approve=[], problems=["too scary"], calls=reviewed),
        deadline=monotonic() + 600,
    )
    assert len(reviewed) == 2
    assert "retry_skipped" not in result.diagnostics


def test_no_deadline_means_only_the_count_bounds_it(attempts):
    """Recaps and backfills have no alert waiting on them."""
    attempts(2)
    reviewed: list = []
    run_pipeline(
        _request(), _research(_fact()), _writer(_story()),
        _reviewer(approve=[], problems=["too scary"], calls=reviewed),
    )
    assert len(reviewed) == 2


def test_each_job_passes_its_own_wait_budget():
    """Wiring, and a bug this caught: the alert path waits
    AI_ARTICLE_WAIT_SECONDS and the recap path waits
    RECAP_STORY_WAIT_SECONDS, which at the defaults are 120 and 300. The
    first version of this read the alert's budget inside the shared
    pipeline entry point, so recap stories were handed 120s and skipped
    retries they had minutes left for.
    """
    import inspect

    from app.jobs import intraday, recap

    alert_src = inspect.getsource(intraday)
    recap_src = inspect.getsource(recap)
    assert "ensure_articles(" in alert_src and "ai_article_wait_seconds" in alert_src
    assert "ensure_articles(" in recap_src and "recap_story_wait_seconds" in recap_src
    assert "ai_article_wait_seconds" not in recap_src, (
        "the recap path must not borrow the alert's budget"
    )


def test_the_deadline_is_anchored_when_the_batch_is_dispatched():
    """The second bug this caught. Generation is pooled at
    AI_ARTICLE_MAX_CONCURRENT, so a story queued behind others starts late
    while the caller's clock has been running the whole time. Measuring
    from each story's own start handed a queued story a budget outlasting
    the wait it was supposed to fit inside — the exact overrun the deadline
    exists to prevent.
    """
    import inspect

    from app.services.articles import service

    src = inspect.getsource(service.ensure_articles)
    assert "monotonic() + wait_budget_s" in src, (
        "the deadline must be computed once, at dispatch"
    )
    # ...and the pipeline entry point must read it rather than invent one.
    run_src = inspect.getsource(service._run_with)
    assert "_current_deadline()" in run_src
    assert "wait_seconds" not in run_src


def test_no_budget_means_no_clock_limit():
    """A backfill or an ad-hoc regeneration has no email waiting on it, so
    only the attempt count bounds it."""
    from app.services.articles import service

    assert service._current_deadline() is None


def test_the_attempt_count_has_a_hard_ceiling(attempts):
    """A typo in the setting must not buy twenty write-and-review rounds.

    The ceiling is a safety limit as much as a spend one: every retry is
    told what the reviewer objected to, so a high count is a writer being
    coached past its own safety check rather than merely an expensive one.
    """
    from app.services.articles.pipeline import MAX_WRITE_ATTEMPTS

    attempts(50)
    reviewed: list = []
    result = run_pipeline(
        _request(), _research(_fact()), _writer(_story()),
        _reviewer(approve=[], problems=["too scary"], calls=reviewed),
    )
    assert result.status == "failed"
    assert len(reviewed) == MAX_WRITE_ATTEMPTS
    assert MAX_WRITE_ATTEMPTS <= 5, "a ceiling this high stops being a ceiling"
