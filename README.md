# AI Stock Watcher

Email alerts when the markets do something you care about. Pick stocks and
index funds, define rules ("notify me if SPY drops more than 3%", "alert me
when AAPL hits a new monthly high"), and get a tidy email three times each US
trading day — or only when something actually triggers.

This repo contains the base scaffolding: backend, frontend, batch jobs, tests,
seed data, and deployment notes. It is intended as the starting point for a
small team to iterate on.

## Architecture at a glance

```
                ┌──────────────────────┐
   Browser ──▶  │  React + Vite (SPA)  │
                └─────────┬────────────┘
                          │ HTTPS / Bearer token
                ┌─────────▼────────────┐         ┌─────────────────┐
                │     FastAPI app      │ ───────▶│ SMTP (Mailtrap, │
                │   + APScheduler      │         │ SendGrid, etc.) │
                └─────────┬────────────┘         └─────────────────┘
                          │ SQLAlchemy
                ┌─────────▼────────────┐
                │ SQLite (dev) / Postgres (prod, Railway) │
                └──────────────────────┘
                          ▲
                          │ 9:35 / 12:30 / 16:05 ET
                ┌─────────┴────────────┐
                │  Batch jobs (in-     │
                │  process scheduler): │
                │  1. ingest (yfinance)│
                │  2. compute trends   │
                │  3. dispatch emails  │
                └──────────────────────┘
```

## Business rules

These are the rules the app is built around. They're worth reading even if
you only plan to touch the frontend.

**Identity.** A user is one row in `users`, keyed by email. There are no
passwords — sign-in is via a single-use magic link emailed to the user. The
link expires after 15 minutes and is single-use (the token-hash row is
marked `consumed_at` on first verify).

**Notification email vs. login email.** The address we send alerts TO
(`notify_email`) defaults to the login email but can be changed. Until the
new address is confirmed (via a separate token, 60-min TTL), the
`notify_email_confirmed` flag is `false` and the notification job will not
send to that user. After confirmation, the user receives a one-time
"notifications activated" message explaining when alerts will fire.

**Watchlist.** Many-to-many between users and tickers. The user can pick
from the seeded ~50 popular tickers or type in any other symbol — unknown
symbols are upserted into `tickers` as `is_seeded=false` and become eligible
for batch ingest immediately.

**Notification rules.** A rule is `(user, ticker, event_type)` — at most one
rule per combination. Supported event types:

- `price_change_range`: fires when % change vs. the previous snapshot is
  outside `[pct_low, pct_high]`. `pct_low < pct_high`, both required.
- `week_low` / `week_high`: today's price equals the rolling-7d low/high.
- `month_low` / `month_high`: rolling-30d low/high.
- `quarter_low` / `quarter_high`: rolling-91d low/high.
- `year_low` / `year_high`: rolling-365d low/high.

A rule can be enabled/disabled without deleting it. Users can edit their
rules at any time.

**Batch jobs.** Three runs each US trading day, at 09:35, 12:30, and 16:05
Eastern (configurable via `BATCH_JOB_TIMES_ET`), Mon–Fri only:

1. **Ingest** (`app/jobs/ingest.py`) — for every ticker on someone's
   watchlist, pull a 1-year daily history from yfinance, derive the current
   price + rolling lows/highs, and REPLACE the row in `price_snapshots`. The
   previous row's price becomes `previous_price` for diff calculations.
2. **Compute** (`app/jobs/compute.py`) — read `price_snapshots`, compute
   `(pct_change, period flags)` per `(user, ticker)` on every watchlist
   entry, REPLACE the rows in `trend_analyses`.
3. **Notify** (`app/jobs/notify.py`) — for every enabled rule whose latest
   trend triggers it, send an email (skipping users with
   `notify_email_confirmed=false`) and log the send in `notification_logs`
   so we don't re-send within a 3-hour dedup window.

**Data lifecycle.** `price_snapshots` and `trend_analyses` are wiped and
re-populated every batch — they are not durable history. `notification_logs`
is durable so we can audit & dedup.

## Tech stack

| Layer       | Tool                                            |
| ----------- | ----------------------------------------------- |
| Frontend    | React 18 + Vite + React Router (plain CSS)      |
| Backend     | FastAPI + SQLAlchemy 2.0 + Pydantic 2           |
| Auth        | Magic-link via signed token (itsdangerous)      |
| Data        | yfinance (Yahoo Finance, no API key needed)     |
| Scheduler   | APScheduler in-process, US/Eastern cron triggers |
| Email       | smtplib via env-driven config (Mailtrap in dev) |
| DB (dev)    | SQLite                                          |
| DB (prod)   | PostgreSQL (Railway-provided)                   |
| Hosting     | Frontend on Vercel · Backend on Railway          |

## Local setup

### 1. Backend

```bash
cd backend
python -m venv .venv
source .venv/bin/activate           # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                # then fill in SMTP if you want real emails
python -m app.db.seed               # populate the ~50 popular tickers
uvicorn app.main:app --reload
# → http://localhost:8000  ·  docs at /docs
```

If you don't configure SMTP, the dev server logs outgoing emails to the
console instead of sending — convenient for development.

For real email delivery in dev, sign up for [Mailtrap](https://mailtrap.io)
(free), copy the SMTP creds from one of its inboxes into `.env`, and every
email sent by the app will land in the Mailtrap UI.

### 2. Frontend

```bash
cd frontend
cp .env.example .env                # VITE_API_URL defaults to http://localhost:8000
npm install
npm run dev
# → http://localhost:5173
```

Sign in with your email, click the link in your inbox (or the dev console),
and you'll land on the dashboard.

## Running tests

```bash
cd backend
pytest
```

The test suite uses an in-memory SQLite DB and a fake SMTP capture, so
nothing touches the network. Current coverage: auth flow (request, verify,
single-use, email change + confirm), watchlist CRUD, rule CRUD with
validation, ticker auto-complete, trend math, and the full
ingest→compute→notify pipeline with a mocked price fetcher.

## Project layout

```
backend/
  app/
    api/                  FastAPI routers (auth, tickers, watchlist, rules, trends)
    core/                 settings, security (token signing)
    db/                   engine/session, seed script
    jobs/                 ingest, compute, notify, scheduler
    models/               SQLAlchemy ORM models
    schemas/              Pydantic request/response shapes
    services/             auth, confirm-email, email sender, trend math
    main.py               FastAPI app factory + lifespan
  tests/                  pytest suite (in-memory DB, captured emails)
  requirements.txt
  .env.example

frontend/
  src/
    pages/                Login, AuthCallback, Dashboard, Rules, Settings, ConfirmEmail
    components/           TickerAutocomplete
    lib/api.js            fetch wrapper + token storage
    App.jsx               router shell + auth guard
    main.jsx              app entry
    styles.css            single global stylesheet
  index.html
  package.json
  vite.config.js
  .env.example
```

## Configuration

All backend secrets and tunables go through `backend/app/core/settings.py`.
See `backend/.env.example` for the full list. Notable knobs:

| Var                      | Purpose                                       |
| ------------------------ | --------------------------------------------- |
| `SECRET_KEY`             | HMAC key for signed tokens. **Rotate per env.** |
| `DATABASE_URL`           | `sqlite:///./dev.db` locally; Postgres in prod |
| `MAGIC_LINK_TTL_MINUTES` | Magic-link expiry. Default 15.                |
| `BATCH_JOB_TIMES_ET`     | Comma-separated `HH:MM` in US/Eastern.        |
| `BATCH_JOBS_ENABLED`     | Set `false` in tests / web-only deploys.      |
| `SMTP_*`                 | Outbound mail. See `.env.example`.            |
| `CORS_ORIGINS`           | Comma-separated allowed origins for browser.   |

## Security notes

A short list of the things this base scaffold does — and a couple it doesn't:

- **No plaintext tokens at rest.** Magic-link and session tokens are stored
  only as SHA-256 hashes. A DB dump cannot be replayed against the auth API.
- **Salted token purposes.** `itsdangerous` serializers are salted with a
  purpose string, so a login-purpose token can't be reused as a
  confirm-email token.
- **Constant-time comparisons** on all hash equality checks.
- **No password storage** — magic-link only.
- **Single-use magic links** enforced by `consumed_at`.
- **CORS allowlist** via env var. No `*`.
- **Bearer-token auth** in the `Authorization` header (no cookies / no
  CSRF surface).
- **Rate limiting** on the API via `slowapi` (default 60 req/min/IP).
- **Email enumeration resistance.** `/auth/request-link` returns 204 whether
  or not the email exists.
- **Not yet implemented and worth adding before public launch:** structured
  audit logging, CAPTCHA on the request-link endpoint, IP-based rate limits
  on auth specifically, content-security-policy headers on the SPA,
  monitoring/alerting on the batch scheduler.

## Deployment

### Backend → Railway

1. Push this repo to GitHub.
2. New Railway project → "Deploy from GitHub" → pick `AI-stock-watcher`.
3. Add a PostgreSQL plugin. Railway sets `DATABASE_URL` automatically.
4. Set the other env vars from `backend/.env.example` (especially
   `SECRET_KEY`, `FRONTEND_BASE_URL`, `SMTP_*`, `CORS_ORIGINS`).
5. Set the start command:

   ```
   cd backend && pip install -r requirements.txt && uvicorn app.main:app --host 0.0.0.0 --port $PORT
   ```

6. After first deploy, exec `python -m app.db.seed` once to seed tickers.

### Frontend → Vercel

1. New Vercel project → import the same repo.
2. Set the root directory to `frontend`.
3. Build command: `npm run build`. Output dir: `dist`.
4. Add the env var `VITE_API_URL` pointing at the Railway backend URL.
5. Deploy.

## Roadmap (suggestions)

- Switch SQLite → Postgres for `DATABASE_URL` parity in dev (docker-compose).
- Move the batch scheduler out-of-process (Celery + Redis) when traffic
  warrants per-job retries and observability.
- Add Alembic for DB migrations once the schema starts to drift in prod.
- Tighten the auth rate limits and add CAPTCHA on `/auth/request-link`.
- Build an admin view for viewing recent NotificationLog rows.

## Contributing

- Match the existing style: typed signatures, clear docstrings on services
  and modules.
- Add or update tests for every behaviour change. The bar is "could a new
  contributor break this and not notice?".
- Keep new business rules documented in this README's "Business rules"
  section.
