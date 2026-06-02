"""Pytest fixtures.

Each test gets an isolated in-memory SQLite DB and a TestClient wired to it.
Emails are captured in a list (`sent_emails`) instead of being sent.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("APP_DEBUG", "true")
os.environ.setdefault("SECRET_KEY", "test-secret-key-must-be-long-enough")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("BATCH_JOBS_ENABLED", "false")
os.environ.setdefault("ALERTS_ENABLED", "false")
os.environ.setdefault("LOG_DB_PERSISTENCE", "false")
os.environ.setdefault("DAILY_SUMMARY_ENABLED", "false")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.core.settings import get_settings  # noqa: E402
from app.db import database as db_mod  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import Base  # noqa: E402
from app.services import emailer  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_settings():
    """Reset cached settings between tests (in case env was mutated)."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def engine():
    """A fresh shared in-memory SQLite for the test.

    StaticPool keeps a single connection so multiple sessions in the same
    test see the same data — required for in-memory SQLite.
    """
    from sqlalchemy.pool import StaticPool

    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=eng)
    yield eng
    eng.dispose()


@pytest.fixture
def db_session(engine):
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client(engine, monkeypatch) -> Iterator[TestClient]:
    """TestClient with DB swapped to the per-test engine and email captured."""
    # Patch the global engine accessor used by FastAPI dependencies.
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    monkeypatch.setattr(db_mod, "_engine", engine, raising=False)
    monkeypatch.setattr(db_mod, "_SessionLocal", SessionLocal, raising=False)
    monkeypatch.setattr(db_mod, "get_engine", lambda: engine)
    monkeypatch.setattr(db_mod, "get_session_factory", lambda: SessionLocal)

    # Capture outgoing emails.
    captured: list[dict] = []

    def _capture(to, subject, body_text, body_html=None):
        captured.append(
            {"to": to, "subject": subject, "body_text": body_text, "body_html": body_html}
        )

    monkeypatch.setattr(emailer, "send_email", _capture)
    # Also patch references in services that imported it directly at top level
    from app.services import auth as auth_mod, confirm_email as ce_mod
    from app.jobs import intraday as intraday_mod, notify as notify_mod

    monkeypatch.setattr(auth_mod, "send_email", _capture)
    monkeypatch.setattr(ce_mod, "send_email", _capture)
    monkeypatch.setattr(notify_mod, "send_email", _capture)
    monkeypatch.setattr(intraday_mod, "send_email", _capture)

    app = create_app()
    with TestClient(app) as tc:
        tc.sent_emails = captured  # type: ignore[attr-defined]
        yield tc


@pytest.fixture
def signed_in(client):
    """Helper: returns (client, session_token, user_dict) for a logged-in user."""
    client.post("/auth/request-link", json={"email": "alice@example.com"})
    # Extract token from the captured email body.
    body = client.sent_emails[-1]["body_text"]
    token = body.split("token=")[1].split("\n")[0].strip()
    r = client.post("/auth/verify", json={"token": token})
    assert r.status_code == 200, r.text
    data = r.json()
    return {
        "client": client,
        "token": data["session_token"],
        "user": data["user"],
        "auth": {"Authorization": f"Bearer {data['session_token']}"},
    }
