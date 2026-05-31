# Claude Code project context — AI Stock Watcher

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
                cleanup, heartbeat
    models/     SQLAlchemy ORM (single file: models/models.py)
    schemas/    Pydantic request/response shapes
    services/   auth, confirm_email, emailer, trend, alerts
    main.py     FastAPI app factory + lifespan
  tests/        pytest suite (in-memory SQLite, captured emails)
  requirements.txt
  .env.example
frontend/
  src/{pages,components,lib}/  …
DEPLOYMENT.md   Railway + Vercel + alerts ops guide
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
- Two daily background jobs: `cleanup` (deletes `log_entries` older than
  `LOG_LIFETIME`) and `daily_summary` (HEALTHY/DEGRADED/UNHEALTHY digest).
  `heartbeat` runs hourly on weekdays and alerts if no successful pipeline
  in 26h.
- All alert delivery is wrapped in try/except — alerting must NEVER raise
  (recursion-safe).

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
