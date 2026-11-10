"""What happens when the mail server drops an email.

`send_email` used to log the SMTP error and return normally, which made
every caller's failure handling unreachable: the recap wrote its `RecapSend`
row, the notify job's `send_failures` counter stayed at zero, and a story
follow-up was marked "sent" and never retried — all for emails nobody
received. The database said the user had been emailed; only an ERROR log
said otherwise.

So these tests are about the claim a row makes. A send that fails must not
leave behind a row saying it succeeded, and the reason has to be recorded
where the dashboard reads it rather than only in the log.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.db.database import get_session_factory
from app.jobs import intraday as intraday_mod, notify as notify_mod, recap as recap_mod
from app.jobs.intraday import run_intraday_capture
from app.jobs.notify import run_notify
from app.jobs.recap import run_watchlist_recap
from app.models import (
    ArticleFollowup,
    DeliveryOutcome,
    NotificationLog,
    RecapSend,
    StockArticle,
    utcnow,
)
from app.services import delivery, emailer
from app.services.articles import service

from tests.test_articles import _Later, _fetch, _rows, stories_on  # noqa: F401
from tests.test_intraday import _seed_prior_day_close, _setup_user_with_range_rule
from tests.test_recap import _closes, _market, _user  # noqa: F401


def _boom(*a, **k):
    """A mail server that takes the message and then drops the connection —
    the real production failure this came from (Gmail never returned its 250
    after the DATA of an HTML story email)."""
    raise emailer.EmailSendError(
        "SMTPServerDisconnected: Connection unexpectedly closed: "
        "The read operation timed out"
    )


def _outcomes(reason: str) -> list[DeliveryOutcome]:
    db = get_session_factory()()
    rows = db.execute(
        select(DeliveryOutcome).where(DeliveryOutcome.reason == reason)
    ).scalars().all()
    db.close()
    return rows


# ---------------------------------------------------------------------------
# The emailer itself
# ---------------------------------------------------------------------------


def test_send_email_raises_so_a_caller_can_tell(monkeypatch):
    """The whole point. A swallowed failure is indistinguishable from a
    delivered email at the call site, which is how a row ends up lying."""
    import smtplib

    class Dropped:
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
            raise smtplib.SMTPServerDisconnected("read operation timed out")

    monkeypatch.setattr(emailer.smtplib, "SMTP", Dropped)
    monkeypatch.setenv("SMTP_HOST", "smtp.gmail.com")
    monkeypatch.setenv("SMTP_USERNAME", "ops@example.com")
    from app.core.settings import get_settings
    get_settings.cache_clear()

    with pytest.raises(emailer.EmailSendError) as e:
        emailer.send_email("a@example.com", "subject", "body")
    assert "SMTPServerDisconnected" in str(e.value)

    # And the quiet wrapper, for callers with nothing to record.
    assert emailer.send_email_quiet("a@example.com", "subject", "body") is False


def test_a_missing_smtp_server_is_not_a_send_failure(monkeypatch):
    """The development default has no mail server at all. That path logs and
    returns — turning it into an error would make every local run noisy and
    every test a failure case."""
    monkeypatch.setenv("SMTP_HOST", "localhost")
    monkeypatch.setenv("SMTP_USERNAME", "")
    from app.core.settings import get_settings
    get_settings.cache_clear()

    emailer.send_email("a@example.com", "subject", "body")  # does not raise


def test_a_broken_mail_server_does_not_break_signing_in(client, monkeypatch):
    """The sign-in path is deliberately quiet: the request must not 500, and
    the user just asks for another link."""
    monkeypatch.setattr(emailer, "send_email", _boom)
    r = client.post("/auth/request-link", json={"email": "someone@example.com"})
    assert r.status_code < 400


# ---------------------------------------------------------------------------
# Alerts: no NotificationLog row for an email that never arrived
# ---------------------------------------------------------------------------


def test_a_failed_alert_writes_no_notification_log(client, monkeypatch):
    """`notification_logs` is the durable record and the dedup key. A row for
    an undelivered alert both lies and stops the next run retrying it."""
    _setup_user_with_range_rule("AAPL", -5.0, 5.0)
    monkeypatch.setattr(notify_mod, "send_email", _boom)

    run_notify()

    assert _rows(NotificationLog) == []


def test_a_failed_intraday_alert_is_recorded_as_send_failed(client, monkeypatch):
    _, ticker_id = _setup_user_with_range_rule("AAPL", -5.0, 5.0)
    _seed_prior_day_close(ticker_id, close=100.0)
    monkeypatch.setattr(intraday_mod, "send_email", _boom)

    run_intraday_capture(fetcher=_fetch({"AAPL": 106.4}), force=True)

    assert _rows(NotificationLog) == []
    rows = _outcomes("send_failed")
    assert len(rows) == 1
    assert "SMTPServerDisconnected" in rows[0].detail
    assert delivery.REASONS["send_failed"].ok is False


# ---------------------------------------------------------------------------
# Recap: no RecapSend row for a recap that never arrived
# ---------------------------------------------------------------------------


def test_a_failed_recap_writes_no_recap_send(client, monkeypatch):
    """`RecapSend` is what stops a second recap the same day. Written after a
    failed send it suppresses the retry as well as the duplicate."""
    _user("parent@example.com", ["AAPL"])
    _closes("AAPL", today=104.0)
    monkeypatch.setattr(recap_mod, "send_email", _boom)

    run_watchlist_recap()

    assert _rows(RecapSend) == []
    rows = _outcomes("send_failed")
    assert len(rows) == 1
    assert "SMTPServerDisconnected" in rows[0].detail


# ---------------------------------------------------------------------------
# Story follow-ups: the promise is kept or recorded as broken, never faked
# ---------------------------------------------------------------------------


def test_a_failed_followup_goes_back_in_the_queue_and_is_retried(
    client, stories_on, monkeypatch  # noqa: F811
):
    """The follow-up is claimed before the send, so a dropped email left the
    row reading "sent" with nobody waiting on it. It goes back to `waiting`
    and the next sweep delivers it."""
    later = _Later()
    monkeypatch.setattr(service, "_executor", later)
    _, ticker_id = _setup_user_with_range_rule("AAPL", -5.0, 5.0)
    _seed_prior_day_close(ticker_id, close=100.0)
    run_intraday_capture(fetcher=_fetch({"AAPL": 106.4}), force=True)
    alert_count = len(client.sent_emails)

    capture = service.send_email  # the conftest capture, put back below
    monkeypatch.setattr(service, "send_email", _boom)
    later.run_all()  # the story finishes, and the follow-up send fails

    assert [f.status for f in _rows(ArticleFollowup)] == ["waiting"]
    assert len(client.sent_emails) == alert_count
    rows = _outcomes("followup_send_failed")
    assert len(rows) == 1
    assert "SMTPServerDisconnected" in rows[0].detail
    assert "next sweep" in rows[0].detail

    # SMTP comes back: the sweep keeps the promise.
    monkeypatch.setattr(service, "send_email", capture)
    db = get_session_factory()()
    service.sweep(db)
    db.close()

    assert [f.status for f in _rows(ArticleFollowup)] == ["sent"]
    assert len(client.sent_emails) == alert_count + 1
    assert client.sent_emails[-1]["subject"].startswith("Re: ")
    assert len(_outcomes("followup_sent")) == 1


def test_a_followup_too_old_to_send_is_abandoned_not_retried(
    client, stories_on, monkeypatch  # noqa: F811
):
    """A reply arriving the next day would describe the wrong day's move, so
    a stale follow-up is a promise we stop trying to keep — and say so."""
    later = _Later()
    monkeypatch.setattr(service, "_executor", later)
    _, ticker_id = _setup_user_with_range_rule("AAPL", -5.0, 5.0)
    _seed_prior_day_close(ticker_id, close=100.0)
    run_intraday_capture(fetcher=_fetch({"AAPL": 106.4}), force=True)

    db = get_session_factory()()
    article = db.execute(select(StockArticle)).scalar_one()
    article.trading_date = service.trading_date() - timedelta(days=1)
    db.commit()
    db.close()

    capture = service.send_email
    monkeypatch.setattr(service, "send_email", _boom)
    later.run_all()

    assert [f.status for f in _rows(ArticleFollowup)] == ["abandoned"]
    rows = _outcomes("followup_send_failed")
    assert len(rows) == 1
    assert "abandoned" in rows[0].detail

    # And it is not picked up again.
    monkeypatch.setattr(service, "send_email", capture)
    before = len(client.sent_emails)
    db = get_session_factory()()
    service.sweep(db)
    db.close()
    assert len(client.sent_emails) == before


def test_the_followup_failure_reason_is_in_the_catalogue():
    """A reason code the dashboard cannot explain is a row that says nothing
    — the failure this whole feature exists to prevent."""
    r = delivery.REASONS["followup_send_failed"]
    assert r.ok is False and r.causes and r.fix
    assert "retried" in r.what
