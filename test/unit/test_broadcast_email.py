"""Unit tests for broadcast email feature."""

from __future__ import annotations

import pytest


def test_email_templates_keys_exist():
    from serving.utils.email import EMAIL_TEMPLATES

    assert "maintenance" in EMAIL_TEMPLATES
    assert "announcement" in EMAIL_TEMPLATES
    assert "quota_change" in EMAIL_TEMPLATES


def test_render_broadcast_template_substitutes_vars():
    from serving.utils.email import render_broadcast_template

    result = render_broadcast_template("maintenance", {"date": "May 1", "duration": "2 hours"})
    assert "May 1" in result["subject"] or "May 1" in result["body_html"]
    assert "2 hours" in result["body_html"] or "2 hours" in result["body_text"]


def test_render_broadcast_template_missing_var_leaves_placeholder():
    from serving.utils.email import render_broadcast_template

    result = render_broadcast_template("maintenance", {})
    # Missing vars should appear as {var_name} — no KeyError raised
    assert "{date}" in result["body_html"] or "{duration}" in result["body_html"]


def test_render_broadcast_template_custom_passthrough():
    from serving.utils.email import render_broadcast_template

    result = render_broadcast_template(
        None,
        {},
        custom_subject="Hello",
        custom_body_html="<p>World</p>",
        custom_body_text="World",
    )
    assert result["subject"] == "Hello"
    assert result["body_html"] == "<p>World</p>"


def test_render_broadcast_template_unknown_key_raises():
    from serving.utils.email import render_broadcast_template

    with pytest.raises(ValueError, match="Unknown template"):
        render_broadcast_template("nonexistent", {})


# ── Scheduler / execute_broadcast tests ───────────────────────────────────

from unittest.mock import AsyncMock, MagicMock, patch


class _AcquireCtx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *_):
        pass


def _make_pool(conn):
    pool = MagicMock()
    pool.acquire.side_effect = lambda: _AcquireCtx(conn)
    return pool


def _broadcast_row(bid: str = "bc1") -> dict:
    return {
        "id": bid,
        "subject": "Hello",
        "body_html": "<p>Hi</p>",
        "body_text": "Hi",
    }


@pytest.mark.asyncio
async def test_execute_broadcast_sends_and_updates_recipients():
    """execute_broadcast claims atomically, sends, and updates recipient statuses."""
    from serving.utils import email_scheduler

    conn = AsyncMock()
    conn.fetchrow.return_value = _broadcast_row("bc1")
    conn.fetch.return_value = [
        {"user_id": "u1", "email": "a@example.com"},
        {"user_id": "u2", "email": "b@example.com"},
    ]
    conn.execute = AsyncMock()

    pool = _make_pool(conn)
    email_scheduler._db_pool = pool

    with patch("serving.utils.email_scheduler.send_email", return_value=True) as mock_send:
        await email_scheduler.execute_broadcast("bc1")

    assert mock_send.call_count == 2
    # Atomic claim returns row; fetch returns recipients; one batch update + final status.
    calls = " ".join(str(c) for c in conn.execute.call_args_list)
    assert "sent" in calls


@pytest.mark.asyncio
async def test_execute_broadcast_skips_when_not_scheduled():
    """If atomic claim returns no row (already claimed), execute_broadcast is a no-op."""
    from serving.utils import email_scheduler

    conn = AsyncMock()
    conn.fetchrow.return_value = None  # atomic claim found no 'scheduled' row
    conn.execute = AsyncMock()
    pool = _make_pool(conn)
    email_scheduler._db_pool = pool

    with patch("serving.utils.email_scheduler.send_email", return_value=True) as mock_send:
        await email_scheduler.execute_broadcast("bc-already-claimed")

    assert mock_send.call_count == 0
    # No recipient fetch, no status updates after the claim attempt.
    conn.fetch.assert_not_called()


@pytest.mark.asyncio
async def test_execute_broadcast_partial_failure():
    """execute_broadcast marks broadcast 'sent' when at least one recipient succeeds."""
    from serving.utils import email_scheduler

    conn = AsyncMock()
    conn.fetchrow.return_value = _broadcast_row("bc2")
    conn.fetch.return_value = [
        {"user_id": "u1", "email": "ok@example.com"},
        {"user_id": "u2", "email": "fail@example.com"},
    ]
    conn.execute = AsyncMock()
    pool = _make_pool(conn)
    email_scheduler._db_pool = pool

    def _send(to_email, *a, **kw):
        return to_email != "fail@example.com"

    with patch("serving.utils.email_scheduler.send_email", side_effect=_send):
        await email_scheduler.execute_broadcast("bc2")

    calls = [str(c) for c in conn.execute.call_args_list]
    assert any("'sent'" in c or "sent" in c for c in calls)


@pytest.mark.asyncio
async def test_execute_broadcast_all_fail_marks_failed():
    """execute_broadcast marks broadcast 'failed' when all recipients fail."""
    from serving.utils import email_scheduler

    conn = AsyncMock()
    conn.fetchrow.return_value = _broadcast_row("bc3")
    conn.fetch.return_value = [{"user_id": "u1", "email": "bad@example.com"}]
    conn.execute = AsyncMock()
    pool = _make_pool(conn)
    email_scheduler._db_pool = pool

    with patch("serving.utils.email_scheduler.send_email", return_value=False):
        await email_scheduler.execute_broadcast("bc3")

    calls = " ".join(str(c) for c in conn.execute.call_args_list)
    assert "failed" in calls


@pytest.mark.asyncio
async def test_execute_broadcast_offloads_smtp_to_thread():
    """SMTP send_email is dispatched via asyncio.to_thread so it doesn't block the loop."""
    import asyncio

    from serving.utils import email_scheduler

    conn = AsyncMock()
    conn.fetchrow.return_value = _broadcast_row("bc4")
    conn.fetch.return_value = [{"user_id": "u1", "email": "a@example.com"}]
    conn.execute = AsyncMock()
    pool = _make_pool(conn)
    email_scheduler._db_pool = pool

    real_to_thread = asyncio.to_thread
    with (
        patch("serving.utils.email_scheduler.send_email", return_value=True),
        patch(
            "serving.utils.email_scheduler.asyncio.to_thread",
            side_effect=real_to_thread,
        ) as mock_to_thread,
    ):
        await email_scheduler.execute_broadcast("bc4")

    assert mock_to_thread.called, "send_email must be wrapped in asyncio.to_thread"
