"""Shared fixtures for Gen AI evaluation tests."""

from __future__ import annotations

import json
import os

import pytest

# Skip the entire folder when no AI key is available.
_HAS_KEY = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("GEMINI_API_KEY"))


def pytest_collection_modifyitems(items):
    if not _HAS_KEY:
        skip = pytest.mark.skip(reason="No AI API key available (set ANTHROPIC_API_KEY or GEMINI_API_KEY)")
        for item in items:
            # Only skip tests that actually call the AI judge; deterministic
            # checks (prompt wording, negative-word scans) always run.
            if "call_judge" in item.function.__code__.co_names or "kid_evaluation" in item.name or "sample_story" in item.name:
                item.add_marker(skip)


# Age-group personas the LLM-judge will role-play.
PERSONAS = {
    "3-5": {
        "name": "Jamie (age 4)",
        "desc": "A 4-year-old who knows nothing about money. Loves stories about "
                "animals and snacks. Uses very simple words. Gets scared easily by "
                "big or confusing words.",
    },
    "6-9": {
        "name": "Riley (age 8)",
        "desc": "An 8-year-old who knows coins and bills exist but nothing about "
                "stocks or companies. Loves games, sports cards and school fairs. "
                "Can read chapter books but not news articles.",
    },
    "10-12": {
        "name": "Morgan (age 11)",
        "desc": "An 11-year-old who has heard of stocks on TV but does not know "
                "how they work. Loves video games, sneakers and YouTube. Can handle "
                "some new vocabulary if explained clearly.",
    },
    "13-17": {
        "name": "Taylor (age 15)",
        "desc": "A 15-year-old with a part-time job who is curious about money but "
                "has never invested. Uses social media, follows some business news "
                "casually. Can read adult-level text but prefers plain language.",
    },
}


def build_judge_prompt(content: str, age_group: str, content_type: str = "educational text") -> str:
    """Build the LLM-as-judge evaluation prompt.

    The judge role-plays as a child in the given age group AND as a parent
    reviewing the content for safety. Returns a JSON evaluation.
    """
    persona = PERSONAS[age_group]
    return f"""\
You are evaluating a {content_type} written for children ages {age_group}.

First, role-play as {persona['name']}: {persona['desc']}
Read the following text and answer honestly:
1. Could you understand what this is about? (yes / mostly / not really / no)
2. Were there any words or ideas that confused you?
3. Did it feel fun and interesting, or boring and hard?
4. Did anything make you feel scared, sad or worried?

Then, switch to a parent reviewing this for their child:
5. Is the language age-appropriate for {age_group}?
6. Is the tone positive and encouraging?
7. Are there any words that could create a negative relationship with money or investing?
   (words like: crash, lose, fail, destroy, terrible, scary, risky in a frightening way)
8. Is there any investment advice or suggestion to buy/sell?
9. Would you feel comfortable reading this to your child?

<content>
{content}
</content>

Respond in JSON only, no other text:
{{
  "age_group": "{age_group}",
  "child_understanding": "yes|mostly|not really|no",
  "confusing_words": ["list any confusing words or empty list"],
  "fun_rating": 1-5,
  "negative_feelings": "none|mild|concerning",
  "age_appropriate": true/false,
  "positive_tone": true/false,
  "negative_money_words": ["list any negative words or empty list"],
  "contains_advice": true/false,
  "parent_comfortable": true/false,
  "overall_pass": true/false,
  "notes": "brief explanation"
}}"""


def _clean_json(text: str) -> dict:
    """Parse JSON from LLM output, handling common quirks."""
    import re

    if "```json" in text:
        text = text.split("```json")[1].split("```")[0]
    elif "```" in text:
        text = text.split("```")[1].split("```")[0]
    text = re.sub(r",\s*}", "}", text)
    text = re.sub(r",\s*]", "]", text)
    text = text.replace("true/false", "true")
    return json.loads(text)


def call_judge(prompt: str, retries: int = 2) -> dict:
    """Call the AI judge and parse the JSON response, retrying on parse errors."""
    for attempt in range(retries + 1):
        try:
            key = os.environ.get("ANTHROPIC_API_KEY")
            if key:
                return _call_anthropic(prompt, key)
            key = os.environ.get("GEMINI_API_KEY")
            if key:
                return _call_gemini(prompt, key)
            pytest.skip("No AI API key available")
        except json.JSONDecodeError:
            if attempt == retries:
                raise


def _call_anthropic(prompt: str, key: str) -> dict:
    import anthropic

    client = anthropic.Anthropic(api_key=key)
    response = client.messages.create(
        model="claude-haiku-5-5",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    text = next(b.text for b in response.content if hasattr(b, "text"))
    return _clean_json(text)


def _call_gemini(prompt: str, key: str) -> dict:
    import google.generativeai as genai

    genai.configure(api_key=key)
    model = genai.GenerativeModel("gemini-2.0-flash")
    response = model.generate_content(prompt)
    return _clean_json(response.text)
