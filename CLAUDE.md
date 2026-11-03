# Claude Code project context — Little Money Monkey

This file is auto-loaded by Claude Code as project memory. Keep it short,
factual, and current — anything stale here makes Claude worse, not better.

## What this app does

Email alerts when stocks the user is watching cross rules they defined
("notify me if SPY drops more than 3%", "alert me when AAPL hits a new
monthly high"). FastAPI backend on Railway, Vite/React SPA on Vercel,
SQLite locally / Postgres in prod, magic-link auth (no passwords).

## Stack

- **Backend:** FastAPI · SQLAlchemy 2.0 (Mapped/mapped_column ORM) · Pydantic 2
  · APScheduler (in-process) · itsdangerous (token signing) · slowapi
  (rate-limit) · psycopg 3 (binary) · pytest
- **Frontend:** React 18 · Vite · React Router · plain CSS, no Tailwind
- **Email:** smtplib via env-driven SMTP (Gmail App Password in dev/prod)
- **Python:** 3.14 (locally). Dependencies pinned to versions with cp314
  wheels — see `backend/requirements.txt`. Do NOT downgrade `pydantic` below
  2.12 or `sqlalchemy` below 2.0.50; older versions break on 3.14.

## Layout

```
backend/
  app/
    api/        FastAPI routers (auth, tickers, watchlist, rules, trends)
    core/       settings (pydantic-settings), security (token signing)
    db/         engine/session, seed
    jobs/       ingest, compute, notify, scheduler, daily_summary,
                cleanup, heartbeat, intraday, recap
    models/     SQLAlchemy ORM (single file: models/models.py)
    schemas/    Pydantic request/response shapes
    services/   auth, confirm_email, emailer, trend, alerts,
                articles/ (AI "why it moved" stories)
                money_ideas.py  (20-idea financial literacy curriculum)
    main.py     FastAPI app factory + lifespan
  tests/        pytest suite (in-memory SQLite, captured emails)
  requirements.txt
  .env.example
frontend/
  src/{pages,components,lib}/  …
(DEPLOYMENT.md  ops guide — kept locally, untracked; see below)
```

## Architecture rules (worth knowing before editing)

- **Identity** keyed by email. No passwords. Magic-link tokens stored
  HASHED only — plaintext only ever exists in the email.
- **Notification email vs login email** are separate. Notification email
  starts equal to login email but can be changed; changes require a
  confirmation token before the notify job will send to that user.
- **Three batch jobs** run Mon-Fri at 09:35 / 12:30 / 16:05 US/Eastern:
  ingest → compute → notify. They REPLACE `price_snapshots` and
  `trend_analyses` each run; only `notification_logs` is durable.
- **A rule is `(user, ticker, event_type)`** — at most one of each
  combination per user. Disabling without deleting is supported.
- **Datetimes are naive UTC** everywhere (SQLite can't store tz info).
  Use `utcnow()` from `app.models`.
- **DB driver:** `app/db/database.py::_build_engine` rewrites
  `postgresql://` → `postgresql+psycopg://` so Railway's auto-injected URL
  works with psycopg 3. Don't add psycopg2.

## Observability (recent addition)

- `LogEntry` table persists WARNING+ logs (see `app/services/alerts.py`
  → `DBLogHandler`).
- ERROR+ records auto-email admin via `EmailAlertHandler`, deduped 15 min
  per `(logger, first 200 chars of message)`. **Use `%s` placeholders, NOT
  f-strings, in log calls** — f-strings defeat dedup if the message includes
  varying counters / timestamps.
- Business events use the explicit `notify_admin(event, ...)` path in
  `app.services.alerts` (signup, first-ever notification, upstream-API-down,
  daily summary). Not deduped.
- `recap` job (Mon-Fri 16:30 ET): daily watchlist recap email per user,
  with the biggest mover's kids' story (shared `StockArticle`, so stories
  aren't written twice). `RecapOptOut` = user switched it off; `RecapSend`
  makes it once per user per day.
- Two daily background jobs: `cleanup` (deletes `log_entries` older than
  `LOG_LIFETIME`) and `daily_summary` (HEALTHY/DEGRADED/UNHEALTHY digest).
  `heartbeat` runs hourly on weekdays and alerts once if the last
  *scheduled* pipeline time (30 min grace) never started. The daily summary
  counts runs from `job_runs`, not log text.

## AI stories (`app/services/articles/`)

- PRICE_CHANGE_RANGE alerts compare against the previous trading day's
  close only (the 10-minute tick signal was removed: it duplicated alerts).
- Day-over-day PRICE_CHANGE_RANGE alerts get a kid-friendly "why it moved"
  story (four age groups; parents pick in Settings). Off unless
  `AI_ARTICLES_ENABLED=true`. `AI_PROVIDER` (gemini | anthropic) with
  optional `AI_FALLBACK_PROVIDER`; keys `GEMINI_API_KEY` / `ANTHROPIC_API_KEY`
  (`providers.py`; fallback logic in `service._generate_with_ai`).
- Pipeline: research (Claude web search by default, or Finnhub news) →
  validate facts in code (quotes word-for-word, links/dates from our data)
  → write → `language.py` check → independent AI review.
  Fail closed: anything unchecked is dropped; the alert still goes out.
- Only moves ≥ `AI_ARTICLE_MIN_PCT_STOCK` (1%) / `AI_ARTICLE_MIN_PCT_FUND`
  (0.5%, ETFs and indexes) get an AI story; smaller ones get the fixed
  per-age "small move" note in `render.py` (no AI cost).
  No cause found but the stock moved with the S&P 500
  (`service.moved_with_market`) → fixed "whole-market day" note.
- The AI never writes numbers, links or the disclaimer — `render.py` does,
  and escapes all AI text. Keep it that way.
- Key/billing problems only email the admin (`report_key_problem`); users
  just get a plain alert. `health.py` checks keys at startup (free calls) and
  emails the setup + cost whenever any AI setting or key changes; prices
  live in `costs.py` (update `PRICES` when providers change them).
- All alert delivery is wrapped in try/except — alerting must NEVER raise
  (recursion-safe).

## Money Ideas (`app/services/money_ideas.py`)

- Every daily recap includes one Money Idea of the Day: a 20-idea
  financial-literacy curriculum for families (companies, shares, risk,
  compounding, etc.). Text is ours, not AI-written — free, reviewed once.
- Ideas are sent basics-first (`FOUNDATION` = 4). After the foundation,
  the next unsent idea that fits the day (SMALL_DAY / MARKET_DAY /
  BIG_MOVE tag) is preferred. After the full cycle, it picks from the 5
  oldest-sent ideas.
- `MoneyIdeaSend` records what each user received and when.
- `GET /money-ideas` returns the user's history (newest first), with
  age-appropriate versions. Frontend: `/money-ideas` page + nav link.
- Language and readability for every version are checked by the same rules
  as AI stories (see tests).

## Conventions

- **Logging:** `log = logging.getLogger(__name__)` per module, never the
  root. Use `log.info` for routine, `log.warning` for per-item failures,
  `log.error` for things you want to be alerted about, `log.critical` for
  app-down conditions.
- **DB sessions** in jobs follow the `own = db is None; if own: db = ...;
  try: …; finally: if own: db.close()` pattern so tests can pass in an
  in-memory session.
- **Types:** `from __future__ import annotations` everywhere; use
  `str | None` (no `Optional`); `Mapped[X]` + `mapped_column` for ORM cols.
- **Pydantic v2:** `model_config = SettingsConfigDict(...)`, not the v1
  `Config` class.

## Running things

```bash
# Backend setup (Python 3.14)
cd backend
python3.14 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # fill in SMTP if you want real emails
python -m app.db.seed
.venv/bin/python -m uvicorn app.main:app --reload
# → http://localhost:8000 · docs at /docs

# Tests (use in-memory SQLite, capture emails, no network)
cd backend && pytest

# Frontend
cd frontend && npm install && npm run dev
# → http://localhost:5173
```

## Things to AVOID

- Don't downgrade `pydantic` or `sqlalchemy` (see Python 3.14 note above).
- Don't add `psycopg2` — we use psycopg 3 with a driver-rewrite.
- Don't email outside the `app.services.emailer.send_email` wrapper — it
  handles the "SMTP not configured" dev fallback.
- Don't use f-strings inside `log.error/critical` calls (defeats alert
  dedup).
- Don't write durable history into `price_snapshots` / `trend_analyses` —
  they get wiped every batch by design.

## When you're unsure

`DEPLOYMENT.md` documents env vars, alerting behaviour, daily-summary
fields, retention units, and troubleshooting for every prod failure mode
we've actually hit. Check there before guessing.

It is **not in the repository** — it is kept locally and gitignored,
because this repo is public and the guide describes the real deployment.
Expect it at the repo root on the maintainer's machine. If it is absent,
say so rather than inventing deployment steps.
