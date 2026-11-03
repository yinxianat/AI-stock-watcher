"""Logs must not accumulate personal data.

`info_logs` and `log_entries` are read by the admin, mailed out in alerts
and (for two endpoints) served to the monitoring agent. None of those need
a real email address or a full IP, so neither is ever written. The admin's
own alert *emails* still carry real addresses — only the log copy is masked.
"""

from __future__ import annotations

import logging

import pytest

from app.core.security import mask_email, redact_emails


# ---------------------------------------------------------------------------
# Masking primitives
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw,masked", [
    ("alice@gmail.com", "a***@gmail.com"),
    ("bob.smith+tag@example.co.uk", "b***@example.co.uk"),
    ("x@y.io", "x***@y.io"),
])
def test_mask_email_keeps_only_first_char_and_domain(raw, masked):
    assert mask_email(raw) == masked


@pytest.mark.parametrize("bad", [None, "", "   ", "not-an-address", "@nolocal.com"])
def test_mask_email_reveals_nothing_for_junk(bad):
    assert mask_email(bad) in ("***", "(none)")


def test_redact_emails_masks_addresses_inside_free_text():
    """The real leak: an alert subject that embeds an address."""
    out = redact_emails("New user signed up: alice@gmail.com")
    assert "alice@gmail.com" not in out
    assert "a***@gmail.com" in out


def test_redact_emails_handles_several_in_one_string():
    out = redact_emails("from a@b.com to carol@d.org failed")
    assert "a@b.com" not in out and "carol@d.org" not in out
    assert out.count("***@") == 2


def test_redact_emails_leaves_ordinary_text_alone():
    assert redact_emails("Pipeline SUCCESS (12.4s)") == "Pipeline SUCCESS (12.4s)"


def test_redact_emails_tolerates_none():
    assert redact_emails(None) == ""


# ---------------------------------------------------------------------------
# The send path — the single biggest PII source before this change
# ---------------------------------------------------------------------------


def test_send_email_never_logs_the_recipient(monkeypatch, caplog):
    """emailer logged the raw recipient on EVERY send, which made info_logs
    the largest store of user emails in the system."""
    from app.services import emailer

    monkeypatch.setattr(emailer.smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_USERNAME", "ops@example.com")
    from app.core.settings import get_settings
    get_settings.cache_clear()

    with caplog.at_level(logging.INFO, logger="app.services.emailer"):
        emailer.send_email("alice@gmail.com", "Your AAPL alert", "body")

    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "alice@gmail.com" not in logged
    assert "a***@gmail.com" in logged


def test_send_email_never_logs_an_address_hidden_in_the_subject(monkeypatch, caplog):
    """Masking the recipient alone wasn't enough — admin alert subjects
    embed the new user's address."""
    from app.services import emailer

    monkeypatch.setattr(emailer.smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_USERNAME", "ops@example.com")
    from app.core.settings import get_settings
    get_settings.cache_clear()

    with caplog.at_level(logging.INFO, logger="app.services.emailer"):
        emailer.send_email("ops@example.com", "New user signed up: victim@gmail.com", "b")

    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "victim@gmail.com" not in logged


def test_send_failure_does_not_log_the_recipient(monkeypatch, caplog):
    from app.services import emailer

    class Boom:
        def __init__(self, *a, **k):
            raise OSError("connection refused")

    monkeypatch.setattr(emailer.smtplib, "SMTP", Boom)
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_USERNAME", "ops@example.com")
    from app.core.settings import get_settings
    get_settings.cache_clear()

    with caplog.at_level(logging.ERROR, logger="app.services.emailer"):
        emailer.send_email("alice@gmail.com", "subject", "body")

    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "alice@gmail.com" not in logged


def test_body_is_not_logged_outside_development(monkeypatch, caplog):
    """Email bodies carry signed magic-link and settings tokens, so the
    dev-fallback dump must not run in production."""
    from app.services import emailer

    monkeypatch.setenv("SMTP_HOST", "localhost")
    monkeypatch.setenv("SMTP_USERNAME", "")
    monkeypatch.setenv("APP_ENV", "production")
    from app.core.settings import get_settings
    get_settings.cache_clear()

    with caplog.at_level(logging.WARNING, logger="app.services.emailer"):
        emailer.send_email("alice@gmail.com", "Sign in", "Click https://x/verify?token=SECRET123")

    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "SECRET123" not in logged
    assert "alice@gmail.com" not in logged


# ---------------------------------------------------------------------------
# Client error ingest
# ---------------------------------------------------------------------------


def test_client_log_truncates_ip_to_a_network(client, caplog):
    with caplog.at_level(logging.ERROR):
        client.post("/client-logs", json={"message": "boom", "source": "feedback"})
    rec = next(r for r in caplog.records if r.name == "app.client")
    net = rec.context["client_net"]
    assert net.endswith("/24") or net.endswith("/48") or net == "unknown"


def test_client_log_strips_the_query_string(client, caplog):
    """Our own email links put signed tokens in ?token=, and this URL is
    persisted to log_entries."""
    with caplog.at_level(logging.ERROR):
        client.post("/client-logs", json={
            "message": "boom", "source": "settings",
            "url": "https://app.example.com/settings?token=SIGNEDSECRET",
        })
    rec = next(r for r in caplog.records if r.name == "app.client")
    assert "SIGNEDSECRET" not in rec.context["client_url"]
    assert rec.context["client_url"] == "https://app.example.com/settings"


def test_client_log_redacts_an_email_in_the_message(client, caplog):
    with caplog.at_level(logging.ERROR):
        client.post("/client-logs", json={
            "message": "failed to load prefs for alice@gmail.com", "source": "settings",
        })
    rec = next(r for r in caplog.records if r.name == "app.client")
    assert "alice@gmail.com" not in rec.getMessage()


class _FakeSMTP:
    """Stand-in SMTP that accepts everything and sends nothing."""

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self):
        pass

    def login(self, *a):
        pass

    def send_message(self, msg):
        pass
