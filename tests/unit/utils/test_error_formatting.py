"""Unit tests for operator-facing error text written to ``api_logs.error``."""

from __future__ import annotations

import pytest

from serving.utils.errors import categorize_exception, format_exception_for_db


@pytest.mark.unit
def test_exception_type_is_recorded():
    """An unattributable message is what made #1361 ungreppable.

    ``IndexError('list index out of range')`` used to record exactly that
    string: no type, no module, no frame. 7,291 rows of it, and searching
    production for the phrase found nothing that named the code at fault.
    """
    assert (
        format_exception_for_db(IndexError("list index out of range"))
        == "IndexError: list index out of range"
    )


@pytest.mark.unit
def test_message_less_exception_still_identifiable():
    """The pre-existing fallback for CancelledError/GeneratorExit is preserved."""
    assert format_exception_for_db(RuntimeError()) == "RuntimeError"


@pytest.mark.unit
def test_upstream_body_still_appended():
    exc = ValueError("bad gateway")
    exc.error_body = '{"detail":"upstream said no"}'
    out = format_exception_for_db(exc)
    assert out.startswith("ValueError: bad gateway")
    assert "upstream said no" in out


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "secret"),
    [
        ('api_key="sk-secret-value"', "sk-secret-value"),
        ("token: plain-value", "plain-value"),
        # A keyed Bearer credential used to survive redaction entirely: the
        # value pattern stops at whitespace, so it replaced only the word
        # "Bearer" and wrote the token itself to api_logs.error. _BEARER_RE
        # could not catch the remainder either, because the word it keys on
        # had just been substituted away.
        ("Authorization: Bearer abc123.def456", "abc123.def456"),
        ("authorization=Bearer sk-live-9999", "sk-live-9999"),
        # An unkeyed Bearer token stays _BEARER_RE's job.
        ("Bearer loose-token-here", "loose-token-here"),
    ],
)
def test_credentials_are_redacted(raw, secret):
    out = format_exception_for_db(ValueError(raw))
    assert "REDACTED" in out
    assert secret not in out


@pytest.mark.unit
def test_truncation_cap_respected():
    out = format_exception_for_db(ValueError("x" * 9000), max_len=100)
    assert len(out) == 100
    assert out.endswith("...[truncated]")


@pytest.mark.unit
def test_categorization_reads_the_type_as_before():
    """categorize_exception already prefixed the type itself; unchanged here."""
    assert categorize_exception(TimeoutError("nope")) == "timeout"
    assert categorize_exception(IndexError("list index out of range")) == "unknown"
