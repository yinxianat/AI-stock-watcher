# Deployment

How to ship AI Stock Watcher to production: backend on Railway, frontend on
Vercel, transactional email via Gmail (or any SMTP provider). Read top-to-bottom
on first deploy; use the Troubleshooting section afterwards.

## Architecture

```
  Vercel (Vite SPA)  ──HTTPS──▶  Railway (FastAPI + APScheduler)
                                          │
                                          ├──▶ Railway Postgres ◀── log_entries
                                          ├──▶ SMTP ──▶ user notifications
                                          └──▶ SMTP ──▶ admin alerts + daily summary
                                                       (→ ALERT_RECEIVER)
```

The scheduler runs **in-process** with FastAPI, so the Railway service must
stay always-on — don't put it behind a serverless cold-start. Five scheduled
jobs:

- **Main pipeline** — Mon–Fri at 09:35 / 12:30 / 16:05 US/Eastern (configurable
  via `BATCH_JOB_TIMES_ET`). Runs ingest → compute → notify.
- **Daily summary** — every day at `DAILY_SUMMARY_TIME_ET` (default 17:30 ET).
  Emails a HEALTHY / DEGRADED / UNHEALTHY digest covering every service.
- **Pipeline heartbeat** — hourly on weekdays. Alerts if no successful pipeline
  in the last 26h.
- **Log retention cleanup** — daily 03:15 ET. Prunes `log_entries` older than
  `LOG_LIFETIME`.

## Prerequisites

- GitHub repo pushed to `yinxianat/AI-stock-watcher`.
- Railway account (<https://railway.app>) — free tier is fine to start.
- Vercel account (<https://vercel.com>) — Hobby plan is fine.
- A Gmail account with 2-Step Verification turned on, plus an
  [App Password](https://myaccount.google.com/apppasswords). See
  [Gmail SMTP](#gmail-smtp-setup) below.

## Backend → Railway

1. **Create the project.** Railway dashboard → **New Project** → **Deploy from
   GitHub** → pick `yinxianat/AI-stock-watcher`.
2. **Service settings** (Settings tab):
   - Root Directory: `backend`
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
3. **Add Postgres.** Project canvas → **+ New** → **Database** → **PostgreSQL**.
   Railway auto-injects `DATABASE_URL` into your service. The app's
   `_build_engine()` rewrites `postgresql://` to `postgresql+psycopg://`
   automatically, so no manual URL surgery is needed.
4. **Set environment variables** (Variables tab — see
   [Environment Variables](#environment-variables) for the full list). At
   minimum:

   ```
   SECRET_KEY=<openssl rand -hex 32>
   APP_ENV=production
   APP_DEBUG=false
   FRONTEND_BASE_URL=https://<your-vercel-domain>.vercel.app
   CORS_ORIGINS=https://<your-vercel-domain>.vercel.app
   SMTP_HOST=smtp.gmail.com
   SMTP_PORT=587
   SMTP_USERNAME=yinxiadev@gmail.com
   SMTP_PASSWORD=<16-char Gmail App Password>
   SMTP_FROM=AI Stock Watcher <yinxiadev@gmail.com>
   SMTP_USE_TLS=true
   # Admin alerts + daily summary — leave ALERT_RECEIVER blank to fall back
   # to SMTP_USERNAME (the same Gmail you send from).
   ALERT_RECEIVER=
   ALERTS_ENABLED=true
   LOG_DB_PERSISTENCE=true
   LOG_LIFETIME=30d
   DAILY_SUMMARY_TIME_ET=17:30
   DAILY_SUMMARY_ENABLED=true
   ```

   You don't know the Vercel domain yet — leave placeholders, deploy, then
   update after the frontend is live.
5. **Expose a public domain.** Settings → Networking → **Generate Domain**.
   Copy the `*.up.railway.app` URL — the frontend needs it.
6. **Seed the database once.** Service ⋯ menu → **Run command** →
   `python -m app.db.seed`. This populates the ~50 popular tickers. Re-running
   it is safe (it upserts).
7. **Smoke test.** Visit `https://<your-railway-domain>/docs`. The FastAPI
   Swagger UI should load. Try `GET /tickers/search?q=AAPL` — should return
   Apple.

8. **Seed dat.** After first successful deploy, run the seed once: Railway → service → ⋯ menu → Run command → python -m app.db.seed.


## Frontend → Vercel

1. **Import the repo.** Vercel → **Add New → Project** → import
   `yinxianat/AI-stock-watcher`.
2. **Project settings:**
   - Root Directory: `frontend`
   - Framework Preset: Vite (auto-detected)
   - Build Command: `npm run build`
   - Output Directory: `dist`
3. **Environment Variables:**

   ```
   VITE_API_URL=https://<your-railway-domain>
   ```

   No trailing slash. Vite bakes this into the build, so changing it later
   requires a redeploy.
4. **Deploy.** Vercel hands you a `<project>.vercel.app` URL.
5. **Close the loop on Railway.** Update `FRONTEND_BASE_URL` and
   `CORS_ORIGINS` on the Railway service to the Vercel URL, then trigger a
   redeploy (Deployments → ⋯ → **Redeploy**).

## Post-deploy smoke test

1. Open the Vercel URL.
2. Sign in with your email. Check inbox — magic link should arrive within a
   few seconds.
3. Click the link → land on the dashboard.
4. Add a ticker to your watchlist, create a rule, save.
5. Wait for the next batch run (or trigger one manually — see
   [Manually running a batch job](#manually-running-a-batch-job)).

If any step fails, jump to [Troubleshooting](#troubleshooting).

## Environment variables

| Var | Where | Purpose | Notes |
| --- | --- | --- | --- |
| `SECRET_KEY` | Railway | HMAC key for signed magic-link tokens | Rotate per env. `openssl rand -hex 32`. |
| `DATABASE_URL` | Railway (auto) | Postgres connection string | Auto-injected by the Postgres plugin. Scheme is rewritten in code. |
| `APP_ENV` | Railway | `development` / `production` / `test` | Set to `production`. |
| `APP_DEBUG` | Railway | Verbose error pages | `false` in prod. |
| `FRONTEND_BASE_URL` | Railway | Used in magic-link emails | Must match Vercel domain exactly, scheme included. |
| `MAGIC_LINK_TTL_MINUTES` | Railway | Magic-link expiry | Default 15. |
| `EMAIL_CONFIRM_TTL_MINUTES` | Railway | Email-change confirm token TTL | Default 60. |
| `SMTP_HOST` / `SMTP_PORT` | Railway | Outbound mail | `smtp.gmail.com:587` for Gmail. |
| `SMTP_USERNAME` / `SMTP_PASSWORD` | Railway | SMTP auth | For Gmail: the App Password, NOT your Google password. |
| `SMTP_FROM` | Railway | From: header | Must match `SMTP_USERNAME` for Gmail or it gets rewritten. |
| `SMTP_USE_TLS` | Railway | STARTTLS | `true` for Gmail on 587. |
| `BATCH_JOB_TIMES_ET` | Railway | Cron times in US/Eastern | Default `09:35,12:30,16:05`. |
| `BATCH_JOBS_ENABLED` | Railway | Toggle scheduler | `true` in prod, `false` for tests. |
| `CORS_ORIGINS` | Railway | Comma-separated allowed origins | Must include the Vercel domain. No `*` in prod. |
| `ALERT_RECEIVER` | Railway | Inbox for admin alerts + daily summary | Blank ⇒ falls back to `SMTP_USERNAME`. |
| `ALERTS_ENABLED` | Railway | Master kill-switch for all admin email | `true` in prod. Set `false` for maintenance windows. |
| `LOG_DB_PERSISTENCE` | Railway | Persist WARNING+ logs to `log_entries` | `true` in prod. `false` for SQLite-only sandboxes. |
| `LOG_LIFETIME` | Railway | How long `log_entries` rows are kept | `30d` default. Units: `s/m/h/d/w/M/y` (lowercase `m`=minutes, `M`=month). Bare int = days. |
| `DAILY_SUMMARY_TIME_ET` | Railway | When the daily digest emails | `HH:MM` in US/Eastern. Default `17:30` (after the 16:05 batch). |
| `DAILY_SUMMARY_ENABLED` | Railway | Toggle the digest | `true` in prod. |
| `VITE_API_URL` | Vercel | Backend base URL the SPA calls | No trailing slash. Baked at build time. |

## Gmail SMTP setup

Gmail killed plain-password SMTP in May 2022. You need an **App Password**,
which only exists if 2-Step Verification is on.

1. Turn on 2FA: <https://myaccount.google.com/signinoptions/twosv>.
2. Generate App Password: <https://myaccount.google.com/apppasswords>. Name it
   "AI Stock Watcher". Google returns a 16-character string like
   `abcd efgh ijkl mnop` (spaces optional).
3. Use that string as `SMTP_PASSWORD`. `SMTP_USERNAME` and `SMTP_FROM` must
   both be your full Gmail address.

Test locally before deploying:

```bash
.venv/bin/python -c "
import smtplib
s = smtplib.SMTP('smtp.gmail.com', 587); s.starttls()
s.login('you@gmail.com', 'abcdefghijklmnop')
print('auth OK'); s.quit()
"
```

If `/apppasswords` redirects you back to account settings, either 2FA isn't
fully active or you're on a Google Workspace account whose admin disabled App
Passwords. For Workspace, ask the admin to allow it, or switch to a
transactional provider (Mailtrap for dev, Resend / SendGrid / Postmark for
prod).

## Logging & alerts

The app persists every WARNING+ log to the `log_entries` Postgres table (via
a custom `DBLogHandler`) and emails the admin on ERROR+ records (via
`EmailAlertHandler`). Two alert channels deliberately use different subject
prefixes so you can filter in Gmail:

| Subject prefix | Channel | Triggered by | Deduped? |
| --- | --- | --- | --- |
| `[AISW ALERT]` | Error | ERROR/CRITICAL log records | 15-min window per `(logger, message)` |
| `[AISW EVENT]` | Event | Explicit business events (signup, first notification) | No |
| `[AISW DAILY]` | Summary | Daily summary job | No |

### What you'll get emailed

- **Pipeline failures** — `[AISW ALERT] ERROR app.jobs.scheduler: Pipeline
  stage 'ingest' failed`. Subject pinpoints which stage broke.
- **Upstream API down** — `[AISW ALERT] CRITICAL app.jobs.ingest: ...
  upstream price API appears down`. Fires when ingest tried tickers and got
  zero usable responses.
- **SMTP storm** — `[AISW ALERT] ERROR app.jobs.notify: ... SMTP likely
  broken`. Fires after ≥5 email send failures in a single notify run.
- **Pipeline heartbeat miss** — `[AISW ALERT] CRITICAL app.jobs.heartbeat:
  No successful pipeline run in the last 26h`. Fires if the hourly weekday
  health-check finds no recent success row in `log_entries`.
- **New user signup** — `[AISW EVENT] New user signed up: <email>`. Fires
  once per user, the first time they verify a magic link.
- **First-ever notification per user** — `[AISW EVENT] First-ever
  notification fired for <email>`. Useful early-stage signal that real users
  are getting real value.
- **Daily summary** — `[AISW DAILY] Daily summary — HEALTHY` (or
  `DEGRADED` / `UNHEALTHY`). Sent at `DAILY_SUMMARY_TIME_ET`. Includes
  pipeline runs, ingest/notify counts, SMTP failures, signups, error rollup
  grouped by logger, top 10 error messages, DB row counts, uptime, Python
  version, log retention setting, next scheduled runs.

### Health classification in the daily summary

- `UNHEALTHY` — zero successful pipelines in last 24h, **or** ≥5 SMTP
  failures.
- `DEGRADED` — any pipeline aborted, any stage failed, or any ERROR/CRITICAL
  log in last 24h.
- `HEALTHY` — none of the above.

### Where the recipient comes from

`ALERT_RECEIVER` if set, else `SMTP_USERNAME` (the same inbox you're sending
from). If both are blank, alerts are silently dropped — visible only in
stderr — so you don't accidentally start spamming yourself if you misconfig.

### Quick smoke test from a Railway shell

```bash
# Trigger an alert (it should land in ALERT_RECEIVER within a few seconds).
python -c "import logging; logging.getLogger('deploy.smoketest').error('test alert from deploy')"

# Fire the daily summary on demand.
python -c "from app.jobs.daily_summary import run_daily_summary; run_daily_summary()"

# Inspect recent logs.
python -c "
from sqlalchemy import select
from app.db.database import get_session_factory
from app.models import LogEntry
db = get_session_factory()()
for r in db.execute(select(LogEntry).order_by(LogEntry.created_at.desc()).limit(20)).scalars():
    print(r.created_at, r.level, r.logger, '—', r.message[:100])
"
```

### Tuning retention

`LOG_LIFETIME` parses these units:

| Suffix | Meaning | Example |
| --- | --- | --- |
| `s` | seconds | `30s` |
| `m` | minutes (lowercase!) | `60m` |
| `h` | hours | `24h` |
| `d` | days | `30d` |
| `w` | weeks | `2w` |
| `M` | month ≈ 30 days (uppercase) | `1M` |
| `y` | year ≈ 365 days | `1y` |

A bare integer is interpreted as days. The cleanup job runs at 03:15 ET; if
you change the lifetime, the next nightly run will drop everything beyond the
new window.

## Manually running a batch job

Railway → service → ⋯ menu → **Run command**:

```bash
# Main pipeline (run in this order; compute/notify rely on ingest output).
python -c "from app.jobs.ingest import run_ingest; run_ingest()"
python -c "from app.jobs.compute import run_compute; run_compute()"
python -c "from app.jobs.notify import run_notify; run_notify()"

# Or run all three through the orchestrator (same code path APScheduler uses):
python -c "from app.jobs.scheduler import run_full_pipeline; run_full_pipeline()"

# Observability jobs you may want to trigger on demand:
python -c "from app.jobs.daily_summary import run_daily_summary; run_daily_summary()"
python -c "from app.jobs.cleanup import run_cleanup; run_cleanup()"
python -c "from app.jobs.heartbeat import run_heartbeat; run_heartbeat()"
```

Useful for debugging an alert that didn't fire — run ingest+compute, then
check `trend_analyses` in the DB, then run notify with SMTP logging cranked
up.

## Local dev parity

If you want local Postgres instead of SQLite (recommended before shipping
schema changes), run:

```bash
docker run -d --name aisw-pg \
  -e POSTGRES_PASSWORD=dev -e POSTGRES_DB=aisw \
  -p 5432:5432 postgres:16
```

Then in `backend/.env`:

```
DATABASE_URL=postgresql://postgres:dev@localhost:5432/aisw
```

The driver-rewrite in `_build_engine()` handles the rest. Run `python -m
app.db.seed` once.

## Troubleshooting

### `ModuleNotFoundError: No module named 'psycopg2'` on Railway

SQLAlchemy resolves `postgresql://` to the psycopg2 driver by default. We ship
psycopg 3. The fix lives in `backend/app/db/database.py`: it rewrites the
scheme to `postgresql+psycopg://` before calling `create_engine`. If you see
this error after a deploy, confirm the rewrite block is still in
`_build_engine()` and that `psycopg[binary]` is in `requirements.txt`.

### `TypeError: descriptor '__getitem__' requires a 'typing.Union' object but received a 'tuple'`

SQLAlchemy < 2.0.50 is incompatible with Python 3.14's typing internals. Bump
`sqlalchemy` to `2.0.50` (or newer 2.0.x) in `requirements.txt`.

### `pydantic-core` fails to build / no wheels found

`pydantic-core < 2.35.0` has no cp314 wheels and falls back to a Rust build
that will likely fail. The pin `pydantic==2.12.0` in `requirements.txt` pulls
`pydantic-core==2.41.1` which ships cp314 wheels. Don't downgrade pydantic
below 2.12 on Python 3.14.

### `uvicorn` runs but imports fail with `ModuleNotFoundError: slowapi`

Your venv isn't active, or `uvicorn` is resolving to a globally-installed
copy. Diagnose:

```bash
source backend/.venv/bin/activate
hash -r
which uvicorn   # must be …/backend/.venv/bin/uvicorn
```

Bulletproof workaround that ignores `PATH`:

```bash
backend/.venv/bin/python -m uvicorn app.main:app --reload
```

### `fatal: Unable to create '.git/index.lock': File exists`

A previous git command crashed. Delete the lock:

```bash
rm -f .git/index.lock
```

Make sure no editor with a `git commit` template is still open before
retrying.

### Magic link email arrives but the link 404s

`FRONTEND_BASE_URL` on Railway doesn't match the Vercel domain (or has a
trailing slash, or wrong scheme). Update it, redeploy the backend, request a
new link — old ones are baked with the wrong URL.

### CORS errors in the browser console

`CORS_ORIGINS` doesn't include the exact origin the SPA is loaded from.
Origins must include the scheme (`https://`) and have no trailing slash.
Multiple origins are comma-separated. After updating, redeploy the backend.

### Magic link auth: "token expired" immediately

Server clock skew, or `MAGIC_LINK_TTL_MINUTES=0`. Railway's clock is fine;
check the env var.

### Emails never send and no error appears

If `SMTP_HOST` is empty or set to `localhost`, the app logs outgoing mail
instead of sending. That's deliberate dev behaviour — check the Railway logs
for the email body. To actually send, configure SMTP as above.

### Gmail returns `535-5.7.8 Username and Password not accepted`

Either the App Password is wrong, 2FA isn't fully on, or you used your
regular Google password. Regenerate the App Password and try the local
`smtplib` test above before redeploying.

### Backend cold-starts and misses a batch window

You probably let Railway scale the service to zero. The in-process scheduler
needs the dyno to be alive at trigger time. Disable autoscaling-to-zero on
the service, or move to a separate worker (Celery + Redis) — the README's
roadmap section covers this transition.

### Vercel build fails: `VITE_API_URL is not defined`

You added the env var after a build was queued. Trigger a fresh deploy
(Deployments → ⋯ → **Redeploy**). Vite bakes env vars in at build time.

### Admin alerts never arrive

Walk down the chain:

1. `ALERTS_ENABLED=true` — verify in Railway Variables.
2. Recipient resolves to something: `python -c "from app.core.settings import
   get_settings; print(repr(get_settings().effective_alert_receiver))"`.
   Blank ⇒ alerts are silently dropped.
3. SMTP works at all: re-run the local `smtplib` test from the
   [Gmail SMTP](#gmail-smtp-setup) section against the Railway env values.
4. Trigger one manually: `python -c "import logging;
   logging.getLogger('deploy').error('hi')"`. If this arrives, real errors
   will too.
5. Dedup hasn't suppressed it: dedup is keyed on `(logger, first 200 chars of
   message)` and lasts 15 minutes per process. A restart resets dedup. If you
   suspect dedup is silencing a real recurring issue, restart the service.

### Daily summary never arrives

Either `DAILY_SUMMARY_ENABLED=false`, `ALERTS_ENABLED=false`, the time is set
to the past for today (cron will fire it tomorrow), or the service was down
at the trigger moment (in-process scheduler, no catch-up). Run it manually to
confirm formatting: `python -c "from app.jobs.daily_summary import
run_daily_summary; run_daily_summary()"`.

### `log_entries` table growing without bound

Either `LOG_LIFETIME` is set too long, the cleanup job isn't running, or
log volume spiked. Check:

```bash
# How many rows? How old is the oldest?
python -c "
from sqlalchemy import func, select
from app.db.database import get_session_factory
from app.models import LogEntry
db = get_session_factory()()
print('rows:', db.execute(select(func.count(LogEntry.id))).scalar_one())
print('oldest:', db.execute(select(func.min(LogEntry.created_at))).scalar_one())
"

# Force a cleanup right now.
python -c "from app.jobs.cleanup import run_cleanup; print(run_cleanup(), 'deleted')"
```

If the cleanup deletes 0 rows when you expected many, your `LOG_LIFETIME`
string didn't parse — invalid specs log an error and skip cleanup. Valid
examples: `7d`, `24h`, `1M`, `90d`.

### Email storm / inbox flooded with `[AISW ALERT]`

The 15-minute dedup is per-process and per-fingerprint, so an error that
slightly mutates its message each time (e.g., includes a timestamp or
counter) won't dedup. Fix the log call site to use `%s`-style placeholders
instead of f-strings so the formatted template is stable:

```python
# Bad — message includes counter, defeats dedup
log.error(f"failed attempt #{n} for {user}")

# Good — template is stable across invocations
log.error("failed attempt #%d for %s", n, user)
```

As an emergency stop, set `ALERTS_ENABLED=false` and redeploy; logs still
persist to the DB so you don't lose visibility.

### `[AISW EVENT] New user signed up` for the same user twice

Shouldn't happen — the hook only fires when `verify_magic_link` *creates* a
new `users` row. If you see duplicates, the user row was deleted and
re-created (e.g., manual cleanup), or there's a race between two requests
verifying the same token (single-use enforcement should prevent this — check
the `login_tokens` table for the offending token's `consumed_at`).

## Rollback

Railway: Deployments tab → pick the previous green deploy → **Redeploy**.
Vercel: Deployments → previous deploy → ⋯ → **Promote to Production**.

Both are < 30 seconds. Do this first if a deploy breaks prod, then debug
locally against the rolled-back build.

## What's not in this guide (yet)

- Alembic migrations — the app currently uses `Base.metadata.create_all` at
  startup, which is fine for adding columns to empty tables but not for
  destructive changes. Add Alembic before the first schema migration in prod.
- External log aggregation — `log_entries` covers durability + query, but a
  hosted service (Logtail / Axiom / Datadog) gives full-text search, alerting
  rules beyond ERROR-level, and survives a Postgres outage. Wire one up once
  log volume exceeds a few hundred MB.
- Multi-replica alert dedup — the in-memory dedup is per-process. If you
  scale to >1 web replica, identical errors emit one alert per replica per
  window. Move dedup state to a tiny `alert_suppressions` table at that
  point.
- CAPTCHA on `/auth/request-link` — listed in the README as a pre-launch
  hardening item.
