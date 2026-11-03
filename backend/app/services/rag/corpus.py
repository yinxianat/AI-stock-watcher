"""Which FDIC documents are in the corpus, and why.

The full download is 29 PDFs, and indexing all of them would be a mistake:
roughly a third is about credit cards, loans, insurance and identity theft.
That material is good, but a stock price moving is not a conversation opener
for identity theft, so those chunks can only ever dilute retrieval — they
compete for the same top-k slots as the material that does fit.

So the corpus is curated, and the reasoning is recorded here rather than
living in someone's head. `EXCLUDED` carries a reason per file, because
"why isn't this in the index?" is the question a future reader will have.

Relevance is judged against one question: **can a stock price moving
plausibly open this conversation with a child?**

    Tier 1  directly about saving, investing and goals — the core
    Tier 2  a stock move is a natural hook into it
    excluded  good material, wrong product
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Source:
    """One curated document."""

    filename: str
    topic: str
    tier: int
    why: str
    # Only set when the filename carries no grade band. Never inferred from
    # the document text: at least one FDIC file has the wrong band in its
    # own page header.
    band_override: str | None = None


# ---------------------------------------------------------------------------
# Tier 1 — the core. Saving, investing, goal-setting.
# ---------------------------------------------------------------------------

TIER_1: tuple[Source, ...] = (
    Source(
        "fdic-gr9-12-parentguide-module-3.pdf", "Save and Invest", 1,
        "The single most relevant document in the collection: invest x56, "
        "stock x22, compound x7, market x9, written for teenagers.",
    ),
    Source(
        "fdic-gr6-8-parent-guide-module-3.pdf", "Save and Invest", 1,
        "Same theme for tweens, and the only source covering risk and "
        "diversification at a level a 10-12 year old can follow.",
    ),
    Source(
        "fdic-gr3-5-parent-guide-module-4.pdf", "Saving", 1,
        "Saving for younger children — the foundation the investing "
        "material builds on.",
    ),
    Source(
        "fdic-gr3-5-parent-guide-module-2.pdf", "Setting Goals", 1,
        "Why you put money aside at all. A price rise is a natural hook "
        "into talking about what you are saving for.",
    ),
    Source(
        "fdic-prek-2-parent-guide-module-5.pdf", "Saving and Spending", 1,
        "The 3-5 version of saving. Deliberately has no investing "
        "vocabulary, which is correct for the age rather than a gap.",
    ),
    Source(
        "fdic-prek-2-parent-guide-module-4.pdf", "Setting Goals", 1,
        "Goal-setting for the youngest group, in terms of waiting for "
        "something you want.",
    ),
    Source(
        "money-smart-basics-kids-full-packet.pdf", "Basics for Kids workbook", 1,
        "A separate workbook, not a duplicate of the module guides. "
        "Chapter 5 is Compound Interest and Chapter 8 is Small Business — "
        "both central to explaining why owning part of a company matters. "
        "Has no grade band in its filename, so it was being skipped "
        "silently until the override below.",
        band_override="gr3-5",
    ),
)

# ---------------------------------------------------------------------------
# Tier 2 — a stock move is a natural way into these.
# ---------------------------------------------------------------------------

TIER_2: tuple[Source, ...] = (
    Source(
        "fdic-gr9-12-parentguide-module-1.pdf", "Earn", 2,
        "A share price moves on how a company earns. This explains earning "
        "and the economy for teenagers.",
    ),
    Source(
        "fdic-gr6-8-parent-guide-module-1.pdf", "Earn", 2,
        "The same for tweens. Note this file has the wrong grade band in "
        "its own page header, which is why bands come from filenames.",
    ),
    Source(
        "fdic-gr9-12-parentguide-module-2.pdf", "Spend", 2,
        "Spending tradeoffs — the other side of choosing to invest.",
    ),
    Source(
        "fdic-gr6-8-parent-guide-module-2.pdf", "Spend", 2,
        "Spending tradeoffs for tweens.",
    ),
    Source(
        "fdic-gr3-5-parent-guide-module-1.pdf", "Buying Decisions", 2,
        "Needs versus wants, which is the clearest everyday analogy for "
        "why people value one company over another.",
    ),
    Source(
        "fdic-gr3-5-parent-guide-module-6.pdf", "Buying Decisions", 2,
        "A second pass at buying decisions, comparing choices.",
    ),
    Source(
        "fdic-gr3-5-parent-guide-module-8.pdf", "Exploring Careers and Income", 2,
        "Where income comes from, which grounds what a company does.",
    ),
    Source(
        "fdic-prek-2-parent-guide-module-3.pdf", "Needs and Wants", 2,
        "The 3-5 entry point for value, in terms of toys and snacks.",
    ),
    Source(
        "fdic-prek-2-parent-guide-module-2.pdf", "Earning Money", 2,
        "Earning for the youngest group — people do work and get paid.",
    ),
    Source(
        "fdic-prek-2-parent-guide-module-1.pdf", "Understanding Currency", 2,
        "Coins and what money is. The only source pitched low enough for "
        "a three-year-old.",
    ),
)

# ---------------------------------------------------------------------------
# Excluded, with reasons.
# ---------------------------------------------------------------------------

EXCLUDED: dict[str, str] = {
    "general-pages-prek-2-parentguide.pdf":
        "Boilerplate — 'What is Money Smart?' and how to use the guide. No "
        "teaching content, and short preamble chunks match everything.",
    "general-pages-gr3-5-parentguide.pdf": "Boilerplate, as above.",
    "general-pages-gr6-8-parentguide.pdf": "Boilerplate, as above.",
    "general-pages-gr9-12-parentguide.pdf": "Boilerplate, as above.",

    "fdic-prek-2-parent-guide-module-6.pdf":
        "Borrowing and Lending. A price move is not a way into borrowing, "
        "and lending is a harder idea than this app needs.",
    "fdic-gr6-8-parent-guide-module-4.pdf": "Borrow — credit and debt, not stock-relevant.",
    "fdic-gr9-12-parentguide-module-4.pdf": "Borrow — credit scores and loans.",

    "fdic-gr6-8-parent-guide-module-5.pdf":
        "Protect — fraud and identity theft. Important, but nothing about a "
        "stock price opens that conversation.",
    "fdic-gr9-12-parentguide-module-5.pdf": "Protect — insurance and fraud.",

    "fdic-gr3-5-parent-guide-module-5.pdf":
        "Payment Options — debit and credit mechanics. Unrelated to owning "
        "part of a company.",
    "fdic-gr3-5-parent-guide-module-7.pdf":
        "Charitable Giving. Worthwhile, but a price move is a weak hook and "
        "it risks reading as moralising in an alert email.",
    "fdic-gr3-5-parent-guide-module-3.pdf":
        "Budgeting. The closest call here — it is adjacent to saving, but "
        "household budgeting isn't what a price move prompts. Reconsider if "
        "the curriculum later covers spending plans.",
}

SELECTED: tuple[Source, ...] = TIER_1 + TIER_2
SELECTED_FILENAMES: tuple[str, ...] = tuple(s.filename for s in SELECTED)
BAND_OVERRIDES: dict[str, str] = {
    s.filename: s.band_override for s in SELECTED if s.band_override
}


def manifest() -> dict:
    """The curation decision, for a reader or a write-up."""
    return {
        "selected": len(SELECTED),
        "excluded": len(EXCLUDED),
        "tier_1": [s.filename for s in TIER_1],
        "tier_2": [s.filename for s in TIER_2],
        "excluded_reasons": EXCLUDED,
    }


def check_manifest(directory: str) -> dict[str, list[str]]:
    """Every curated file exists, and every file on disk has a decision.

    Catches the quiet failure: a typo'd filename is skipped at ingest with
    no error, so the corpus silently loses a document.
    """
    from pathlib import Path

    on_disk = {p.name for p in Path(directory).glob("*.pdf")}
    decided = set(SELECTED_FILENAMES) | set(EXCLUDED)
    return {
        "missing_from_disk": sorted(set(SELECTED_FILENAMES) - on_disk),
        "undecided_on_disk": sorted(on_disk - decided),
    }
