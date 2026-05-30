"""FastAPI application entry point.

Run locally:    uvicorn app.main:app --reload
Health check:   GET /healthz
OpenAPI docs:   GET /docs
"""

from __future__ import annotations

import asyncio
import logging
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
from app.db.database import get_engine
from app.models import Base


logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    await asyncio.to_thread(Base.metadata.create_all, bind=get_engine())
    log.info("DB schema ensured (env=%s)", settings.app_env)

    scheduler = None
    if settings.batch_jobs_enabled and settings.app_env != "test":
        # Import here so tests don't accidentally start a scheduler thread.
        from app.jobs.scheduler import start_scheduler

        scheduler = start_scheduler()
        log.info("Batch scheduler started.")
    app.state.scheduler = scheduler

    try:
        yield
    finally:
        if scheduler is not None:
            scheduler.shutdown(wait=False)
            log.info("Batch scheduler stopped.")


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
