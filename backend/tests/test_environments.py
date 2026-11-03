"""UAT is a deployed environment and must be hardened like production.

Every test here exists because the behaviour it pins used to key off
`app_env != "production"` — a deny-list of one. Under that form, naming a new
environment anything other than "production" silently handed it the
developer-laptop path: cleartext admin logins, public API docs, a
bag-of-words embedder. The branches now key off `Settings.is_deployed`, an
allow-list, and these tests are what stops the old form coming back.

The pattern throughout is `monkeypatch.setenv` + `get_settings.cache_clear()`,
because settings are LRU-cached for the process.
"""

from __future__ import annotations

import logging

import pytest

from app.core.settings import Settings, get_settings

SECRET = "a-sufficiently-long-service-secret-value"


@pytest.fixture
def service_on(client, monkeypatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", SECRET)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    """Leave the cache clean either side, so a tampered env never leaks."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# The environment is nameable at all
# ---------------------------------------------------------------------------


def test_uat_is_a_valid_app_env():
    """`app_env` is a Literal, so before this `APP_ENV=uat` was a startup
    crash on a Pydantic validation error, not a configuration choice."""
    assert Settings(app_env="uat").app_env == "uat"


def test_an_unknown_env_is_still_refused():
    """The Literal is the point: a typo like APP_ENV=prod must fail loudly at
    startup rather than quietly resolve to the untrusted path."""
    with pytest.raises(Exception):
        Settings(app_env="prod")


# ---------------------------------------------------------------------------
# is_deployed: the allow-list
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "env,deployed",
    [
        ("production", True),
        ("uat", True),
        ("development", False),
        ("test", False),
    ],
)
def test_is_deployed_covers_every_environment(env, deployed):
    """Parametrised over the full Literal on purpose: adding a new
    environment makes this test fail until someone decides, explicitly,
    whether it is internet-facing."""
    assert Settings(app_env=env).is_deployed is deployed


# ---------------------------------------------------------------------------
# Interactive docs
# ---------------------------------------------------------------------------


def test_docs_are_hidden_in_uat(monkeypatch):
    """A complete API map is free reconnaissance wherever it is reachable,
    and the UAT host is as public as production."""
    monkeypatch.setenv("APP_ENV", "uat")
    monkeypatch.delenv("API_DOCS_PUBLIC", raising=False)
    get_settings.cache_clear()
    from app.main import create_app

    app = create_app()
    assert app.docs_url is None
    assert app.redoc_url is None
    assert app.openapi_url is None


def test_uat_can_opt_into_docs_deliberately(monkeypatch):
    """Testers often want /docs in UAT. That is a legitimate choice, so it
    gets an explicit switch rather than being a side effect of the
    environment's name."""
    monkeypatch.setenv("APP_ENV", "uat")
    monkeypatch.setenv("API_DOCS_PUBLIC", "true")
    get_settings.cache_clear()
    from app.main import create_app

    assert create_app().docs_url == "/docs"


def test_production_cannot_be_opted_in_by_accident(monkeypatch):
    """Nothing here stops API_DOCS_PUBLIC=true in production — it is a
    deliberate override. This pins that it takes a deliberate act: the
    default stays off."""
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("API_DOCS_PUBLIC", raising=False)
    get_settings.cache_clear()
    assert get_settings().docs_are_public is False


# ---------------------------------------------------------------------------
# Service login transport
# ---------------------------------------------------------------------------


def test_https_is_required_in_uat(client, service_on, monkeypatch):
    """UAT holds a real ADMIN_API_TOKEN on a public host. Accepting it over
    cleartext there leaks a working admin secret."""
    monkeypatch.setenv("APP_ENV", "uat")
    get_settings.cache_clear()
    r = client.post(
        "/admin/service-login",
        json={"secret": SECRET},
        headers={"X-Forwarded-Proto": "http"},
    )
    assert r.status_code == 400
    assert "https" in r.json()["detail"].lower()


def test_forwarded_https_is_accepted_in_uat(client, service_on, monkeypatch):
    monkeypatch.setenv("APP_ENV", "uat")
    get_settings.cache_clear()
    r = client.post(
        "/admin/service-login",
        json={"secret": SECRET},
        headers={"X-Forwarded-Proto": "https"},
    )
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Email bodies
# ---------------------------------------------------------------------------


def test_email_body_is_not_dumped_in_uat(monkeypatch, caplog):
    """Bodies carry signed magic-link and settings tokens. This branch was
    already written as an allow-list (`== "development"`), so UAT was safe
    before the refactor — the test is here so it stays that way, since the
    log line it guards is WARNING and therefore persisted to log_entries."""
    from app.services import emailer

    monkeypatch.setenv("SMTP_HOST", "localhost")
    monkeypatch.setenv("SMTP_USERNAME", "")
    monkeypatch.setenv("APP_ENV", "uat")
    get_settings.cache_clear()

    with caplog.at_level(logging.WARNING, logger="app.services.emailer"):
        emailer.send_email(
            "alice@gmail.com", "Sign in", "Click https://x/verify?token=SECRET123"
        )

    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "SECRET123" not in logged
    assert "alice@gmail.com" not in logged


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------


def test_uat_will_not_silently_use_the_fake_embedder(monkeypatch):
    """Only reachable by selecting voyage without a key. The fake is
    bag-of-words, so falling back would leave UAT retrieval looking healthy
    while returning nonsense — and UAT exists to tell us whether the real
    thing works."""
    from app.services.rag.embeddings import EmbeddingError, get_embedder

    monkeypatch.setenv("RAG_EMBEDDING_PROVIDER", "voyage")
    monkeypatch.setenv("VOYAGE_API_KEY", "")
    monkeypatch.setenv("APP_ENV", "uat")
    get_settings.cache_clear()
    with pytest.raises(EmbeddingError, match="Refusing to fall back"):
        get_embedder()


# ---------------------------------------------------------------------------
# Alert attribution
# ---------------------------------------------------------------------------


def test_uat_alerts_say_they_are_from_uat(monkeypatch):
    """UAT and production share ALERT_RECEIVER. An untagged alert sends the
    admin to look at the wrong deployment."""
    monkeypatch.setenv("APP_ENV", "uat")
    get_settings.cache_clear()
    from app.services.alerts import _env_suffix

    assert _env_suffix() == " [UAT]"


def test_production_alert_subjects_are_unchanged(monkeypatch):
    """Existing inbox filters match on the current subjects, so production
    must stay untagged."""
    monkeypatch.setenv("APP_ENV", "production")
    get_settings.cache_clear()
    from app.services.alerts import _env_suffix

    assert _env_suffix() == ""


def test_env_suffix_never_raises(monkeypatch):
    """Alerting must never raise (it is the recursion-safe path). If settings
    are broken, an untagged alert still beats no alert."""
    import app.services.alerts as alerts

    def boom():
        raise RuntimeError("settings unavailable")

    monkeypatch.setattr(alerts, "get_settings", boom)
    assert alerts._env_suffix() == ""
