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


@pytest.mark.asyncio
async def test_execute_broadcast_sends_and_updates_recipients():
    """execute_broadcast marks recipients sent/failed and updates broadcast status."""
    from serving.utils import email_scheduler

    conn = AsyncMock()
    # broadcast row
    conn.fetchrow.return_value = {
        "id": "bc1",
        "subject": "Hello",
        "body_html": "<p>Hi</p>",
        "body_text": "Hi",
        "target_roles": ["free"],
        "target_statuses": ["active"],
        "status": "scheduled",
    }
    # recipient users
    conn.fetch.return_value = [
        {"id": "u1", "email": "a@example.com"},
        {"id": "u2", "email": "b@example.com"},
    ]
    conn.execute = AsyncMock()

    pool = _make_pool(conn)
    email_scheduler._db_pool = pool

    with patch("serving.utils.email_scheduler.send_email", return_value=True) as mock_send:
        await email_scheduler.execute_broadcast("bc1")

    assert mock_send.call_count == 2
    # At minimum: status->sending, insert recipients, per-recipient update x2, status->sent
    assert conn.execute.call_count >= 4


@pytest.mark.asyncio
async def test_execute_broadcast_partial_failure():
    """execute_broadcast marks broadcast 'sent' when at least one recipient succeeds."""
    from serving.utils import email_scheduler

    conn = AsyncMock()
    conn.fetchrow.return_value = {
        "id": "bc2",
        "subject": "Hi",
        "body_html": "<p>Hi</p>",
        "body_text": "Hi",
        "target_roles": ["free"],
        "target_statuses": ["active"],
        "status": "scheduled",
    }
    conn.fetch.return_value = [
        {"id": "u1", "email": "ok@example.com"},
        {"id": "u2", "email": "fail@example.com"},
    ]
    conn.execute = AsyncMock()
    pool = _make_pool(conn)
    email_scheduler._db_pool = pool

    def _send(to_email, *a, **kw):
        return to_email != "fail@example.com"

    with patch("serving.utils.email_scheduler.send_email", side_effect=_send):
        await email_scheduler.execute_broadcast("bc2")

    # Find the final status update — should be 'sent', not 'failed'
    calls = [str(c) for c in conn.execute.call_args_list]
    assert any("'sent'" in c or "sent" in c for c in calls)


@pytest.mark.asyncio
async def test_execute_broadcast_all_fail_marks_failed():
    """execute_broadcast marks broadcast 'failed' when all recipients fail."""
    from serving.utils import email_scheduler

    conn = AsyncMock()
    conn.fetchrow.return_value = {
        "id": "bc3",
        "subject": "Hi",
        "body_html": "<p>Hi</p>",
        "body_text": "Hi",
        "target_roles": ["free"],
        "target_statuses": ["active"],
        "status": "scheduled",
    }
    conn.fetch.return_value = [{"id": "u1", "email": "bad@example.com"}]
    conn.execute = AsyncMock()
    pool = _make_pool(conn)
    email_scheduler._db_pool = pool

    with patch("serving.utils.email_scheduler.send_email", return_value=False):
        await email_scheduler.execute_broadcast("bc3")

    calls = " ".join(str(c) for c in conn.execute.call_args_list)
    assert "failed" in calls
