"""Why each user got what they got, recorded when the decision is made.

Every alert and recap email either carries a kid-friendly "why it moved"
story or it doesn't, and until now the "doesn't" was silent: the alert went
out as a plain price line, the admin saw a healthy pipeline, and the only
way to find out why was to re-derive it from `stock_articles` the next day
and guess. A guess is not good enough here — "no story" has at least ten
distinct causes, from "the move was too small to be worth paying for"
(working as designed, free) to "the provider key is dead" (needs you now),
and they look identical from the outside.

So the sending job records the outcome as it sends: a `DeliveryOutcome` row
per (email, ticker), plus one INFO log line, which `InfoLogHandler`
persists to `info_logs` for the dashboard's log view.

`REASONS` is the catalogue, and it is the single source of truth for the
wording. The dashboard renders it, `/admin/reason-catalogue` serves it and
the monitoring agent quotes it, so an outcome is explained the same way
everywhere. Each entry carries ranked `causes` rather than one guess,
because that is what the admin actually needs when a row looks wrong: not
"story_missing" but "here are the three things that produce this, most
likely first, and how to tell them apart".

Nothing in here may raise into the email path. A monitoring record is
worth less than the email it describes, so `record` swallows everything.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date as date_

from sqlalchemy.orm import Session as DBSession

from app.models import DeliveryOutcome, Ticker, User

log = logging.getLogger(__name__)

# What the family saw in the email.
STATE_STORY = "story"                      # a generated AI story
STATE_SMALL_MOVE = "small_move_note"       # the fixed below-the-bar note
STATE_MARKET_DAY = "market_day_note"       # the fixed whole-market-day note
STATE_PENDING = "pending_followup"         # "still looking", follow-up promised
STATE_NONE = "none"                        # price line only, nothing for kids

# Which email this was.
CHANNEL_ALERT = "alert"
CHANNEL_RECAP = "recap"
CHANNEL_FOLLOWUP = "followup"


@dataclass(frozen=True)
class Cause:
    """One candidate explanation, and how to confirm it."""

    cause: str
    check: str


@dataclass(frozen=True)
class Reason:
    """One row of the catalogue.

    `ok` separates "this is the system working" from "this is a miss".
    Without it a dashboard counts the free small-move note as a failure and
    cries wolf on every quiet day, which is most days.
    """

    label: str
    what: str
    ok: bool
    causes: tuple[Cause, ...] = ()
    fix: str | None = None


REASONS: dict[str, Reason] = {
    # ---- the healthy outcomes ------------------------------------------
    "ready": Reason(
        label="Story sent",
        what="An AI story was generated, passed both safety checks and went out with the email.",
        ok=True,
    ),
    "age_substituted": Reason(
        label="Story sent, different age",
        what=(
            "A story went out, but not in the age group this parent chose: that version "
            "was dropped by the language check or the AI reviewer, so the nearest age was "
            "sent instead. The parent is not told, which is why it shows here."
        ),
        ok=True,
        causes=(
            Cause("One age group's wording keeps failing the language check.",
                  "Look for \"ages X dropped\" warnings in the logs for this symbol; a "
                  "pattern on one group means the prompt for that age needs work."),
            Cause("The AI reviewer rejected that version on content.",
                  "Look for \"reviewer\" warnings naming the age group."),
        ),
        fix="If one age group is substituted often, fix that age's prompt in "
            "app/services/articles/prompts.py rather than lowering the safety checks.",
    ),
    "below_bar": Reason(
        label="Too small for a story",
        what=(
            "The move was under AI_ARTICLE_MIN_PCT_STOCK / _FUND, so the family got the "
            "fixed small-move note instead. No AI call, no cost — this is the design: "
            "a 0.3% wobble has no cause worth researching."
        ),
        ok=True,
        fix="Lower AI_ARTICLE_MIN_PCT_STOCK to buy stories for smaller moves. Expect most "
            "of them to come back no_clear_cause, because small moves usually have no "
            "reportable cause.",
    ),
    "market_day": Reason(
        label="Whole-market day",
        what=(
            "No cause specific to this stock was found, but it moved with the S&P 500, so "
            "the family got the fixed \"the whole market moved\" note. Correct answer, no AI cost."
        ),
        ok=True,
    ),
    "still_generating": Reason(
        label="Promised a follow-up",
        what=(
            "The story was not finished when the email had to go, so it said we are still "
            "looking and a follow-up was queued. Check the follow-ups card: a row still "
            "\"waiting\" hours later, or \"abandoned\", is a promise that was never kept."
        ),
        ok=True,
        causes=(
            Cause("Generation is slower than the job's wait budget.",
                  "Compare stock_articles.completed_at - created_at against "
                  "AI_ARTICLE_WAIT_SECONDS (alerts) or RECAP_STORY_WAIT_SECONDS (recaps)."),
        ),
        fix="If follow-ups are the normal path rather than the exception, raise "
            "AI_ARTICLE_WAIT_SECONDS; a story that arrives in its own email is read less.",
    ),
    "followup_sent": Reason(
        label="Follow-up delivered",
        what="The promised story arrived as a reply in the same email thread.",
        ok=True,
    ),

    # ---- no story, and something to look at -----------------------------
    "no_clear_cause": Reason(
        label="No verifiable cause",
        what=(
            "Research ran but nothing survived fact-checking, so the email carried the "
            "price line and nothing for kids. The family is not told we looked. This is "
            "the most common cause of a silently story-less alert."
        ),
        ok=False,
        causes=(
            Cause("The news genuinely does not explain the move (common on small moves, "
                  "index funds, and stocks moving on flows rather than news).",
                  "Check the move size. Under ~2% on a large-cap with no headline is "
                  "usually real: there was nothing to find."),
            Cause("Facts were found but dropped by validation — quote not word-for-word, "
                  "URL not in the search results, domain not allow-listed, or article "
                  "older than AI_NEWS_MAX_AGE_DAYS.",
                  "Look for \"dropped N unverifiable fact(s)\" in the logs for this symbol. "
                  "That line is the giveaway: research worked and validation rejected it."),
            Cause("The search budget was too tight to reach the story.",
                  "AI_ARTICLE_MAX_SEARCHES is 2 by default. Raise it and re-run the symbol."),
        ),
        fix="Check AI_NEWS_DOMAINS coverage and AI_ARTICLE_MAX_SEARCHES. Recurring on a "
            "liquid large-cap that clearly had news means validation or the search budget "
            "is too tight, not that the market was quiet.",
    ),
    "generation_failed": Reason(
        label="Generation failed",
        what=(
            "The pipeline errored or a safety layer rejected the whole story, so the email "
            "went out without one. The alert itself was never at risk — story failures "
            "cannot block an alert."
        ),
        ok=False,
        causes=(
            Cause("A safety layer rejected everything: the grown-up summary, or every age "
                  "version, failed the language check or the AI reviewer.",
                  "stock_articles.error says which. \"rejected:\" is the code-level check, "
                  "\"reviewer rejected\" is the AI reviewer."),
            Cause("The AI call itself failed — timeout, rate limit, or a malformed response.",
                  "stock_articles.error holds the exception text."),
            Cause("The reviewer could not run, so the story was dropped by design (fail closed).",
                  "Look for a review-stage exception in the logs for this symbol."),
        ),
        fix="Read stock_articles.error for this row. Key and billing problems also email "
            "the admin on their own via report_key_problem.",
    ),
    "ai_disabled": Reason(
        label="Stories switched off",
        what=(
            "AI_ARTICLES_ENABLED is false, so no story was attempted and nobody gets one — "
            "not even the free small-move note, which is gated on the same switch. Alerts "
            "and recaps are otherwise unaffected."
        ),
        ok=False,
        causes=(
            Cause("The switch is off in this environment.",
                  "Check AI_ARTICLES_ENABLED in Railway for the service that sent this. "
                  "It defaults to false, so an unset variable means off."),
            Cause("You are looking at the wrong environment's database.",
                  "UAT and production have separate databases. Check APP_ENV on the "
                  "service, and whether this row's siblings came from the other one."),
        ),
        fix="Set AI_ARTICLES_ENABLED=true on the service that sends the emails.",
    ),
    "provider_paused": Reason(
        label="Provider paused",
        what=(
            "The AI provider is in its cool-off after a key, quota or billing refusal, so "
            "no story was attempted. Users are told nothing; only the admin is emailed."
        ),
        ok=False,
        causes=(
            Cause("The API key is wrong, disabled or deleted.",
                  "Look for the \"AI stories paused\" admin email — it names the service "
                  "and the exact refusal."),
            Cause("The account is out of credit or over its spending cap.",
                  "Check the provider console's billing page."),
            Cause("The free-tier daily quota is used up.",
                  "It resets on the provider's clock, not ours; stories resume on their own."),
        ),
        fix="Fix the key or the billing, then wait AI_KEY_PROBLEM_PAUSE_MINUTES, or restart "
            "the service to clear the pause immediately. Setting AI_FALLBACK_PROVIDER means "
            "the backup covers the next outage instead of users losing stories.",
    ),
    "cap_reached": Reason(
        label="Daily cap used up",
        what=(
            "AI_ARTICLE_MAX_PER_DAY was already spent when this move fired, so this family "
            "got no story while earlier ones did. Biggest movers are served first, so this "
            "is the tail of a busy day."
        ),
        ok=False,
        causes=(
            Cause("A volatile day genuinely produced more qualifying moves than the cap.",
                  "Compare the stories card's count against AI_ARTICLE_MAX_PER_DAY."),
            Cause("The cap is set lower than the watchlist needs.",
                  "Distinct tickers across all watchlists is the real daily ceiling, since "
                  "one story is shared by everyone watching that stock."),
        ),
        fix="Raise AI_ARTICLE_MAX_PER_DAY, or raise AI_ARTICLE_ALWAYS_PCT so big moves "
            "bypass the cap (they already get twice it).",
    ),
    "no_prev_close": Reason(
        label="No previous close",
        what=(
            "The story step needs the previous trading day's close to describe the move "
            "and there was none, so it was skipped. This is a guard rather than an "
            "expected outcome: the capture loop already skips a ticker with no previous "
            "close before any rule is evaluated, so seeing this means something reached "
            "the story step by a path that does not check."
        ),
        ok=False,
        causes=(
            Cause("This ticker has no daily_closes row for the previous trading day.",
                  "Check daily_closes for this ticker_id around this date. A gap means a "
                  "batch run missed it."),
            Cause("The ticker was added today, so it has no history yet.",
                  "Check the watchlist_items row's created date. This fixes itself tomorrow."),
            Cause("PRICE_HISTORY_LIFETIME pruned the history out from under it.",
                  "Check the storage card: daily_closes is the source of truth for history "
                  "and the training corpus, so it should have a long retention."),
        ),
        fix="Confirm the last batch of the previous trading day completed, and that "
            "PRICE_HISTORY_LIFETIME is long enough to keep daily_closes.",
    ),
    "generation_error": Reason(
        label="Story step errored",
        what=(
            "Setting up or waiting for stories raised before any story could be attached, "
            "so this whole run's emails went out without one. Caught on purpose: a story "
            "problem must never stop an alert."
        ),
        ok=False,
        causes=(
            Cause("A database error while creating or reloading the story rows.",
                  "Look for \"Stories unavailable this run\" or \"Recap: stories "
                  "unavailable\" in the logs, with the exception text."),
            Cause("The market-move lookup failed and took the step down with it.",
                  "Look for \"Market move lookup failed\" just before it."),
        ),
        fix="Read the warning this logged; it carries the exception.",
    ),
    "content_unreadable": Reason(
        label="Stored story unreadable",
        what=(
            "The story row says ready but its JSON could not be parsed back into a story, "
            "so nothing could be rendered and the email went out without it."
        ),
        ok=False,
        causes=(
            Cause("The stored shape no longer matches the current schema — usually a "
                  "schemas.py change deployed after the row was written.",
                  "Look for \"Stored story N unreadable\" naming this row, then read "
                  "stock_articles.content for it."),
        ),
        fix="If a schema change caused it, old rows stay unreadable: they are training data, "
            "so migrate the content rather than deleting the rows.",
    ),
    "followup_abandoned": Reason(
        label="Promise not kept",
        what=(
            "This user was told we were still looking and never received the story. "
            "User-visible and silent: they are still waiting."
        ),
        ok=False,
        causes=(
            Cause("The story finished as failed or no_clear_cause, so there was nothing to send.",
                  "Check the story's status. This is the intended behaviour, but the user "
                  "was still promised something."),
            Cause("The user became undeliverable between the promise and the story — "
                  "unsubscribed, or their notification email went unconfirmed.",
                  "Check email_opt_outs and users.notify_email_confirmed."),
            Cause("The process restarted while the story was in flight and the sweep never "
                  "picked it up (it only runs during market hours).",
                  "Check whether the story was created near a deploy."),
        ),
        fix="Nothing to do per user. If this recurs, the sweep window is the thing to widen.",
    ),

    # ---- why a user got no email at all -------------------------------
    # Not story outcomes, but the same question one step earlier, and the
    # dashboard explains both from this one catalogue.
    "opted_out": Reason(
        label="Unsubscribed",
        what="The user turned all alert emails off, so nothing is sent to them.",
        ok=True,
    ),
    "recap_off": Reason(
        label="Recap switched off",
        what="The user kept alerts but turned the daily recap off in Settings.",
        ok=True,
    ),
    "email_unconfirmed": Reason(
        label="Email not confirmed",
        what=(
            "The user's notification email has never been confirmed, so nothing is sent "
            "to it. They may believe they are signed up."
        ),
        ok=False,
        causes=(
            Cause("They changed their notification email and never clicked the "
                  "confirmation link.",
                  "Check confirmation_tokens for this user, and whether the address "
                  "differs from their login email."),
            Cause("The confirmation email never arrived.",
                  "Check the logs around their signup for an SMTP failure."),
        ),
        fix="Ask the user to confirm their notification email; nothing reaches them "
            "until they do.",
    ),
    "no_watchlist": Reason(
        label="Empty watchlist",
        what=(
            "The user watches no tickers, so there is no watchlist recap to send. The "
            "starter recap covers them instead, so they should still hear from us."
        ),
        ok=True,
        causes=(
            Cause("They deliberately cleared their watchlist.", "Normal; the starter "
                  "recap is the right email for them."),
            Cause("The default ticker was never seeded for a new account.",
                  "Check that a new signup gets the default index fund."),
        ),
        fix="Confirm the starter recap is enabled and the default ticker is seeded.",
    ),
    "unexplained": Reason(
        label="Unexplained",
        what=(
            "The user was eligible and no gate explains why they got nothing. This is "
            "the one reason that always means a bug."
        ),
        ok=False,
        causes=(
            Cause("The job never reached this user — it failed partway through the "
                  "recipient loop.",
                  "Check the jobs card for a FAILED recap run, and the logs for an "
                  "exception naming this user id."),
            Cause("Their email send raised and the failure was only logged.",
                  "Look for \"Recap email failed for user=N\" with this user's id."),
            Cause("They became eligible after the job ran (a watchlist added later "
                  "in the day).",
                  "Compare the watchlist row's timestamp against the recap run time."),
        ),
        fix="Read the recap job's warnings for this day; an unexplained miss is always "
            "logged, even when it is not recorded.",
    ),
}

# Reasons that mean the pipeline worked, for counting.
OK_REASONS = frozenset(code for code, r in REASONS.items() if r.ok)

# Reasons that mean a story was attached, for counting.
STORY_REASONS = frozenset({"ready", "age_substituted", "followup_sent"})


@dataclass
class StoryOutcome:
    """The decision about one user's story, as the sending job made it."""

    state: str
    reason: str
    detail: str | None = None
    article_id: int | None = None
    age_group: str | None = None
    age_group_sent: str | None = None

    @property
    def ok(self) -> bool:
        return REASONS[self.reason].ok if self.reason in REASONS else False


def catalogue() -> dict:
    """The catalogue as plain JSON, for the API and the dashboard."""
    return {
        code: {
            "label": r.label,
            "what": r.what,
            "ok": r.ok,
            "fix": r.fix,
            "causes": [{"cause": c.cause, "check": c.check} for c in r.causes],
        }
        for code, r in REASONS.items()
    }


def record(
    db: DBSession,
    *,
    user: User,
    ticker: Ticker | None,
    channel: str,
    trading_date: date_,
    outcome: StoryOutcome,
) -> None:
    """Add the row and log the line. Never raises, never commits.

    No commit: the caller is mid-send and owns the transaction, so this rides
    along with the NotificationLog or RecapSend row it belongs to. Either both
    land or neither does, which is what makes the two tables agree.
    """
    try:
        db.add(DeliveryOutcome(
            user_id=user.id,
            ticker_id=ticker.id if ticker else None,
            article_id=outcome.article_id,
            trading_date=trading_date,
            channel=channel,
            story_state=outcome.state,
            reason=outcome.reason,
            detail=outcome.detail,
            age_group=outcome.age_group,
            age_group_sent=outcome.age_group_sent,
        ))
        # INFO, so it lands in info_logs and shows in the dashboard's log
        # view alongside the structured row. Placeholders, not an f-string:
        # f-strings defeat the alert handler's dedup.
        log.info(
            "Delivery: user=%d %s %s story=%s reason=%s",
            user.id, channel, ticker.symbol if ticker else "-",
            outcome.state, outcome.reason,
        )
    except Exception as e:  # noqa: BLE001 - monitoring must never break a send
        log.warning("Could not record delivery outcome for user=%d: %s", user.id, e)
