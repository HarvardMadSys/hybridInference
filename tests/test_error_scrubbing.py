"""Unit tests for user-facing error scrubbing."""

import pytest

from serving.exceptions import (
    AuthenticationError,
    HybridInferenceError,
    QuotaExceededError,
    UserFacingError,
    scrub_error_for_user,
)

# Realistic provider error bodies (must NEVER appear in user-facing output).
PROVIDER_ERROR_SAMPLES = [
    "anthropic returned 500: internal_server_error",
    'OpenAI API error: {"error": {"message": "model overloaded"}}',
    "OpenRouter upstream timeout from https://openrouter.ai/api/v1",
    "claude-3-5-sonnet failed: token limit exceeded",
    "Connection refused to https://api.anthropic.com/v1/messages",
]

FORBIDDEN_SUBSTRINGS = ("anthropic", "openai", "openrouter", "claude", "https://", "api.")


@pytest.mark.parametrize("body", PROVIDER_ERROR_SAMPLES)
@pytest.mark.parametrize(
    "status_code,expected_prefix",
    [
        (401, "Authentication failed"),
        (403, "Authentication failed"),
        (429, "Rate limit exceeded"),
        (400, "Invalid request"),
        (422, "Invalid request"),
        (500, "Upstream service error"),
        (502, "Upstream service error"),
        (503, "Upstream service error"),
        (418, "Request failed"),
    ],
)
def test_scrub_replaces_message_by_status(body, status_code, expected_prefix):
    exc = RuntimeError(body)
    msg = scrub_error_for_user(exc, "req_abc", status_code)
    assert msg.startswith(expected_prefix), msg
    lowered = msg.lower()
    for forbidden in FORBIDDEN_SUBSTRINGS:
        assert forbidden not in lowered, f"forbidden token {forbidden!r} leaked in {msg!r}"


def test_scrub_appends_request_id_when_present():
    msg = scrub_error_for_user(RuntimeError("boom"), "req_xyz", 500)
    assert "(request_id: req_xyz)" in msg


@pytest.mark.parametrize("rid", ["", None])
def test_scrub_omits_request_id_when_blank(rid):
    msg = scrub_error_for_user(RuntimeError("boom"), rid, 500)
    assert "request_id" not in msg


def test_user_facing_error_passes_through():
    exc = QuotaExceededError(quota=1.0, spent=2.5)
    msg = scrub_error_for_user(exc, "req_q", 402)
    assert "Quota exceeded" in msg
    assert "(request_id: req_q)" in msg


def test_non_user_facing_subclass_is_scrubbed():
    class InternalUpstream(HybridInferenceError):
        pass

    msg = scrub_error_for_user(InternalUpstream("anthropic 500"), "req_i", 500)
    assert "anthropic" not in msg.lower()
    assert msg.startswith("Upstream service error")


def test_authentication_error_is_user_facing():
    # AuthenticationError must inherit UserFacingError so its messages reach the user.
    assert issubclass(AuthenticationError, UserFacingError)


def test_none_exception_still_scrubs():
    msg = scrub_error_for_user(None, "req_n", 500)
    assert msg.startswith("Upstream service error")
    assert "(request_id: req_n)" in msg


def test_provider_pin_error_is_scrubbed():
    """ProviderPinError carries a pinned provider name in its message; the
    user must see the generic 400 message, not the provider name."""
    from routing.routers import ProviderPinError

    exc = ProviderPinError("Pinned provider 'anthropic' not found for model claude-3-5-sonnet")
    msg = scrub_error_for_user(exc, "req_p", 400)
    assert msg.startswith("Invalid request")
    assert "anthropic" not in msg.lower()
    assert "claude" not in msg.lower()
    assert "(request_id: req_p)" in msg
