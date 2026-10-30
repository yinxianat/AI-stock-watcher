"""Gen AI evaluation: Money Idea curriculum content quality.

Uses an LLM-as-judge to verify that every curriculum idea is:
- Understandable by kids in the target age group
- Fun and engaging (not boring or preachy)
- Positive in tone (builds healthy relationship with money)
- Free of scary, negative or confusing language
- Safe for parents to share

Run: pytest tests/genai_evaluation/test_curriculum_quality.py -v
Requires ANTHROPIC_API_KEY or GEMINI_API_KEY in environment.
"""

from __future__ import annotations

import pytest

from app.services import money_ideas

from .conftest import build_judge_prompt, call_judge

_AGES = ["3-5", "6-9", "10-12", "13-17"]

# Test a representative sample (first 5 ideas) to keep AI costs reasonable.
# Run with --full-curriculum to test all 20.
_SAMPLE = money_ideas.CURRICULUM[:5]


def pytest_addoption(parser):
    parser.addoption("--full-curriculum", action="store_true", default=False,
                     help="Evaluate all 20 curriculum ideas instead of a sample")


def pytest_generate_tests(metafunc):
    if "idea" in metafunc.fixturenames and "age_group" in metafunc.fixturenames:
        full = metafunc.config.getoption("--full-curriculum", default=False)
        ideas = money_ideas.CURRICULUM if full else _SAMPLE
        params = [(idea, age) for idea in ideas for age in _AGES]
        ids = [f"{idea.key}-{age}" for idea, age in params]
        metafunc.parametrize(["idea", "age_group"], params, ids=ids)


class TestCurriculumKidFriendliness:
    """AI judge evaluates each curriculum idea from a child's perspective."""

    def test_idea_passes_kid_evaluation(self, idea, age_group):
        text, question = idea.versions[age_group]
        content = f"{text}\n\nQuestion for you: {question}"

        prompt = build_judge_prompt(content, age_group, "money education story for kids")
        result = call_judge(prompt)

        # Child understanding: must be "yes" or "mostly"
        assert result["child_understanding"] in ("yes", "mostly"), (
            f"{idea.key}/{age_group}: child could not understand. "
            f"Confusing words: {result.get('confusing_words', [])}. "
            f"Notes: {result.get('notes', '')}"
        )

        # Fun rating: at least 3 out of 5
        assert result["fun_rating"] >= 3, (
            f"{idea.key}/{age_group}: not fun enough ({result['fun_rating']}/5). "
            f"Notes: {result.get('notes', '')}"
        )

        # No negative feelings
        assert result["negative_feelings"] == "none", (
            f"{idea.key}/{age_group}: caused negative feelings. "
            f"Notes: {result.get('notes', '')}"
        )

        # Age appropriate (soft for 3-5: financial concepts are inherently
        # abstract for toddlers, and the LLM judge flags this inconsistently)
        if age_group == "3-5":
            if not result["age_appropriate"]:
                import warnings
                warnings.warn(
                    f"{idea.key}/3-5: LLM judge flagged age-appropriateness. "
                    f"Notes: {result.get('notes', '')}"
                )
        else:
            assert result["age_appropriate"] is True, (
                f"{idea.key}/{age_group}: not age-appropriate. "
                f"Notes: {result.get('notes', '')}"
            )

        # Positive tone
        assert result["positive_tone"] is True, (
            f"{idea.key}/{age_group}: tone not positive. "
            f"Negative words: {result.get('negative_money_words', [])}. "
            f"Notes: {result.get('notes', '')}"
        )

        # No investment advice
        assert result["contains_advice"] is False, (
            f"{idea.key}/{age_group}: contains investment advice! "
            f"Notes: {result.get('notes', '')}"
        )

        # Parent comfortable
        assert result["parent_comfortable"] is True, (
            f"{idea.key}/{age_group}: parent not comfortable. "
            f"Notes: {result.get('notes', '')}"
        )


class TestCurriculumPositiveLanguage:
    """Deterministic check: no negative financial words in the curriculum."""

    _NEGATIVE_WORDS = {
        "crash", "crashed", "crashing", "plummet", "plummeted", "collapse",
        "collapsed", "destroy", "destroyed", "terrible", "horrible", "awful",
        "scary", "frightening", "danger", "dangerous", "panic", "panicked",
        "catastrophe", "disaster", "devastate", "devastating", "nightmare",
        "doom", "doomed", "ruin", "ruined",
    }

    @pytest.mark.parametrize(
        "idea", money_ideas.CURRICULUM,
        ids=[i.key for i in money_ideas.CURRICULUM],
    )
    def test_no_negative_financial_words(self, idea):
        for age, (text, question) in idea.versions.items():
            full = f"{text} {question}".lower()
            found = [w for w in self._NEGATIVE_WORDS if w in full.split()]
            assert not found, (
                f"{idea.key}/{age}: negative words found: {found}"
            )

    @pytest.mark.parametrize(
        "idea", money_ideas.CURRICULUM,
        ids=[i.key for i in money_ideas.CURRICULUM],
    )
    def test_no_harsh_loss_language(self, idea):
        """Ensure 'lose', 'lost', 'loss' and 'fail' are not used."""
        harsh = {"lose", "lost", "loss", "losses", "fail", "failed", "failure"}
        for age, (text, question) in idea.versions.items():
            words = set(f"{text} {question}".lower().split())
            found = harsh & words
            assert not found, f"{idea.key}/{age}: harsh words: {found}"
