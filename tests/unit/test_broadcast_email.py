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


def test_render_markdown_email_converts_markdown_to_html():
    from serving.utils.email import render_markdown_email

    src = "## Hello\n\nThis is **bold** and a [link](https://example.com)."
    html, text = render_markdown_email(src)

    assert "<h2" in html
    assert "<strong>" in html
    assert "<a href" in html and "https://example.com" in html
    # Plaintext is the stripped markdown source.
    assert text == src.strip()


def test_render_markdown_email_includes_shell_and_footer():
    from serving.utils.email import render_markdown_email

    html, _ = render_markdown_email("hi")

    assert html.startswith("<html><body")
    assert "max-width: 600px; margin: 0 auto; padding: 20px;" in html
    assert "You received this because you have an active FreeInference account." in html
    assert html.rstrip().endswith("</body></html>")


def test_render_broadcast_template_custom_markdown_rendered():
    from serving.utils.email import render_broadcast_template

    result = render_broadcast_template(
        None,
        {},
        custom_subject="Subj",
        custom_body_markdown="## Heading\n\n**bold**",
    )
    assert result["subject"] == "Subj"
    # HTML is rendered markdown, not the raw source.
    assert "<h2" in result["body_html"]
    assert "<strong>" in result["body_html"]
    assert "## Heading" not in result["body_html"]
    # Plaintext is the stripped markdown source.
    assert result["body_text"] == "## Heading\n\n**bold**"


def test_render_broadcast_template_markdown_takes_precedence_over_html():
    from serving.utils.email import render_broadcast_template

    result = render_broadcast_template(
        None,
        {},
        custom_subject="Subj",
        custom_body_html="<p>raw html should be ignored</p>",
        custom_body_markdown="# Markdown wins",
    )
    assert "Markdown wins" in result["body_html"]
    assert "<h1" in result["body_html"]
    assert "raw html should be ignored" not in result["body_html"]


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
async def test_execute_broadcast_persists_exception_message():
    """When send_email raises, the exception class+message is stored in the
    recipient row, not a generic placeholder."""
    from serving.utils import email_scheduler

    conn = AsyncMock()
    conn.fetchrow.return_value = _broadcast_row("bc-exc")
    conn.fetch.return_value = [{"user_id": "u1", "email": "boom@example.com"}]
    conn.execute = AsyncMock()
    pool = _make_pool(conn)
    email_scheduler._db_pool = pool

    def _raises(*a, **kw):
        raise RuntimeError("smtp boom")

    with patch("serving.utils.email_scheduler.send_email", side_effect=_raises):
        await email_scheduler.execute_broadcast("bc-exc")

    failed_call = next(c for c in conn.execute.call_args_list if "status = 'failed'" in str(c))
    # Args include the per-user error list; the actual message should be in there.
    args_str = str(failed_call)
    assert "RuntimeError" in args_str and "smtp boom" in args_str


def test_broadcast_preview_request_rejects_empty_filters():
    """Empty target_roles or target_statuses must be rejected — ANY('{}') matches
    nothing in postgres, so silently sending zero emails would be confusing."""
    from pydantic import ValidationError

    from serving.schemas_admin import BroadcastPreviewRequest

    with pytest.raises(ValidationError):
        BroadcastPreviewRequest(target_roles=[], target_statuses=["active"])
    with pytest.raises(ValidationError):
        BroadcastPreviewRequest(target_roles=["free"], target_statuses=[])
    # Non-empty on both sides is accepted.
    BroadcastPreviewRequest(target_roles=["free"], target_statuses=["active"])


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
