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
    operator_safe_error,
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


# ----------------------------------------------------------------------
# operator_safe_error (operator-facing surfaces, e.g. Slack alerts)
# ----------------------------------------------------------------------


def test_operator_safe_error_strips_api_key_from_client_response_error():
    """A Gemini-style URL with ?key=<api_key> must never reach an alert.

    aiohttp.ClientResponseError embeds the request URL in str(exc); Gemini
    builds URLs as ".../generateContent?key=<api_key>", so an unscrubbed
    str(exc) would leak the provider key.
    """
    exc = _make_client_response_error(
        503,
        message="Service Unavailable",
        url="https://generativelanguage.googleapis.com/v1beta/models/gemini:generateContent?key=AIzaSecretKey123",
    )
    detail = operator_safe_error(exc)
    assert detail is not None
    assert "AIzaSecretKey123" not in detail
    assert "key=" not in detail
    assert "https://" not in detail
    assert "googleapis" not in detail.lower()


def test_operator_safe_error_keeps_useful_message():
    exc = _make_client_response_error(429, message="rate limit exceeded for project")
    detail = operator_safe_error(exc)
    assert detail is not None
    assert "rate limit exceeded" in detail


def test_operator_safe_error_handles_plain_exception():
    detail = operator_safe_error(ValueError("connection reset by peer"))
    assert detail == "connection reset by peer"


def test_operator_safe_error_scrubs_secrets_in_plain_exception():
    detail = operator_safe_error(RuntimeError("auth failed: api_key=sk-supersecret"))
    assert detail is not None
    assert "sk-supersecret" not in detail
    assert "[REDACTED]" in detail


@pytest.mark.parametrize(
    "body,secret",
    [
        (
            "Incorrect API key provided: sk-proj-ABC123DEF456GHI789. "
            "You can find your API key at https://platform.openai.com/account/api-keys.",
            "sk-proj-ABC123DEF456GHI789",
        ),
        ("API key: sk-live-SECRETVALUE123", "sk-live-SECRETVALUE123"),
        ("your api key sk-abc12345 is wrong", "sk-abc12345"),
        ('{"error": {"message": "Incorrect API key provided: sk-1234567890"}}', "sk-1234567890"),
        ("auth failed for key AIzaSyD1234567890abcdefghij", "AIzaSyD1234567890abcdefghij"),
        ("groq rejected gsk_abcdef1234567890XYZ", "gsk_abcdef1234567890XYZ"),
    ],
)
def test_operator_safe_error_redacts_spaced_and_prefixed_keys(body, secret):
    """Upstream 401 bodies echo the key without an api_key= separator.

    OpenAI-family providers return "Incorrect API key provided: sk-..." which
    the assignment-based secret regex misses; the value-based token regex must
    redact it before it reaches a Slack alert.
    """
    detail = operator_safe_error(RuntimeError(body))
    assert detail is not None
    assert secret not in detail
    assert "[REDACTED]" in detail


def test_scrub_provider_identity_redacts_bare_key_token():
    assert "sk-abcdef123456" not in scrub_provider_identity("token sk-abcdef123456 invalid")


def test_operator_safe_error_none_for_no_exception():
    assert operator_safe_error(None) is None


def test_operator_safe_error_truncates():
    detail = operator_safe_error(RuntimeError("x" * 1000), max_len=50)
    assert detail is not None
    assert len(detail) <= 50
    assert detail.endswith("…")


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
        503,
        message="Service Unavailable",
        url="https://api.deepseek.com/v1/chat/completions",
        error_body='{"error": {"message": "Model is overloaded", "type": "server_error"}}',
    )
    msg = scrub_error_for_user(exc, "req_u", 503)
    assert "Model is overloaded" in msg
    assert "(request_id: req_u)" in msg
    # Provider identity must not leak.
    for forbidden in FORBIDDEN_SUBSTRINGS:
        assert forbidden not in msg.lower(), msg


# ----------------------------------------------------------------------
# Upstream quota/balance suppression (never expose that OUR account is out
# of quota/funds — fall back to the generic status-based message).
# ----------------------------------------------------------------------

# Realistic upstream quota/balance/billing bodies that must NOT reach the user.
UPSTREAM_QUOTA_SAMPLES = [
    '{"error": {"message": "Insufficient Balance", "type": "quota"}}',
    '{"error": {"message": "You exceeded your current quota, please check your '
    'plan and billing details.", "type": "insufficient_quota"}}',
    '{"error": {"message": "This request would exceed your monthly spending '
    'limit.", "type": "billing"}}',
    '{"error": {"message": "Your credit balance is too low to access this model."}}',
    '{"error": {"message": "Insufficient funds, please recharge your account."}}',
]


@pytest.mark.parametrize("body", UPSTREAM_QUOTA_SAMPLES)
def test_upstream_quota_message_is_suppressed(body):
    """Upstream quota/balance errors fall back to the generic status message."""
    exc = _make_client_response_error(
        402,
        message="Payment Required",
        url="https://api.deepseek.com/v1/chat/completions",
        error_body=body,
    )
    msg = scrub_error_for_user(exc, "req_q1", 402)
    # 402 has no dedicated generic message → falls back to "Request failed".
    assert msg.startswith("Request failed"), msg
    assert "(request_id: req_q1)" in msg
    lowered = msg.lower()
    for token in ("quota", "balance", "billing", "credit", "funds", "recharge"):
        assert token not in lowered, f"quota token {token!r} leaked in {msg!r}"


def test_upstream_quota_on_429_falls_back_to_rate_limit():
    """A 429 quota body surfaces the generic rate-limit message, not the quota."""
    exc = _make_client_response_error(
        429,
        message="Too Many Requests",
        error_body='{"error": {"message": "You exceeded your current quota."}}',
    )
    msg = scrub_error_for_user(exc, "req_q2", 429)
    assert msg.startswith("Rate limit exceeded"), msg
    assert "quota" not in msg.lower()


def test_user_quota_error_still_surfaces_own_message():
    """Our own QuotaExceededError is user-facing and must still be shown."""
    msg = scrub_error_for_user(QuotaExceededError(quota=5.0, spent=6.0), "req_q3", 402)
    assert "Quota exceeded" in msg
    assert "(request_id: req_q3)" in msg


def test_user_safe_upstream_error_suppresses_quota_text():
    assert user_safe_upstream_error("Insufficient Balance") is None
    assert user_safe_upstream_error("You exceeded your current quota") is None
    # Non-quota upstream messages still pass through.
    assert user_safe_upstream_error("Model is overloaded") == "Model is overloaded"


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
