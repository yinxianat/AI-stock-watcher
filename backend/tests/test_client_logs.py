"""Frontend errors post here; the server pipeline picks them up.

A POST to /client-logs re-emits each entry through the standard logger,
so `DBLogHandler` persists it to `log_entries` and `EmailAlertHandler`
emails the admin (deduped). These tests check the pipeline is wired up
right — not the handlers themselves, which have their own tests.
"""

from __future__ import annotations

import logging


def _records(caplog):
    return [r for r in caplog.records if r.name == "app.client"]


def test_error_from_client_is_logged_at_error(client, caplog):
    with caplog.at_level(logging.WARNING):
        r = client.post("/client-logs", json={
            "level": "error",
            "message": "feedback card threw",
            "stack": "at Feedback (/assets/index.js:1)",
            "url": "https://ai-stock-watcher.vercel.app/feedback?token=...",
            "source": "feedback",
        })
    assert r.status_code == 204
    recs = _records(caplog)
    assert len(recs) == 1
    assert recs[0].levelno == logging.ERROR
    assert "[client:feedback]" in recs[0].getMessage()
    assert "feedback card threw" in recs[0].getMessage()


def test_warning_from_client_is_logged_at_warning(client, caplog):
    with caplog.at_level(logging.WARNING):
        client.post("/client-logs", json={
            "level": "warning",
            "message": "retrying rate request",
            "source": "money-ideas",
        })
    recs = _records(caplog)
    assert len(recs) == 1
    assert recs[0].levelno == logging.WARNING


def test_stack_is_included_in_message(client, caplog):
    with caplog.at_level(logging.WARNING):
        client.post("/client-logs", json={
            "message": "crash", "stack": "line1\nline2", "source": "render",
        })
    msg = _records(caplog)[0].getMessage()
    assert "line1" in msg
    assert "line2" in msg


def test_oversize_message_is_rejected(client):
    r = client.post("/client-logs", json={
        "message": "x" * 10_000, "source": "render",
    })
    assert r.status_code == 422


def test_oversize_stack_is_rejected(client):
    r = client.post("/client-logs", json={
        "message": "ok", "stack": "y" * 50_000, "source": "render",
    })
    assert r.status_code == 422


def test_empty_message_is_rejected(client):
    assert client.post("/client-logs", json={"message": "", "source": "x"}).status_code == 422


def test_invalid_level_rejected(client):
    r = client.post("/client-logs", json={"level": "fatal", "message": "x"})
    assert r.status_code == 422


def test_no_auth_required(client, caplog):
    """Errors can happen on signed-out pages (login, feedback link), so the
    endpoint must accept anonymous posts."""
    with caplog.at_level(logging.ERROR):
        r = client.post("/client-logs", json={
            "message": "login page crashed", "source": "login",
        })
    assert r.status_code == 204
    assert len(_records(caplog)) == 1


def test_logs_go_to_app_client_logger(client, caplog):
    """Separate logger name so a reader can tell client errors apart from
    server errors in the DB and in Railway logs."""
    with caplog.at_level(logging.ERROR):
        client.post("/client-logs", json={"message": "x", "source": "y"})
    recs = _records(caplog)
    assert recs and recs[0].name == "app.client"
