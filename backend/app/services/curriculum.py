"""The money ideas a stock story is allowed to teach.

Before this, `prompts.py` listed five concepts inline and the writer picked
one freely:

    owning a share is owning part of a company · prices move daily · the
    market moves together · prices follow hopes and worries · nobody knows
    what happens next

All five are about market mechanics, so every story ever sent taught from
that one slice. Nothing about saving, goals, opportunity cost or risk —
even though "Save and Invest" is the largest theme in the FDIC curriculum
and the one closest to what this app is about.

So concepts are now a closed set, grounded in FDIC Money Smart. Three
things follow from making it closed rather than free text:

* the writer *chooses* a concept instead of inventing one, so a lesson can
  never be an idea nobody reviewed;
* coverage is measurable — "concept X has never been sent" is a query;
* validating a lesson against its source becomes a dict lookup rather than
  a retrieval at request time.

`source` records provenance. Most concepts come from an FDIC section;
three are marked OURS because a stock-watching app needs them and the
curriculum has no equivalent (it never discusses daily price movement).
Those three are the ones to scrutinise, since nobody else reviewed them.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from app.services.rag.chunking import BAND_TO_AGE_GROUPS

ALL_AGE_GROUPS: tuple[str, ...] = ("3-5", "6-9", "10-12", "13-17")


class Situation(str, enum.Enum):
    """What kind of day it was for this stock.

    Chosen to match the branches that already exist in
    `articles.service.kid_block`, so a situation is something the code
    already knows rather than a new judgement call.
    """

    DROP = "drop"                  # down past the story threshold
    RISE = "rise"                  # up past the story threshold
    WHOLE_MARKET = "whole_market"  # moved with the S&P 500, no company cause
    SMALL_MOVE = "small_move"      # inside the quiet range
    ANY = "any"                    # fits any day


# Provenance for a concept the curriculum doesn't cover.
OURS = "(ours — no FDIC equivalent)"


@dataclass(frozen=True)
class Concept:
    """One teachable idea, with who it suits and when it fits."""

    key: str
    title: str          # becomes the story's `lesson_title`
    statement: str      # the idea in one line, for the writer prompt
    theme: str          # FDIC theme or topic
    age_groups: tuple[str, ...]
    situations: tuple[Situation, ...]
    source: str         # the FDIC file and question, or OURS

    def fits(self, age_group: str, situation: Situation) -> bool:
        if age_group not in self.age_groups:
            return False
        return Situation.ANY in self.situations or situation in self.situations


def _ages(*bands: str) -> tuple[str, ...]:
    """Age groups covered by these FDIC grade bands, in curriculum order."""
    covered = {g for b in bands for g in BAND_TO_AGE_GROUPS[b]}
    return tuple(g for g in ALL_AGE_GROUPS if g in covered)


CONCEPTS: tuple[Concept, ...] = (
    # ---- Money basics, for the youngest ---------------------------------
    Concept(
        key="what_is_money",
        title="Money is for trading",
        statement="Money is what people trade for the things they need and want.",
        theme="Understanding Currency",
        age_groups=_ages("prek-2"),
        situations=(Situation.ANY,),
        source="fdic-prek-2-parent-guide-module-1.pdf — 'What is money?'",
    ),
    Concept(
        key="people_earn_by_working",
        title="People earn money by working",
        statement="Grown-ups do work, and in return they earn money.",
        theme="Earning Money",
        age_groups=_ages("prek-2"),
        situations=(Situation.ANY,),
        source="fdic-prek-2-parent-guide-module-2.pdf — 'What is a job?'",
    ),
    Concept(
        key="companies_earn_too",
        title="Companies earn money too",
        statement="A company earns money when people buy what it makes, just "
                  "like a person earns money by working.",
        theme="Earn",
        age_groups=_ages("prek-2", "gr3-5"),
        situations=(Situation.RISE, Situation.DROP),
        source="fdic-gr3-5-parent-guide-module-8.pdf — 'What is income, and "
               "how is it earned?'",
    ),

    # ---- Needs, wants, choices ------------------------------------------
    Concept(
        key="needs_and_wants",
        title="Needs and wants are different",
        statement="A need is something you cannot live without; a want is "
                  "something you would like but could manage without.",
        theme="Needs and Wants",
        age_groups=_ages("prek-2", "gr3-5"),
        situations=(Situation.ANY,),
        source="fdic-gr3-5-parent-guide-module-1.pdf — 'What is the "
               "difference between a need and a want?'",
    ),
    Concept(
        key="one_persons_need",
        title="The same thing can be a need or a want",
        statement="One person might need something that another person only "
                  "wants, which is why people value things differently.",
        theme="Buying Decisions",
        age_groups=_ages("gr3-5"),
        situations=(Situation.ANY,),
        source="fdic-gr3-5-parent-guide-module-1.pdf — 'How can one "
               "person's need be another person's want?'",
    ),
    Concept(
        key="comparing_choices",
        title="Comparing helps you choose",
        statement="Looking at a few choices side by side helps you decide "
                  "which one is worth it.",
        theme="Buying Decisions",
        age_groups=_ages("gr3-5"),
        situations=(Situation.ANY,),
        source="fdic-gr3-5-parent-guide-module-6.pdf — 'How can comparing "
               "choices help you decide what to buy?'",
    ),
    Concept(
        key="opportunity_cost",
        title="Choosing one thing means skipping another",
        statement="Whenever you pick one thing, you give up the other thing "
                  "you could have had instead. That is its opportunity cost.",
        theme="Buying Decisions / Spend",
        age_groups=_ages("gr3-5", "gr6-8"),
        situations=(Situation.ANY,),
        source="fdic-gr3-5-parent-guide-module-1.pdf and "
               "fdic-gr6-8-parent-guide-module-2.pdf — 'What is an "
               "opportunity cost?'",
    ),

    # ---- Saving and goals -----------------------------------------------
    Concept(
        key="why_save",
        title="Saving means you can choose later",
        statement="Putting money aside now means you can get something you "
                  "want later, instead of only what you can afford today.",
        theme="Saving and Spending",
        age_groups=_ages("prek-2", "gr6-8"),
        situations=(Situation.ANY,),
        source="fdic-prek-2-parent-guide-module-5.pdf — 'Why should I save?'",
    ),
    Concept(
        key="saving_needs_a_goal",
        title="Saving works better with a goal",
        statement="Deciding what you are saving for makes it much easier to "
                  "keep going.",
        theme="Setting Goals",
        age_groups=_ages("prek-2", "gr3-5"),
        situations=(Situation.ANY,),
        source="fdic-gr3-5-parent-guide-module-2.pdf — 'Setting Goals'",
    ),
    Concept(
        key="reaching_goals_takes_time",
        title="Goals are reached a little at a time",
        statement="Big goals are reached by setting aside a small amount "
                  "again and again, not all at once.",
        theme="Save and Invest",
        age_groups=_ages("gr3-5", "gr6-8"),
        situations=(Situation.ANY,),
        source="fdic-gr6-8-parent-guide-module-3.pdf — 'How can you reach "
               "financial goals?'",
    ),
    Concept(
        key="pay_yourself_first",
        title="Save some before you spend any",
        statement="Setting money aside as soon as you get it works better "
                  "than hoping something is left over at the end.",
        theme="Save and Invest",
        age_groups=_ages("gr6-8"),
        situations=(Situation.ANY,),
        source="fdic-gr6-8-parent-guide-module-3.pdf — 'What does it mean "
               "to \"pay yourself first\"?'",
    ),

    # ---- Investing -------------------------------------------------------
    Concept(
        key="what_is_investing",
        title="Investing is hoping money grows",
        statement="Investing means putting money into something, hoping it "
                  "is worth more later. It is different from saving because "
                  "the amount can go down as well as up.",
        theme="Save and Invest",
        age_groups=_ages("gr6-8"),
        situations=(Situation.ANY,),
        source="fdic-gr6-8-parent-guide-module-3.pdf — 'What is investing?'",
    ),
    Concept(
        key="risks_and_returns",
        title="Growing money has a risk",
        statement="Money that can grow can also shrink. The chance of more "
                  "and the chance of less come together.",
        theme="Save and Invest",
        age_groups=_ages("gr3-5", "gr6-8"),
        situations=(Situation.DROP, Situation.RISE),
        source="fdic-gr3-5-parent-guide-module-6.pdf — 'What are the risks "
               "and returns of investing?'",
    ),
    Concept(
        key="owning_part_of_a_company",
        title="A share is a small piece of a company",
        statement="Buying one share means owning a tiny part of a real "
                  "company, so how that company does matters to you.",
        theme="Save and Invest",
        age_groups=_ages("gr6-8", "gr9-12"),
        situations=(Situation.ANY,),
        source="fdic-gr9-12-parentguide-module-3.pdf — 'What if my teen "
               "wants to buy a stock?'",
    ),
    Concept(
        key="many_things_to_invest_in",
        title="There is more than one way to invest",
        statement="People can put money into different kinds of things, and "
                  "spreading it out means not everything rises or falls at once.",
        theme="Save and Invest",
        age_groups=_ages("gr6-8", "gr9-12"),
        situations=(Situation.DROP, Situation.WHOLE_MARKET),
        source="fdic-gr6-8-parent-guide-module-3.pdf — 'What are some "
               "investment vehicles?'",
    ),
    Concept(
        key="compound_growth",
        title="Growth builds on itself",
        statement="When money grows, the new amount can grow too, so staying "
                  "invested for a long time matters more than any single day.",
        theme="Save and Invest",
        age_groups=_ages("gr6-8", "gr9-12"),
        situations=(Situation.ANY,),
        source="money-smart-basics-kids-full-packet.pdf — Chapter 5, "
               "Compound Interest",
    ),

    # ---- The wider economy ----------------------------------------------
    Concept(
        key="economy_moves_everything",
        title="Some news moves everything at once",
        statement="News about the whole economy can change the price of "
                  "almost every company on the same day, so a change is not "
                  "always about one company.",
        theme="Earn",
        age_groups=_ages("gr6-8", "gr9-12"),
        situations=(Situation.WHOLE_MARKET,),
        source="fdic-gr9-12-parentguide-module-1.pdf — 'In what ways does "
               "the economy affect you?'",
    ),
    Concept(
        key="what_is_an_entrepreneur",
        title="Someone has to start the company",
        statement="An entrepreneur spots an opportunity and starts a "
                  "business, taking the risk that it might not work.",
        theme="Earn",
        age_groups=_ages("gr6-8", "gr9-12"),
        situations=(Situation.ANY,),
        source="fdic-gr9-12-parentguide-module-1.pdf — 'What is an "
               "entrepreneur?'",
    ),

    # ---- Ours: daily price movement --------------------------------------
    # The FDIC curriculum never discusses day-to-day price movement, so
    # these three have no source to check against. They carry the most risk
    # of the set and should be reviewed most carefully.
    Concept(
        key="prices_wiggle_daily",
        title="Prices move a little every day",
        statement="Prices shift up and down most days as people buy and "
                  "sell. One day is a small part of a long story.",
        theme="(daily price movement)",
        # Not 3-5: `prompts.py` bans "price", "market", "stock" and
        # "investor" for that group, and FDIC teaches the youngest children
        # about money with no market vocabulary at all. They are covered by
        # the ANY concepts above, which talk about money and saving instead.
        age_groups=_ages("gr3-5", "gr6-8", "gr9-12"),
        situations=(Situation.SMALL_MOVE, Situation.ANY),
        source=OURS,
    ),
    Concept(
        key="hopes_and_worries",
        title="Prices follow what people expect",
        statement="A price changes when people's hopes or worries about the "
                  "future change, not only when something has already happened.",
        theme="(daily price movement)",
        age_groups=_ages("gr3-5", "gr6-8", "gr9-12"),
        situations=(Situation.RISE, Situation.DROP),
        source=OURS,
    ),
    Concept(
        key="nobody_knows_next",
        title="Nobody knows what happens next",
        statement="No one can tell what a price will do tomorrow, which is "
                  "why people who invest think in years rather than days.",
        theme="(daily price movement)",
        age_groups=_ages("gr3-5", "gr6-8", "gr9-12"),  # see prices_wiggle_daily
        situations=(Situation.ANY,),
        source=OURS,
    ),
)

BY_KEY: dict[str, Concept] = {c.key: c for c in CONCEPTS}


def concepts_for(age_group: str, situation: Situation) -> list[Concept]:
    """Concepts the writer may choose from for this child and this day.

    Never empty for a valid pair — `test_every_age_and_situation_has_a_concept`
    guards that, because an empty list would leave the writer with nothing
    to teach and no way to say so.
    """
    return [c for c in CONCEPTS if c.fits(age_group, situation)]


def situation_for(move_pct: float, moved_with_market: bool, below_bar: bool) -> Situation:
    """Classify a day from values the story pipeline already has."""
    if below_bar:
        return Situation.SMALL_MOVE
    if moved_with_market:
        return Situation.WHOLE_MARKET
    return Situation.RISE if move_pct >= 0 else Situation.DROP


def prompt_block(age_group: str, situation: Situation) -> str:
    """The allowed concepts, formatted for the writer prompt.

    Keys are included because the writer returns one, which is what makes
    the choice checkable: a key either exists or it doesn't.
    """
    lines = [
        f"{c.key}: {c.title} — {c.statement}"
        for c in concepts_for(age_group, situation)
    ]
    return "\n".join(f"  - {line}" for line in lines)


def unsourced() -> list[Concept]:
    """Concepts with no curriculum backing — the ones to review hardest."""
    return [c for c in CONCEPTS if c.source == OURS]
