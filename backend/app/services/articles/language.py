"""Code-level language check on everything the AI writes.

First of two safety layers (the second is the independent AI reviewer in
the pipeline). Deterministic and strict: anything that matches is dropped,
never "fixed". Word lists are deliberately short and unambiguous so honest
stories aren't rejected; the AI reviewer catches subtler problems. (Phrases
like "nothing is guaranteed" or "the biggest losers today" are honest
lessons and finance terms, so they're not on the lists.)
"""

from __future__ import annotations

import re

AGE_LIMITS = {
    # group: (max words in story, max words in one sentence)
    "3-5": (50, 14),
    "6-9": (95, 22),
    "10-12": (130, 28),
    "13-17": (165, 34),
}
YOUNG_GROUPS = {"3-5", "6-9"}


def _words(*terms: str) -> re.Pattern[str]:
    return re.compile(r"\b(" + "|".join(terms) + r")\b", re.IGNORECASE)


_CHECKS: list[tuple[str, re.Pattern[str]]] = [
    ("a number about money", re.compile(r"[$%€£¥]|\bpercent\b", re.IGNORECASE)),
    ("a link", re.compile(r"https?://|www\.", re.IGNORECASE)),
    ("contact details", re.compile(
        r"[\w.+-]+@[\w-]+\.[\w.]+|\+?\d[\d ().-]{7,}\d")),
    ("investment advice", re.compile(
        r"\b(you|we) should (buy|sell|hold|invest)|\btime to (buy|sell)\b"
        r"|\bgo (buy|sell)\b|\bprice target\b|\bstrong (buy|sell)\b",
        re.IGNORECASE)),
    ("hype or get-rich talk", re.compile(
        r"\bto the moon\b|\bfree money\b|\bcan'?t lose\b|\bsure thing\b"
        r"|\bno[- ]brainer\b|\bdouble your money\b|\byolo\b|\bfomo\b|\bjackpot\b",
        re.IGNORECASE)),
    ("a call to action", re.compile(
        r"\bclick\b|\bsign up\b|\bdownload\b|\bsubscribe\b|\bpassword\b"
        r"|\b(send|wire|transfer) (money|cash|funds)\b|\bfollow us\b",
        re.IGNORECASE)),
    ("text aimed at an AI", re.compile(
        r"\bignore (all |any )?(previous|prior|above)|\bas an ai\b|\blanguage model\b"
        r"|\bsystem prompt\b|\b(my|these) instructions\b|```|<[a-z/!]",
        re.IGNORECASE)),
    ("rude language", _words(
        "fuck\\w*", "shit\\w*", "bitch\\w*", "bastard", "asshole", "damn",
        "crap", "piss\\w*", "slut", "whore", "stupid", "idiot\\w*", "dumb",
        "moron\\w*",
    )),
]
_SCARY_FOR_YOUNG = _words(
    "kill\\w*", "dead", "death\\w*", "die", "died", "dying", "blood\\w*",
    "gun\\w*", "shoot\\w*", "bomb\\w*", "war", "wars", "murder\\w*",
    "suicide", "terror\\w*", "disaster\\w*", "catastroph\\w*", "panic\\w*",
)
_SENTENCE_END = re.compile(r"[.!?]+")
_WORD = re.compile(r"[\w'’-]+")


def word_count(text: str) -> int:
    return len(_WORD.findall(text))


def language_problems(text: str, age_group: str | None = None) -> list[str]:
    """What's wrong with one piece of AI text. `age_group` adds the stricter
    checks for that age; None means grown-up text."""
    problems = [f"contains {what}" for what, pattern in _CHECKS if pattern.search(text)]
    if age_group in YOUNG_GROUPS and _SCARY_FOR_YOUNG.search(text):
        problems.append("frightening words for a young child")
    return problems


def readability_problems(story: str, age_group: str) -> list[str]:
    """Too long, or sentences too long, for the age group."""
    max_words, max_sentence = AGE_LIMITS[age_group]
    problems = []
    if word_count(story) > max_words:
        problems.append(f"story too long for ages {age_group}")
    longest = max((word_count(s) for s in _SENTENCE_END.split(story)), default=0)
    if longest > max_sentence:
        problems.append(f"sentence too long for ages {age_group}")
    return problems
