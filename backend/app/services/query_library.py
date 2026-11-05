"""Saved queries for the Data Lens console.

These are the questions worth asking about this database, written out so
nobody has to remember the schema to ask them. Each one carries a `teaches`
line naming the SQL idea it demonstrates, because the console has two jobs:
answer the question now, and leave the admin able to write the next query
without it.

They are ordered within each category from simplest to hardest, so working
down a category is a usable lesson plan: a bare SELECT, then WHERE, then
JOIN, then GROUP BY, then a window function.

**Dialect:** written for Postgres, which is what production runs. Date
arithmetic is the one place the dialects differ enough to matter, so these
avoid `INTERVAL` where a literal date does the job — which also reads better
for someone learning, since the window is visible in the text instead of
hidden in an expression.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SavedQuery:
    key: str
    category: str
    title: str
    question: str
    teaches: str
    sql: str

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "category": self.category,
            "title": self.title,
            "question": self.question,
            "teaches": self.teaches,
            "sql": self.sql.strip(),
        }


CATEGORIES: list[tuple[str, str]] = [
    ("start", "Start here"),
    ("pipeline", "Jobs and the pipeline"),
    ("delivery", "Emails and why they looked like that"),
    ("stories", "AI stories"),
    ("ai_tuning", "AI generation tuning"),
    ("prices", "Price data"),
    ("people", "Users and watchlists"),
    ("errors", "Errors and logs"),
]


QUERIES: list[SavedQuery] = [
    # ---------------------------------------------------------------- start
    SavedQuery(
        key="recent_jobs",
        category="start",
        title="What has run recently?",
        question="The last 20 scheduled jobs, newest first.",
        teaches="SELECT, ORDER BY … DESC and LIMIT — the three things almost "
                "every query here needs.",
        sql="""
SELECT job_name, status, started_at, duration_seconds, result_summary
FROM job_runs
ORDER BY started_at DESC
LIMIT 20
""",
    ),
    SavedQuery(
        key="row_counts",
        category="start",
        title="How big is everything?",
        question="Row count for a few key tables, in one result.",
        teaches="COUNT(*) and UNION ALL — stacking separate results into one "
                "table. Each SELECT must return the same number of columns.",
        sql="""
SELECT 'users' AS table_name, COUNT(*) AS row_count FROM users
UNION ALL SELECT 'tickers', COUNT(*) FROM tickers
UNION ALL SELECT 'notification_rules', COUNT(*) FROM notification_rules
UNION ALL SELECT 'daily_closes', COUNT(*) FROM daily_closes
UNION ALL SELECT 'stock_articles', COUNT(*) FROM stock_articles
UNION ALL SELECT 'delivery_outcomes', COUNT(*) FROM delivery_outcomes
UNION ALL SELECT 'notification_logs', COUNT(*) FROM notification_logs
UNION ALL SELECT 'job_runs', COUNT(*) FROM job_runs
ORDER BY row_count DESC
""",
    ),
    SavedQuery(
        key="today_everything",
        category="start",
        title="What happened today?",
        question="Every job that ran today, with how long it took.",
        teaches="WHERE with a date comparison. `CURRENT_DATE` is midnight "
                "today, so `>= CURRENT_DATE` means 'today so far'.",
        sql="""
SELECT job_name, status, started_at, finished_at,
       duration_seconds, result_summary, error
FROM job_runs
WHERE started_at >= CURRENT_DATE
ORDER BY started_at DESC
""",
    ),

    # ------------------------------------------------------------- pipeline
    SavedQuery(
        key="failed_jobs",
        category="pipeline",
        title="Which jobs have failed?",
        question="Every job run that did not succeed, newest first.",
        teaches="Filtering on a value that is NOT something, with `<>`. Note "
                "that SKIPPED is not a failure — a weekend run is skipped on "
                "purpose.",
        sql="""
SELECT job_name, status, started_at, duration_seconds, error
FROM job_runs
WHERE status <> 'SUCCESS'
ORDER BY started_at DESC
LIMIT 50
""",
    ),
    SavedQuery(
        key="job_reliability",
        category="pipeline",
        title="Is each job reliable?",
        question="Per job: how many runs, how many failed, average duration.",
        teaches="GROUP BY with several aggregates at once, and the "
                "COUNT(CASE WHEN …) trick for counting only some rows inside "
                "a group.",
        sql="""
SELECT job_name,
       COUNT(*) AS runs,
       COUNT(CASE WHEN status = 'SUCCESS' THEN 1 END) AS succeeded,
       COUNT(CASE WHEN status = 'FAILED'  THEN 1 END) AS failed,
       ROUND(AVG(duration_seconds)::numeric, 2) AS avg_seconds,
       ROUND(MAX(duration_seconds)::numeric, 2) AS slowest_seconds,
       MAX(started_at) AS last_run
FROM job_runs
GROUP BY job_name
ORDER BY failed DESC, job_name
""",
    ),
    SavedQuery(
        key="job_timing",
        category="pipeline",
        title="Are jobs firing at the right time?",
        question="What clock time each job actually started, per day. Compare "
                "against the schedule on the System panel.",
        teaches="Pulling pieces out of a timestamp. The schedule is written "
                "in US/Eastern but stored UTC, so convert before reading the "
                "hour — this is the query that catches a timezone bug.",
        sql="""
SELECT job_name,
       (started_at AT TIME ZONE 'UTC' AT TIME ZONE 'America/New_York')::date AS day_et,
       to_char(started_at AT TIME ZONE 'UTC' AT TIME ZONE 'America/New_York',
               'HH24:MI') AS started_et,
       status
FROM job_runs
WHERE started_at >= CURRENT_DATE - 7
ORDER BY started_at DESC
""",
    ),

    # ------------------------------------------------------------- delivery
    SavedQuery(
        key="delivery_reasons_today",
        category="delivery",
        title="Why did today's emails look the way they did?",
        question="Today's delivery outcomes grouped by reason — the one-screen "
                "answer to 'did the stories work today?'.",
        teaches="GROUP BY on two columns. Read the reason codes against the "
                "catalogue on the Monitoring page.",
        sql="""
SELECT channel, story_state, reason, COUNT(*) AS emails
FROM delivery_outcomes
WHERE trading_date = CURRENT_DATE
GROUP BY channel, story_state, reason
ORDER BY emails DESC
""",
    ),
    SavedQuery(
        key="no_story_detail",
        category="delivery",
        title="Who got an email with nothing for the kids?",
        question="Every send that carried no story, with the recorded reason "
                "and the sentence explaining that specific send.",
        teaches="JOIN plus LEFT JOIN in one query. `delivery_outcomes.ticker_id` "
                "can be NULL (the starter recap names no stock), which is "
                "exactly when LEFT JOIN matters — an inner join would silently "
                "drop those rows.",
        sql="""
SELECT u.id AS user_id, u.email, t.symbol,
       d.channel, d.story_state, d.reason, d.detail, d.sent_at
FROM delivery_outcomes d
JOIN users u ON u.id = d.user_id
LEFT JOIN tickers t ON t.id = d.ticker_id
WHERE d.story_state = 'none'
ORDER BY d.sent_at DESC
LIMIT 100
""",
    ),
    SavedQuery(
        key="one_user_story",
        category="delivery",
        title="What did one person actually receive?",
        question="Everything sent to one user. Change the email on line 7.",
        teaches="Filtering a join by a value on the joined table. This is the "
                "query to run when someone writes in to say they got nothing.",
        sql="""
SELECT d.trading_date, d.channel, t.symbol, d.story_state, d.reason,
       d.age_group, d.age_group_sent, d.detail, d.sent_at
FROM delivery_outcomes d
JOIN users u ON u.id = d.user_id
LEFT JOIN tickers t ON t.id = d.ticker_id
WHERE u.email = 'CHANGE-ME@example.com'
ORDER BY d.sent_at DESC
LIMIT 100
""",
    ),
    SavedQuery(
        key="recap_coverage",
        category="delivery",
        title="Who is missing their daily recap?",
        question="Users with a watchlist who have not opted out and got no "
                "recap today.",
        teaches="NOT EXISTS — 'find rows with no matching row over there'. "
                "Three of them stacked is how you express 'eligible but "
                "didn't get it' without a single join.",
        sql="""
SELECT u.id AS user_id, u.email, u.notify_email_confirmed,
       COUNT(w.id) AS watching
FROM users u
JOIN watchlist_items w ON w.user_id = u.id
WHERE NOT EXISTS (SELECT 1 FROM recap_opt_outs r WHERE r.user_id = u.id)
  AND NOT EXISTS (SELECT 1 FROM email_opt_outs e WHERE e.user_id = u.id)
  AND NOT EXISTS (
        SELECT 1 FROM recap_sends s
        WHERE s.user_id = u.id AND s.trading_date = CURRENT_DATE
      )
GROUP BY u.id, u.email, u.notify_email_confirmed
ORDER BY watching DESC
""",
    ),
    SavedQuery(
        key="alert_volume",
        category="delivery",
        title="How many alerts go out, and for what?",
        question="Alert emails per day and event type over the last fortnight.",
        teaches="Casting a timestamp to a date to group by day — the standard "
                "way to turn per-event rows into a daily trend.",
        sql="""
SELECT sent_at::date AS day, event_type, COUNT(*) AS emails
FROM notification_logs
WHERE sent_at >= CURRENT_DATE - 14
GROUP BY sent_at::date, event_type
ORDER BY day DESC, emails DESC
""",
    ),

    # -------------------------------------------------------------- stories
    SavedQuery(
        key="story_outcomes",
        category="stories",
        title="Is the story pipeline healthy?",
        question="Stories per day by status, with token use and model.",
        teaches="GROUP BY a date column directly (trading_date is already a "
                "date, so no cast is needed) and SUM over a group.",
        sql="""
SELECT trading_date, status, model, COUNT(*) AS stories,
       SUM(input_tokens) AS tokens_in,
       SUM(output_tokens) AS tokens_out
FROM stock_articles
GROUP BY trading_date, status, model
ORDER BY trading_date DESC, stories DESC
LIMIT 100
""",
    ),
    SavedQuery(
        key="story_failures",
        category="stories",
        title="What went wrong with the failed stories?",
        question="Failed and cause-less stories with the recorded error.",
        teaches="IN (…) for 'any of these values', which beats a chain of ORs.",
        sql="""
SELECT a.trading_date, t.symbol, a.direction, a.status,
       a.move_pct, a.market_pct, a.error, a.created_at, a.completed_at
FROM stock_articles a
JOIN tickers t ON t.id = a.ticker_id
WHERE a.status IN ('failed', 'no_clear_cause', 'pending')
ORDER BY a.created_at DESC
LIMIT 100
""",
    ),
    SavedQuery(
        key="story_feedback",
        category="stories",
        title="What do parents think of the stories?",
        question="Feedback ratings by content type and age group.",
        teaches="Counting by category across two dimensions. This is the "
                "table that will become training labels, so its shape "
                "matters.",
        sql="""
SELECT content_type, age_group, rating, COUNT(*) AS votes
FROM content_feedback
GROUP BY content_type, age_group, rating
ORDER BY content_type, age_group, votes DESC
""",
    ),

    # ---------------------------------------------------------- ai_tuning
    SavedQuery(
        key="retry_weekly_summary",
        category="ai_tuning",
        title="How are retries performing this week?",
        question="Stories attempted this week: how many passed first try, "
                "how many were saved by a retry, how many failed anyway, "
                "and how many retries were skipped for lack of time.",
        teaches="json_extract_path_text for reading fields from a JSON column. "
                "CASE WHEN inside COUNT for conditional counting. This is "
                "the query that tells you whether AI_ARTICLE_MAX_WRITE_ATTEMPTS "
                "is set correctly.",
        sql="""
SELECT
  COUNT(*) AS total_stories,
  COUNT(CASE WHEN status = 'ready'
             AND (content::json->'diagnostics'->>'write_attempts')::int = 1
        THEN 1 END) AS passed_first_try,
  COUNT(CASE WHEN status = 'ready'
             AND (content::json->'diagnostics'->>'write_attempts')::int > 1
        THEN 1 END) AS saved_by_retry,
  COUNT(CASE WHEN status = 'failed'
             AND content::json->'diagnostics'->>'rejections' IS NOT NULL
        THEN 1 END) AS failed_after_retries,
  COUNT(CASE WHEN content::json->'diagnostics'->>'retry_skipped' IS NOT NULL
        THEN 1 END) AS retry_skipped_no_time,
  COUNT(CASE WHEN status = 'no_clear_cause' THEN 1 END) AS no_clear_cause,
  COUNT(CASE WHEN status = 'ready' THEN 1 END) AS ready,
  COUNT(CASE WHEN status = 'failed' THEN 1 END) AS failed
FROM stock_articles
WHERE trading_date >= CURRENT_DATE - 7
  AND content IS NOT NULL
""",
    ),
    SavedQuery(
        key="retry_per_story",
        category="ai_tuning",
        title="How many attempts did each story take?",
        question="Every story from the last 7 days with its attempt count, "
                "rejection history, and whether it was saved by a retry. "
                "Sort by most attempts first to find the hardest stories.",
        teaches="Extracting nested JSON, casting to int, and ordering by a "
                "computed value. The 'rejections' field is a JSON array "
                "showing what went wrong on each failed attempt.",
        sql="""
SELECT
  t.symbol,
  a.direction,
  a.status,
  a.move_pct,
  (a.content::json->'diagnostics'->>'write_attempts')::int AS attempts,
  a.content::json->'diagnostics'->>'retry_skipped' AS skipped_reason,
  a.content::json->'diagnostics'->'rejections' AS rejection_history,
  a.content::json->'diagnostics'->>'rejected_by' AS final_rejected_by,
  a.created_at,
  a.completed_at
FROM stock_articles a
JOIN tickers t ON t.id = a.ticker_id
WHERE a.trading_date >= CURRENT_DATE - 7
  AND a.content IS NOT NULL
ORDER BY (a.content::json->'diagnostics'->>'write_attempts')::int DESC NULLS LAST,
         a.created_at DESC
""",
    ),
    SavedQuery(
        key="rejection_reasons",
        category="ai_tuning",
        title="Which safety layer rejects the most stories?",
        question="Rejection reasons across all stories in the last 30 days. "
                "Shows whether the problem is the language check, the "
                "reviewer, or a specific age group failing repeatedly.",
        teaches="json_array_elements for iterating over a JSON array inside "
                "a column. Each story's rejections array has one entry per "
                "failed attempt, each with the layer that rejected it.",
        sql="""
SELECT
  r->>'rejected_by' AS rejected_by,
  COUNT(*) AS times,
  COUNT(DISTINCT a.id) AS distinct_stories
FROM stock_articles a,
     json_array_elements(a.content::json->'diagnostics'->'rejections') AS r
WHERE a.trading_date >= CURRENT_DATE - 30
  AND a.content IS NOT NULL
  AND a.content::json->'diagnostics'->'rejections' IS NOT NULL
GROUP BY r->>'rejected_by'
ORDER BY times DESC
""",
    ),
    SavedQuery(
        key="search_budget_usage",
        category="ai_tuning",
        title="Are stories hitting the search budget ceiling?",
        question="Search usage per story: how many searches were used vs. "
                "the ceiling. Stories that use the full budget and still "
                "find no cause are budget-bound — raising "
                "AI_ARTICLE_MAX_SEARCHES may help. Stories that stop early "
                "chose to stop — the prompt or domains setting is the "
                "constraint.",
        teaches="Comparing two JSON fields. 'Budget-bound' means the story "
                "used its full ceiling and still failed. 'Stopped early' "
                "means it found what it needed or gave up before the limit.",
        sql="""
SELECT
  t.symbol,
  a.status,
  (a.content::json->'diagnostics'->>'searches_used')::int AS searches_used,
  (a.content::json->'diagnostics'->>'max_searches')::int AS max_searches,
  CASE
    WHEN (a.content::json->'diagnostics'->>'searches_used')::int
       >= (a.content::json->'diagnostics'->>'max_searches')::int
    THEN 'budget-bound'
    ELSE 'stopped early'
  END AS budget_status,
  (a.content::json->'diagnostics'->>'facts_found')::int AS facts_found,
  (a.content::json->'diagnostics'->>'facts_kept')::int AS facts_kept,
  a.trading_date
FROM stock_articles a
JOIN tickers t ON t.id = a.ticker_id
WHERE a.trading_date >= CURRENT_DATE - 14
  AND a.content IS NOT NULL
  AND a.content::json->'diagnostics'->>'searches_used' IS NOT NULL
ORDER BY a.trading_date DESC, searches_used DESC
""",
    ),
    SavedQuery(
        key="fact_drop_reasons",
        category="ai_tuning",
        title="Why are facts being dropped by validation?",
        question="Which validation rules drop the most facts, and which "
                "domains are being rejected. If 'domain_not_allowed' "
                "dominates, check AI_NEWS_DOMAINS. If 'url_not_in_search_"
                "results' dominates, the model is fabricating URLs.",
        teaches="json_array_elements to unpack a nested JSON array, then "
                "GROUP BY on the extracted rule name. This is how you find "
                "the dominant validation problem without reading each story.",
        sql="""
SELECT
  d->>'rule' AS validation_rule,
  COUNT(*) AS facts_dropped,
  COUNT(DISTINCT a.id) AS stories_affected,
  (array_agg(DISTINCT d->>'detail'))[1:3] AS example_details
FROM stock_articles a,
     json_array_elements(a.content::json->'diagnostics'->'dropped') AS d
WHERE a.trading_date >= CURRENT_DATE - 14
  AND a.content IS NOT NULL
  AND a.content::json->'diagnostics'->'dropped' IS NOT NULL
GROUP BY d->>'rule'
ORDER BY facts_dropped DESC
""",
    ),
    SavedQuery(
        key="story_cost_per_day",
        category="ai_tuning",
        title="How much is the AI costing per day?",
        question="Token usage and estimated cost per day. The cost estimate "
                "uses Haiku 3.5 pricing ($0.25/M input, $1.25/M output) — "
                "adjust if your model differs. Searches are ~$0.02 each.",
        teaches="Date casting with aggregation, and inline cost arithmetic. "
                "The search cost dominates, so the total is only useful "
                "when you include it.",
        sql="""
SELECT
  trading_date,
  COUNT(*) AS stories,
  SUM(input_tokens) AS total_input_tokens,
  SUM(output_tokens) AS total_output_tokens,
  ROUND((SUM(input_tokens) * 0.25 / 1000000 +
         SUM(output_tokens) * 1.25 / 1000000)::numeric, 4) AS llm_cost_usd,
  SUM((content::json->'diagnostics'->>'searches_used')::int) AS total_searches,
  ROUND((SUM((content::json->'diagnostics'->>'searches_used')::int) * 0.02)::numeric, 4)
    AS search_cost_usd
FROM stock_articles
WHERE trading_date >= CURRENT_DATE - 14
  AND content IS NOT NULL
GROUP BY trading_date
ORDER BY trading_date DESC
""",
    ),
    SavedQuery(
        key="retry_trend",
        category="ai_tuning",
        title="Is the retry rate trending up or down?",
        question="Per-day retry statistics over the last 30 days. A rising "
                "retry rate means the prompt or the provider is degrading. "
                "A rising 'saved_by_retry' rate means the retries are earning "
                "their cost. A rising 'skipped' rate means the budget is too "
                "tight.",
        teaches="Multiple conditional aggregates side by side for a trend "
                "view. Each column is a different question about the same "
                "day.",
        sql="""
SELECT
  trading_date,
  COUNT(*) AS total,
  COUNT(CASE WHEN status = 'ready'
             AND (content::json->'diagnostics'->>'write_attempts')::int = 1
        THEN 1 END) AS first_try,
  COUNT(CASE WHEN status = 'ready'
             AND (content::json->'diagnostics'->>'write_attempts')::int > 1
        THEN 1 END) AS saved_by_retry,
  COUNT(CASE WHEN status = 'failed'
             AND content::json->'diagnostics'->>'rejections' IS NOT NULL
        THEN 1 END) AS failed_after_retry,
  COUNT(CASE WHEN content::json->'diagnostics'->>'retry_skipped' IS NOT NULL
        THEN 1 END) AS skipped_no_time,
  ROUND(AVG((content::json->'diagnostics'->>'write_attempts')::int)::numeric, 2)
    AS avg_attempts
FROM stock_articles
WHERE trading_date >= CURRENT_DATE - 30
  AND content IS NOT NULL
GROUP BY trading_date
ORDER BY trading_date DESC
""",
    ),
    SavedQuery(
        key="age_group_failure_rate",
        category="ai_tuning",
        title="Which age group fails the most?",
        question="Age groups dropped by the language check or the reviewer. "
                "If ages 3-5 consistently fail, the writing prompt for that "
                "age needs work — not more retries.",
        teaches="json_each_text to unpack a JSON object's keys and values. "
                "The 'versions_dropped_by_language_check' field maps each "
                "failing age group to its problems.",
        sql="""
SELECT
  kv.key AS age_group,
  COUNT(*) AS times_dropped,
  COUNT(DISTINCT a.id) AS stories_affected
FROM stock_articles a,
     json_each_text(
       a.content::json->'diagnostics'->'versions_dropped_by_language_check'
     ) AS kv
WHERE a.trading_date >= CURRENT_DATE - 30
  AND a.content IS NOT NULL
  AND a.content::json->'diagnostics'->'versions_dropped_by_language_check' IS NOT NULL
GROUP BY kv.key
ORDER BY times_dropped DESC
""",
    ),

    # --------------------------------------------------------------- prices
    SavedQuery(
        key="price_freshness",
        category="prices",
        title="Is the price data current?",
        question="Latest stored close per ticker, and how far behind it is. "
                "Anything above a few days is why an alert percentage would "
                "disagree with a public quote.",
        teaches="MAX() inside a GROUP BY, and doing arithmetic on dates. "
                "`CURRENT_DATE - MAX(date)` gives a number of days.",
        sql="""
SELECT t.symbol, t.type,
       MAX(d.date) AS latest_close,
       CURRENT_DATE - MAX(d.date) AS days_behind,
       COUNT(*) AS days_stored
FROM tickers t
JOIN daily_closes d ON d.ticker_id = t.id
GROUP BY t.symbol, t.type
ORDER BY days_behind DESC, t.symbol
""",
    ),
    SavedQuery(
        key="price_coverage_gaps",
        category="prices",
        title="Which trading days are thin?",
        question="How many tickers got a close on each of the last 30 days. A "
                "day well below the others is a partial ingest.",
        teaches="Counting DISTINCT values inside a group — the way to ask "
                "'how many different things' rather than 'how many rows'.",
        sql="""
SELECT date, COUNT(DISTINCT ticker_id) AS tickers_with_a_close
FROM daily_closes
WHERE date >= CURRENT_DATE - 30
GROUP BY date
ORDER BY date DESC
""",
    ),
    SavedQuery(
        key="suspicious_moves",
        category="prices",
        title="Any price that jumped implausibly?",
        question="Day-over-day moves above 25%, which are usually a split or "
                "bad provider data rather than real.",
        teaches="A window function. LAG() reads the previous row in the same "
                "ticker's history, which is how you compare a row to the one "
                "before it. The outer query filters on it — you cannot filter "
                "a window function in the same WHERE, which is why this needs "
                "two levels.",
        sql="""
SELECT symbol, date, prev_date, close, prev_close,
       ROUND((((close - prev_close) / prev_close) * 100)::numeric, 2) AS pct
FROM (
  SELECT t.symbol, d.date, d.close,
         LAG(d.close) OVER (PARTITION BY d.ticker_id ORDER BY d.date) AS prev_close,
         LAG(d.date)  OVER (PARTITION BY d.ticker_id ORDER BY d.date) AS prev_date
  FROM daily_closes d
  JOIN tickers t ON t.id = d.ticker_id
) AS withprev
WHERE prev_close IS NOT NULL
  AND prev_close > 0
  AND ABS((close - prev_close) / prev_close) > 0.25
ORDER BY date DESC
LIMIT 50
""",
    ),
    SavedQuery(
        key="snapshot_staleness",
        category="prices",
        title="Is the current view up to date?",
        question="When each price snapshot was computed. These are replaced "
                "wholesale every batch, so they should all share one "
                "timestamp from today.",
        teaches="Reading a table whose contents are rewritten rather than "
                "appended. There is no history here to find.",
        sql="""
SELECT t.symbol, s.price, s.previous_price,
       ROUND((((s.price - s.previous_price) / s.previous_price) * 100)::numeric, 2)
         AS pct_change,
       s.captured_at
FROM price_snapshots s
JOIN tickers t ON t.id = s.ticker_id
WHERE s.previous_price IS NOT NULL AND s.previous_price > 0
ORDER BY ABS((s.price - s.previous_price) / s.previous_price) DESC
""",
    ),

    # --------------------------------------------------------------- people
    SavedQuery(
        key="user_overview",
        category="people",
        title="Who uses this, and how much?",
        question="Every user with watchlist size, rule count and last login.",
        teaches="Several LEFT JOINs with COUNT(DISTINCT …). The DISTINCT is "
                "load-bearing: joining two one-to-many tables multiplies the "
                "rows, and a plain COUNT would report the product rather than "
                "the count.",
        sql="""
SELECT u.id AS user_id, u.email, u.created_at, u.last_login_at,
       u.notify_email_confirmed,
       COUNT(DISTINCT w.ticker_id) AS watching,
       COUNT(DISTINCT r.id) AS rules,
       COUNT(DISTINCT CASE WHEN r.enabled THEN r.id END) AS rules_enabled
FROM users u
LEFT JOIN watchlist_items w ON w.user_id = u.id
LEFT JOIN notification_rules r ON r.user_id = u.id
GROUP BY u.id, u.email, u.created_at, u.last_login_at, u.notify_email_confirmed
ORDER BY u.created_at DESC
""",
    ),
    SavedQuery(
        key="watchlist_no_rules",
        category="people",
        title="Who is watching stocks but gets no alerts?",
        question="Users with a watchlist and no enabled rule. They see the "
                "recap but will never get an alert, which usually means "
                "they never finished setting up.",
        teaches="NOT EXISTS against a filtered subquery — note the `enabled` "
                "check is inside it, so 'has only disabled rules' counts as "
                "'has none'.",
        sql="""
SELECT u.id AS user_id, u.email, COUNT(w.id) AS watching, u.created_at
FROM users u
JOIN watchlist_items w ON w.user_id = u.id
WHERE NOT EXISTS (
        SELECT 1 FROM notification_rules r
        WHERE r.user_id = u.id AND r.enabled
      )
GROUP BY u.id, u.email, u.created_at
ORDER BY watching DESC
""",
    ),
    SavedQuery(
        key="popular_tickers",
        category="people",
        title="Which symbols matter most?",
        question="Tickers ranked by how many people watch them.",
        teaches="Aggregating the many side of a relationship and ordering by "
                "the count. HAVING filters groups, where WHERE filters rows.",
        sql="""
SELECT t.symbol, t.name, t.type,
       COUNT(DISTINCT w.user_id) AS watchers,
       COUNT(DISTINCT r.user_id) AS with_rules
FROM tickers t
LEFT JOIN watchlist_items w ON w.ticker_id = t.id
LEFT JOIN notification_rules r ON r.ticker_id = t.id
GROUP BY t.symbol, t.name, t.type
HAVING COUNT(DISTINCT w.user_id) > 0
ORDER BY watchers DESC, t.symbol
""",
    ),
    SavedQuery(
        key="money_idea_progress",
        category="people",
        title="How far through the curriculum is everyone?",
        question="Money Ideas sent per user, with the most recent one.",
        teaches="MAX() over text and a count in the same group — 'how many, "
                "and the latest'.",
        sql="""
SELECT u.id AS user_id, u.email,
       COUNT(*) AS ideas_sent,
       MAX(m.trading_date) AS latest_day,
       MAX(m.idea_key) AS an_idea_key
FROM money_idea_sends m
JOIN users u ON u.id = m.user_id
GROUP BY u.id, u.email
ORDER BY ideas_sent DESC
""",
    ),

    # --------------------------------------------------------------- errors
    SavedQuery(
        key="recent_errors",
        category="errors",
        title="What has gone wrong lately?",
        question="WARNING and above, newest first.",
        teaches="Reading the durable log table. These rows are pruned on "
                "LOG_LIFETIME, so there is a horizon beyond which this is "
                "empty by design, not because nothing happened.",
        sql="""
SELECT created_at, level, logger, message
FROM log_entries
ORDER BY created_at DESC
LIMIT 100
""",
    ),
    SavedQuery(
        key="error_hotspots",
        category="errors",
        title="Where do errors cluster?",
        question="Which module logs the most problems, and when it last did.",
        teaches="GROUP BY on a text column to find hotspots. The logger name "
                "is the Python module path, so the answer points at a file.",
        sql="""
SELECT logger, level, COUNT(*) AS entries,
       MIN(created_at) AS first_seen,
       MAX(created_at) AS last_seen
FROM log_entries
GROUP BY logger, level
ORDER BY entries DESC
LIMIT 50
""",
    ),
    SavedQuery(
        key="job_narration",
        category="errors",
        title="What did the jobs say they were doing?",
        question="The INFO-level narration from the jobs, newest first. This "
                "is where to look when nothing errored but the outcome was "
                "still wrong.",
        teaches="LIKE with a wildcard for prefix matching. Note "
                "INFO_LOG_LIFETIME is the shortest retention window in the "
                "system — this table only goes back days.",
        sql="""
SELECT created_at, logger, message
FROM info_logs
WHERE logger LIKE 'app.jobs%'
ORDER BY created_at DESC
LIMIT 150
""",
    ),
]

BY_KEY: dict[str, SavedQuery] = {q.key: q for q in QUERIES}


def catalogue() -> dict:
    """The saved queries, grouped for display."""
    labels = dict(CATEGORIES)
    return {
        "categories": [
            {
                "key": key,
                "label": labels[key],
                "queries": [q.as_dict() for q in QUERIES if q.category == key],
            }
            for key, _ in CATEGORIES
        ],
        "count": len(QUERIES),
        "dialect": "postgresql",
        "note": (
            "Written for Postgres, which is what production runs. Each query "
            "is a starting point — edit it, re-run it, and the result updates."
        ),
    }
