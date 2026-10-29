"""Keep the admin informed about AI stories: keys, models in use, and cost.

- `check_keys` tests every key the setup needs with a free call (model
  info / a stock quote; no tokens or searches). A key that doesn't work is
  reported straight away through `service.report_key_problem`.
- `startup_check` runs when the app starts. Railway restarts the app
  whenever a variable changes, so if any AI setting or key changed since the
  last start, the admin gets a "setup" email: what's in use, key status,
  cost per story and the most it can cost per day. Keys are never shown,
  only a short fingerprint to tell whether one changed.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

import anthropic
import httpx
from sqlalchemy import select

from app.core.settings import Settings, get_settings
from app.db.database import get_session_factory
from app.models import JobRun
from app.services.alerts import AlertChannel, notify_admin
from app.services.articles import costs
from app.services.articles.pipeline import failed_service, key_problem_reason
from app.services.articles.providers import AIServiceError

log = logging.getLogger(__name__)

_JOB = "ai_config"
_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=10.0)
_NAMES = {"gemini": "Gemini", "anthropic": "Anthropic", "finnhub": "Finnhub"}
_KEY_VARS = {"gemini": "GEMINI_API_KEY", "anthropic": "ANTHROPIC_API_KEY", "finnhub": "FINNHUB_API_KEY"}


# ---------------------------------------------------------------------------
# Free key checks
# ---------------------------------------------------------------------------


def _check_gemini(key: str, model: str) -> None:
    r = httpx.get(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}",
        headers={"x-goog-api-key": key}, timeout=_TIMEOUT,
    )
    if r.status_code != 200:
        raise AIServiceError("Gemini", r.status_code, r.text[:200])


def _check_anthropic(key: str, model: str) -> None:
    anthropic.Anthropic(api_key=key, max_retries=0, timeout=10.0).models.retrieve(model)


def _check_finnhub(key: str) -> None:
    r = httpx.get("https://finnhub.io/api/v1/quote",
                  params={"symbol": "SPY", "token": key}, timeout=_TIMEOUT)
    if r.status_code != 200:
        raise AIServiceError("Finnhub", r.status_code, r.text[:200])


def services_in_use(settings: Settings) -> list[str]:
    used = list(settings.provider_chain)
    if any(settings.news_source_for(p) == "finnhub" for p in used):
        used.append("finnhub")
    return used


def check_keys(report: bool = True) -> dict[str, str]:
    """Status per service in use: "OK", "not set", or the problem. With
    `report`, problems are emailed to the admin immediately (first time)."""
    from app.services.articles import service

    settings = get_settings()
    statuses: dict[str, str] = {}
    for svc in services_in_use(settings):
        key = settings.finnhub_api_key.strip() if svc == "finnhub" else settings.key_for(svc)
        if not key:
            statuses[svc] = "not set"
            if report:
                service.report_key_problem(svc, f"{_KEY_VARS[svc]} is not set")
            continue
        try:
            if svc == "gemini":
                _check_gemini(key, settings.model_for("gemini"))
            elif svc == "anthropic":
                _check_anthropic(key, settings.model_for("anthropic"))
            else:
                _check_finnhub(key)
        except Exception as e:  # noqa: BLE001
            status = getattr(e, "status_code", None)
            if failed_service(e) is None or status is None:
                statuses[svc] = f"couldn't check ({type(e).__name__})"
                log.warning("Key check for %s couldn't connect: %s", svc, e)
                continue
            reason = key_problem_reason(e) or (
                f"the model {settings.model_for(svc)} wasn't found" if status == 404
                else f"{_NAMES[svc]} returned HTTP {status}"
            )
            statuses[svc] = f"PROBLEM: {reason}"
            if report:
                service.report_key_problem(svc, reason)
            continue
        statuses[svc] = "OK"
        if report:
            service._service_works(svc)
    return statuses


# ---------------------------------------------------------------------------
# What's in use and what it costs
# ---------------------------------------------------------------------------


def _fingerprint(key: str) -> str:
    key = key.strip()
    return hashlib.sha256(key.encode()).hexdigest()[:10] if key else ""


def snapshot(settings: Settings) -> dict[str, Any]:
    """Every setting that affects which AI runs or what it costs."""
    return {
        "AI_ARTICLES_ENABLED": settings.ai_articles_enabled,
        "AI_PROVIDER": settings.ai_provider,
        "AI_FALLBACK_PROVIDER": settings.ai_fallback_provider or "(none)",
        "main model": settings.model_for(settings.ai_provider),
        "backup model": settings.model_for(settings.ai_fallback_provider)
        if len(settings.provider_chain) > 1 else "(none)",
        "AI_NEWS_SOURCE": settings.ai_news_source,
        "AI_GEMINI_FREE_TIER": settings.ai_gemini_free_tier,
        "AI_ARTICLE_MAX_PER_DAY": settings.ai_article_max_per_day,
        "AI_ARTICLE_ALWAYS_PCT": settings.ai_article_always_pct,
        "AI_ARTICLE_MAX_SEARCHES": settings.ai_article_max_searches,
        "AI_ARTICLE_MAX_CONCURRENT": settings.ai_article_max_concurrent,
        "keys": {
            "GEMINI_API_KEY": _fingerprint(settings.key_for("gemini")),
            "ANTHROPIC_API_KEY": _fingerprint(settings.key_for("anthropic")),
            "FINNHUB_API_KEY": _fingerprint(settings.finnhub_api_key),
        },
    }


def changes(old: dict[str, Any] | None, new: dict[str, Any]) -> list[str]:
    if old is None:
        return ["first setup report"]
    out = [f"{k}: {old.get(k)} → {v}" for k, v in new.items() if k != "keys" and old.get(k) != v]
    old_keys = old.get("keys", {})
    for name, fp in new["keys"].items():
        before = old_keys.get(name, "")
        if before != fp:
            out.append(f"{name}: {'added' if not before else 'removed' if not fp else 'changed'}")
    return out


def setup_lines(settings: Settings, statuses: dict[str, str] | None = None) -> list[str]:
    statuses = statuses or {}
    if not settings.ai_articles_enabled:
        return ["AI stories are OFF (AI_ARTICLES_ENABLED=false). No AI cost."]

    def row(role: str, provider: str) -> str:
        model = settings.model_for(provider)
        price = costs.money(costs.story_estimate(provider, settings))
        key = statuses.get(provider, "not checked")
        return f"  {role:<8}{_NAMES[provider]} {model}: {price} per story; key {key}"

    lines = ["In use now", row("Main", settings.ai_provider)]
    if len(settings.provider_chain) > 1:
        lines.append(row("Backup", settings.ai_fallback_provider)
                     + " (only when the main one fails)")
    news = ("Finnhub (free)" if "finnhub" in services_in_use(settings)
            else f"Claude web search (${costs.WEB_SEARCH_USD:.2f}/search)")
    lines.append(f"  News    {news}"
                 + (f"; key {statuses.get('finnhub', 'not checked')}" if "finnhub" in services_in_use(settings) else ""))
    daily = costs.max_daily_cost(settings)
    monthly = None if daily is None else daily * costs.TRADING_DAYS_PER_MONTH
    lines += [
        "",
        "Limits and cost",
        f"  Stories per day:   up to {settings.ai_article_max_per_day}"
        + (f" ({costs.max_stories_per_day(settings)} with big moves ≥ {settings.ai_article_always_pct:g}%)"
           if settings.ai_article_always_pct > 0 else ""),
        f"  Most per day:      {costs.money(daily)} (every story on the priciest AI in use)",
        f"  Most per month:    {costs.money(monthly)} ({costs.TRADING_DAYS_PER_MONTH} trading days)",
    ]
    return lines


def _last_snapshot() -> dict[str, Any] | None:
    db = get_session_factory()()
    try:
        row = db.execute(
            select(JobRun).where(JobRun.job_name == _JOB).order_by(JobRun.id.desc()).limit(1)
        ).scalar_one_or_none()
        return json.loads(row.result_summary) if row and row.result_summary else None
    except Exception:  # noqa: BLE001
        return None
    finally:
        db.close()


def startup_check() -> None:
    """Check keys (problems are emailed at once) and, if any AI setting or
    key changed since the last start, email the admin the new setup."""
    from app.jobs.audit import record_job_run
    from app.models import utcnow

    try:
        settings = get_settings()
        statuses = check_keys(report=True) if settings.ai_articles_enabled else {}
        current = snapshot(settings)
        previous = _last_snapshot()
        if current == previous:
            return
        what_changed = changes(previous, current)
        daily = costs.max_daily_cost(settings) if settings.ai_articles_enabled else 0.0
        state = "ON" if settings.ai_articles_enabled else "OFF"
        notify_admin(
            "ai_setup",
            f"AI stories setup: {state}, {_NAMES[settings.ai_provider]} "
            f"{settings.model_for(settings.ai_provider)}, at most {costs.money(daily)}/day",
            "\n".join(
                ["What changed:"] + [f"  {c}" for c in what_changed] + [""]
                + setup_lines(settings, statuses)
                + ["", "Estimates are typical token use at published prices; your "
                   "provider's billing page is the final word."]
            ),
            channel=AlertChannel.EVENT,
        )
        record_job_run(_JOB, "SUCCESS", utcnow(), 0.0, result_summary=json.dumps(current))
    except Exception as e:  # noqa: BLE001 — never break startup
        log.warning("AI setup check failed: %s", e)
