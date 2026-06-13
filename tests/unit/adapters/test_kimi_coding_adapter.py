"""Unit tests for the Kimi coding-plan adapter."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from serving.adapters.base import ModelConfig
from serving.adapters.kimi_coding import KimiCodingAdapter
from serving.servers.registry import _make_adapter


def _make_cfg(**overrides: Any) -> ModelConfig:
    base: dict[str, Any] = {
        "id": "kimi-k2.6",
        "name": "Kimi K2.6",
        "provider": "kimi_coding",
        "base_url": "https://api.moonshot.ai/v1",
        "api_key": "sk-kimi-test",
        "provider_model_id": "kimi-k2-0905-preview",
        "supports_tools": False,
        "supports_structured_output": False,
        "supported_params": ["temperature", "top_p", "max_tokens"],
    }
    base.update(overrides)
    return ModelConfig(**base)


def test_make_adapter_returns_kimi_coding_adapter() -> None:
    adapter = _make_adapter(
        "kimi_coding",
        {
            "id": "kimi-k2.6",
            "name": "Kimi K2.6",
            "provider": "kimi_coding",
            "base_url": "https://api.moonshot.ai/v1",
            "api_key": "sk-kimi-test",
            "provider_model_id": "kimi-k2-0905-preview",
        },
    )
    assert isinstance(adapter, KimiCodingAdapter)


def test_build_headers_sets_user_agent() -> None:
    adapter = KimiCodingAdapter(_make_cfg())
    headers = adapter._build_headers()
    assert headers["User-Agent"] == "claude-code/0.1.0"
    assert headers["Authorization"] == "Bearer sk-kimi-test"


def test_build_headers_user_agent_overridable_via_extra_headers() -> None:
    adapter = KimiCodingAdapter(_make_cfg(extra_headers={"User-Agent": "custom/9.9"}))
    headers = adapter._build_headers()
    assert headers["User-Agent"] == "custom/9.9"


def test_build_headers_user_agent_override_is_case_insensitive() -> None:
    # A lowercase override must win without adding a duplicate "User-Agent" key.
    adapter = KimiCodingAdapter(_make_cfg(extra_headers={"user-agent": "custom/9.9"}))
    headers = adapter._build_headers()
    assert headers["user-agent"] == "custom/9.9"
    assert "User-Agent" not in headers
    ua_keys = [k for k in headers if k.lower() == "user-agent"]
    assert len(ua_keys) == 1


def test_prepare_messages_prepends_opencode_system() -> None:
    adapter = KimiCodingAdapter(_make_cfg())
    out = adapter._prepare_messages([{"role": "user", "content": "hi"}])
    assert out[0] == {"role": "system", "content": "You are OpenCode"}
    assert out[1] == {"role": "user", "content": "hi"}


def test_prepare_messages_prepends_before_other_system_message() -> None:
    adapter = KimiCodingAdapter(_make_cfg())
    out = adapter._prepare_messages(
        [
            {"role": "system", "content": "You are a helpful assistant"},
            {"role": "user", "content": "hi"},
        ]
    )
    assert out[0] == {"role": "system", "content": "You are OpenCode"}
    assert out[1] == {"role": "system", "content": "You are a helpful assistant"}
    assert out[2] == {"role": "user", "content": "hi"}


def test_prepare_messages_no_duplicate_when_already_present() -> None:
    adapter = KimiCodingAdapter(_make_cfg())
    out = adapter._prepare_messages(
        [
            {"role": "system", "content": "You are OpenCode"},
            {"role": "user", "content": "hi"},
        ]
    )
    assert out == [
        {"role": "system", "content": "You are OpenCode"},
        {"role": "user", "content": "hi"},
    ]
    assert [m["content"] for m in out].count("You are OpenCode") == 1


def test_prepare_messages_empty_list() -> None:
    adapter = KimiCodingAdapter(_make_cfg())
    out = adapter._prepare_messages([])
    assert out == [{"role": "system", "content": "You are OpenCode"}]


@pytest.mark.asyncio
async def test_chat_completion_sends_user_agent_and_system_prompt() -> None:
    adapter = KimiCodingAdapter(_make_cfg())
    upstream_response = {
        "id": "x",
        "choices": [{"message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }

    captured: dict[str, Any] = {}

    async def fake_json_post_with_retry(*, url, json, headers, timeout, retries):
        captured["headers"] = headers
        captured["payload"] = json
        return upstream_response

    with patch.object(
        adapter.http, "json_post_with_retry", AsyncMock(side_effect=fake_json_post_with_retry)
    ):
        await adapter.chat_completion([{"role": "user", "content": "hi"}])

    assert captured["headers"]["User-Agent"] == "claude-code/0.1.0"
    assert captured["payload"]["messages"][0] == {
        "role": "system",
        "content": "You are OpenCode",
    }
