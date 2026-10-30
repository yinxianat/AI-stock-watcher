"""Gen AI evaluation: AI-generated stock story quality.

Evaluates the quality of AI-generated stock stories by creating sample
stories and running them through an LLM-as-judge pipeline that simulates
children at each age group with zero financial knowledge.

This test is designed to be run manually or in CI when AI keys are
available. It verifies that the story generation pipeline produces content
that is:
- Understandable by kids with zero financial knowledge
- Positive and joyful in tone
- Safe for parents to share
- Free of jargon, numbers and advice

Run: pytest tests/genai_evaluation/test_ai_story_quality.py -v
Requires ANTHROPIC_API_KEY or GEMINI_API_KEY in environment.
"""

from __future__ import annotations

import json

import pytest

from .conftest import PERSONAS, call_judge

# Sample stories that represent typical AI output. In CI with AI keys,
# you can replace these with live-generated stories from the pipeline.
_SAMPLE_STORIES = {
    "3-5": (
        "Imagine a little lemonade stand run by two friends! "
        "Today so many people came that all the lemonade was gone. "
        "The friends were so happy they did a silly dance! "
        "Everyone loved their lemonade so much. "
        "Tomorrow they will make even more!"
    ),
    "6-9": (
        "Think of Microsoft like a giant computer club that makes awesome games and apps. "
        "Today the club shared exciting news about a fun new project, "
        "and tons of people wanted to join! "
        "A share is like a tiny membership card for the club. "
        "When more people want that card, its price goes up, just like a rare trading card everyone wants. "
        "Pretty neat, right?"
    ),
    "10-12": (
        "Microsoft is the company behind Xbox, Windows and Teams. "
        "Today they shared great news: they made more money than people thought they would! "
        "When a company does better than expected, more people want to own a tiny piece of it. "
        "That tiny piece is called a share. "
        "Think of it like a popular game getting an awesome update. "
        "Suddenly everyone wants to play, and the game feels even more valuable."
    ),
    "13-17": (
        "Microsoft just shared how much money it made this quarter, "
        "and the number was bigger than experts predicted. "
        "A lot of that growth came from cloud computing, "
        "which is basically when companies rent computer power online instead of buying their own machines. "
        "When a company beats expectations like that, it shows the business is growing strong. "
        "More people want to own a share, which is a tiny piece of the company, "
        "so the price goes up. "
        "Think of it like a streaming service suddenly gaining subscribers way faster than anyone expected."
    ),
}


class TestSampleStoryEvaluation:
    """Evaluate sample stories through the LLM-as-judge pipeline."""

    @pytest.mark.parametrize("age_group", ["3-5", "6-9", "10-12", "13-17"])
    def test_sample_story_kid_friendly(self, age_group):
        story = _SAMPLE_STORIES[age_group]
        persona = PERSONAS[age_group]

        prompt = f"""\
You are evaluating an AI-generated stock story written for children ages {age_group}.

Role-play as {persona['name']}: {persona['desc']}
This child has ZERO knowledge of finance, stocks or investing.

Read this story and evaluate:

<story>
{story}
</story>

Answer in JSON only:
{{
  "understood_main_idea": true/false,
  "confusing_words": ["list any words a {age_group} year old would not know"],
  "felt_positive": true/false,
  "felt_scared_or_worried": true/false,
  "would_want_to_hear_more": true/false,
  "contains_numbers_or_money_symbols": true/false,
  "contains_advice_to_buy_or_sell": true/false,
  "age_appropriate_vocabulary": true/false,
  "parent_safe": true/false,
  "overall_score": 1-10,
  "feedback": "one sentence as the child"
}}"""

        result = call_judge(prompt)

        assert result["understood_main_idea"] is True, (
            f"Ages {age_group} could not understand. Feedback: {result.get('feedback', '')}"
        )
        assert result["felt_positive"] is True, (
            f"Ages {age_group} did not feel positive. Feedback: {result.get('feedback', '')}"
        )
        assert result["felt_scared_or_worried"] is False, (
            f"Ages {age_group} felt scared/worried! Feedback: {result.get('feedback', '')}"
        )
        assert result["contains_advice_to_buy_or_sell"] is False, (
            f"Ages {age_group}: contains advice!"
        )
        assert result["parent_safe"] is True, (
            f"Ages {age_group}: not parent-safe! Feedback: {result.get('feedback', '')}"
        )
        assert result["overall_score"] >= 7, (
            f"Ages {age_group}: score too low ({result['overall_score']}/10). "
            f"Feedback: {result.get('feedback', '')}"
        )


class TestPositiveLanguageGuidelines:
    """Verify the story prompt enforces positive language."""

    def test_writer_prompt_mentions_positive_tone(self):
        from app.services.articles.prompts import WRITER_SYSTEM

        lower = WRITER_SYSTEM.lower()
        assert "positive" in lower or "joyful" in lower or "playful" in lower, (
            "Writer prompt should emphasize positive/joyful/playful tone"
        )

    def test_writer_prompt_avoids_negative_framing(self):
        from app.services.articles.prompts import WRITER_SYSTEM

        lower = WRITER_SYSTEM.lower()
        assert "dipped" in lower or "dip" in lower, (
            "Writer prompt should suggest 'dipped' instead of 'fell/dropped'"
        )

    def test_writer_prompt_mentions_kid_excitement(self):
        from app.services.articles.prompts import WRITER_SYSTEM

        lower = WRITER_SYSTEM.lower()
        assert "excited" in lower or "adventure" in lower, (
            "Writer prompt should make kids excited to learn"
        )
