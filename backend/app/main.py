"""FastAPI application entry point.

Run locally:    uvicorn app.main:app --reload
Health check:   GET /healthz
OpenAPI docs:   GET /docs
"""

from __future__ import annotations

import asyncio
import logging
import os
import time as _time_mod
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from app.api import (
    auth_routes,
    rules_routes,
    tickers_routes,
    trends_routes,
    watchlist_routes,
)
from app.core.settings import get_settings
from app.db.database import get_engine, get_session_factory
from app.db.seed import seed
from app.models import Base
from app.services.alerts import install_handlers

# Force the process timezone to US/Pacific BEFORE any logging happens.
# This affects time.localtime(), logging.Formatter.formatTime(), and
# everything else that reads the C-level timezone. Works even when the
# TZ env var is ignored by the container runtime.
os.environ["TZ"] = "US/Pacific"
_time_mod.tzset()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("app")
# Attach DB + email alert handlers to root logger. Safe & idempotent.
install_handlers()


def _log_persistence_snapshot() -> None:
    """Emit a one-line summary of user-owned row counts after startup.

    Read-only. Wrapped in try/except — telemetry failure must never block
    the app from coming up.
    """
    try:
        from sqlalchemy import func, select

        from app.models import (
            DailyClose,
            IntradayPrice,
            JobRun,
            NotificationLog,
            NotificationRule,
            User,
            WatchlistItem,
        )

        db = get_session_factory()()
        try:
            counts = {
                "users": db.execute(select(func.count(User.id))).scalar_one(),
                "watchlist_items": db.execute(select(func.count(WatchlistItem.id))).scalar_one(),
                "notification_rules": db.execute(
                    select(func.count(NotificationRule.id))
                ).scalar_one(),
                "notification_logs": db.execute(
                    select(func.count(NotificationLog.id))
                ).scalar_one(),
                "daily_closes": db.execute(select(func.count(DailyClose.id))).scalar_one(),
                "intraday_prices": db.execute(
                    select(func.count(IntradayPrice.id))
                ).scalar_one(),
                "job_runs": db.execute(
                    select(func.count(JobRun.id))
                ).scalar_one(),
            }
        finally:
            db.close()
        log.info("Persistence snapshot after startup: %s", counts)
    except Exception:
        log.exception("Could not log persistence snapshot (non-fatal)")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()

    try:
        # NOTE: do NOT drop_all here — it wipes users/rules/notification_logs
        # on every restart. `create_all` is idempotent and only adds missing
        # tables, which is what we want. Schema migrations should be explicit.
        await asyncio.to_thread(Base.metadata.create_all, bind=get_engine())
        log.info("DB schema ensured (env=%s)", settings.app_env)
        n = await asyncio.to_thread(seed)
        log.info("DB seeded: %d new tickers inserted.", n)

        # Persistence guardrail: log how much user-owned data we found AFTER
        # schema+seed ran. If a future change ever wipes user data on redeploy,
        # this line in the production deploy log makes it visible immediately.
        await asyncio.to_thread(_log_persistence_snapshot)
    except Exception:
        log.exception("STARTUP FAILED: error during DB schema initialisation")
        raise

    # ---- Config dump: makes misconfigurations visible in deploy logs ----
    log.info(
        "Batch config: BATCH_JOBS_ENABLED=%s, BATCH_JOB_TIMES_ET=%s, "
        "INTRADAY_INGEST_ENABLED=%s, INTRADAY_TICK_MINUTES=%s, "
        "STOCK_DATA_PROVIDER=%s, APP_ENV=%s",
        settings.batch_jobs_enabled,
        settings.batch_job_times_et,
        settings.intraday_ingest_enabled,
        settings.intraday_tick_minutes,
        settings.stock_data_provider,
        settings.app_env,
    )

    scheduler = None
    if settings.app_env != "test":
        if not settings.batch_jobs_enabled:
            log.warning(
                "BATCH_JOBS_ENABLED=false — scheduler is OFF. "
                "No batch pipeline, intraday capture, heartbeat, or cleanup "
                "jobs will run. Set BATCH_JOBS_ENABLED=true to enable."
            )
        else:
            # Import here so tests don't accidentally start a scheduler thread.
            try:
                from app.jobs.scheduler import start_scheduler

                scheduler = start_scheduler()
                log.info("Batch scheduler started.")
            except Exception:
                log.exception("STARTUP FAILED: error starting batch scheduler")
                raise
    app.state.scheduler = scheduler

    try:
        yield
    except Exception:
        log.exception("LIFESPAN ERROR: unhandled exception during application runtime")
        raise
    finally:
        if scheduler is not None:
            try:
                scheduler.shutdown(wait=False)
                log.info("Batch scheduler stopped.")
            except Exception:
                log.exception("SHUTDOWN ERROR: error stopping batch scheduler")


limiter = Limiter(key_func=get_remote_address, default_limits=["60/minute"])


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="AI Stock Watcher API",
        version="0.1.0",
        debug=settings.app_debug,
        lifespan=lifespan,
    )
    # Rate limiting
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=False,  # tokens travel in Authorization header, not cookies
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/healthz", tags=["meta"])
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(auth_routes.router)
    app.include_router(tickers_routes.router)
    app.include_router(watchlist_routes.router)
    app.include_router(rules_routes.router)
    app.include_router(trends_routes.router)
    return app


app = create_app()
