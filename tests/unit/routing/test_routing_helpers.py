"""Direct contracts for public routing telemetry and streaming helpers."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from routing.streaming import has_non_empty_content
from routing.telemetry import failed_attempt, routing_chunk


def _adapter(
    *,
    provider: object = "openai",
    base_url: str = "https://api.example.com/v1",
    endpoint_id: object | None = "openai:api.example.com:443",
) -> SimpleNamespace:
    return SimpleNamespace(
        config=SimpleNamespace(
            provider=provider,
            base_url=base_url,
            endpoint_id=endpoint_id,
        )
    )


def test_failed_attempt_uses_canonical_endpoint_identity():
    adapter = _adapter(endpoint_id=None)

    assert failed_attempt(adapter, ValueError("bad request")) == {
        "provider": "openai",
        "endpoint_id": "openai",
        "error_type": "ValueError",
        "error": "bad request",
    }


def test_failed_attempt_uses_unknown_backup_sentinel_without_adapter():
    assert failed_attempt(None, TimeoutError("late")) == {
        "provider": "unknown-backup",
        "endpoint_id": "unknown-backup",
        "error_type": "TimeoutError",
        "error": "late",
    }


def test_failed_attempt_preserves_hedge_string_coercion():
    adapter = _adapter(provider=123, endpoint_id=456)

    attempt = failed_attempt(adapter, RuntimeError("failed"))

    assert attempt["provider"] == "123"
    assert attempt["endpoint_id"] == "456"


def test_routing_chunk_preserves_framing_fields_and_order():
    chunk = routing_chunk(_adapter(), failed_attempts=[])

    assert chunk == (
        'data: {"choices": [], "_routing": {"provider": "openai", '
        '"base_url": "https://api.example.com/v1", '
        '"endpoint_id": "openai:api.example.com:443"}}\n\n'
    )
    payload = json.loads(chunk.removeprefix("data: ").strip())
    assert list(payload) == ["choices", "_routing"]
    assert list(payload["_routing"]) == ["provider", "base_url", "endpoint_id"]


def test_routing_chunk_preserves_raw_nullable_endpoint_and_optional_field_order():
    attempts = [
        {
            "provider": "primary",
            "endpoint_id": "primary:host:443",
            "error_type": "RuntimeError",
            "error": "failed",
        }
    ]

    chunk = routing_chunk(
        _adapter(provider="backup", endpoint_id=None),
        fallback=True,
        failed_attempts=attempts,
    )
    payload = json.loads(chunk.removeprefix("data: ").strip())
    routing = payload["_routing"]

    assert chunk.startswith("data: ")
    assert chunk.endswith("\n\n")
    assert list(routing) == [
        "provider",
        "base_url",
        "endpoint_id",
        "fallback",
        "failed_attempts",
    ]
    assert routing["endpoint_id"] is None
    assert routing["fallback"] is True
    assert routing["failed_attempts"] == attempts


@pytest.mark.parametrize(
    "chunk",
    [
        'data: {"choices": []}\n\n',
        'data: {"choices": [{"delta": {}}]}\n\n',
        'data: {"choices": [{"delta": {"content": ""}}]}\n\n',
        'data: {"choices": [{"delta": {"tool_calls": []}}]}\n\n',
        "data: [DONE]\n\n",
    ],
)
def test_has_non_empty_content_rejects_protocol_only_chunks(chunk):
    assert has_non_empty_content(chunk) is False


@pytest.mark.parametrize(
    "delta",
    [
        {"content": "hello"},
        {"reasoning_content": "thinking"},
        {"reasoning": "thinking"},
        {"thinking": "thinking"},
        {"tool_calls": [{"id": "call-1"}]},
    ],
)
def test_has_non_empty_content_accepts_supported_delta_fields(delta):
    chunk = f"data: {json.dumps({'choices': [{'delta': delta}]})}\n\n"

    assert has_non_empty_content(chunk) is True


@pytest.mark.parametrize(
    "chunk",
    [
        object(),
        "not-sse",
        "data: {malformed-json}\n\n",
        b"data: \xff\n\n",
    ],
)
def test_has_non_empty_content_is_conservative_for_unknown_or_invalid_chunks(chunk):
    assert has_non_empty_content(chunk) is True


def test_has_non_empty_content_accepts_bytes():
    chunk = b'data: {"choices": [{"delta": {"content": "hello"}}]}\n\n'

    assert has_non_empty_content(chunk) is True
