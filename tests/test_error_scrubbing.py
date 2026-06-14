"""Unit tests for user-facing error scrubbing."""

import aiohttp
import pytest
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL

from serving.exceptions import (
    AuthenticationError,
    HybridInferenceError,
    QuotaExceededError,
    UserFacingError,
    scrub_error_for_user,
    scrub_provider_identity,
    user_safe_upstream_error,
)


def _make_client_response_error(
    status: int,
    *,
    message: str = "Service Unavailable",
    url: str = "https://api.deepseek.com/v1/chat/completions",
    error_body: str | None = None,
) -> aiohttp.ClientResponseError:
    """Build a realistic aiohttp.ClientResponseError for tests."""
    yarl_url = URL(url)
    req_info = aiohttp.RequestInfo(yarl_url, "POST", CIMultiDictProxy(CIMultiDict()), yarl_url)
    exc = aiohttp.ClientResponseError(
        request_info=req_info,
        history=(),
        status=status,
        message=message,
    )
    if error_body is not None:
        exc.error_body = error_body  # type: ignore[attr-defined]
    return exc


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
        (500, "Internal server error"),
        (502, "Internal server error"),
        (503, "Internal server error"),
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
    assert msg.startswith("Internal server error")


def test_authentication_error_is_user_facing():
    # AuthenticationError must inherit UserFacingError so its messages reach the user.
    assert issubclass(AuthenticationError, UserFacingError)


def test_none_exception_still_scrubs():
    msg = scrub_error_for_user(None, "req_n", 500)
    assert msg.startswith("Internal server error")
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


# ----------------------------------------------------------------------
# Upstream message surfacing (hide provider identity, keep the message)
# ----------------------------------------------------------------------


def test_upstream_json_body_message_is_surfaced():
    """A genuine upstream error surfaces the provider's human-readable message."""
    exc = _make_client_response_error(
        402,
        message="Payment Required",
        url="https://api.deepseek.com/v1/chat/completions",
        error_body='{"error": {"message": "Insufficient Balance", "type": "quota"}}',
    )
    msg = scrub_error_for_user(exc, "req_u", 402)
    assert "Insufficient Balance" in msg
    assert "(request_id: req_u)" in msg
    # Provider identity must not leak.
    for forbidden in FORBIDDEN_SUBSTRINGS:
        assert forbidden not in msg.lower(), msg


def test_upstream_error_without_body_drops_url():
    """A ClientResponseError with no body surfaces its message but never the URL."""
    exc = _make_client_response_error(
        503,
        message="Service Unavailable",
        url="https://api.anthropic.com/v1/messages",
    )
    msg = scrub_error_for_user(exc, "req_v", 503)
    assert msg.startswith("Service Unavailable")
    assert "http" not in msg.lower()
    assert "anthropic" not in msg.lower()
    assert "api." not in msg.lower()


def test_internal_exception_stays_generic():
    """Non-upstream exceptions (no error_body, not ClientResponseError) stay generic."""
    msg = scrub_error_for_user(RuntimeError("boom in our own code"), "req_w", 500)
    assert msg.startswith("Internal server error")
    assert "boom" not in msg.lower()


def test_streaming_db_format_is_unwrapped():
    """The persisted streaming format ('... | upstream_body=<json>') is unwrapped."""
    raw = (
        "503, message='Service Unavailable', "
        "url='https://api.deepseek.com/v1/chat/completions' "
        '| upstream_body={"error": {"message": "Model is overloaded"}}'
    )
    out = user_safe_upstream_error(raw)
    assert out == "Model is overloaded"


def test_claude_wrapper_is_unwrapped():
    out = user_safe_upstream_error("Upstream API error: rate limit reached for this key")
    assert out == "rate limit reached for this key"


@pytest.mark.parametrize(
    "raw",
    [
        "Connection refused to https://api.anthropic.com/v1/messages",
        "openrouter upstream timeout from api.openrouter.ai",
        'OpenAI error: {"detail": "model overloaded"}',
        "503, message='busy', url='https://api.deepseek.com/v1'",
    ],
)
def test_scrub_provider_identity_removes_all_identity_tokens(raw):
    out = scrub_provider_identity(raw)
    lowered = out.lower()
    for forbidden in FORBIDDEN_SUBSTRINGS:
        assert forbidden not in lowered, f"{forbidden!r} leaked in {out!r}"


def test_user_safe_upstream_error_returns_none_for_blank():
    assert user_safe_upstream_error(None) is None
    assert user_safe_upstream_error("") is None


def test_user_safe_upstream_error_truncates_long_messages():
    out = user_safe_upstream_error("x" * 5000)
    assert out is not None
    assert len(out) <= 500
