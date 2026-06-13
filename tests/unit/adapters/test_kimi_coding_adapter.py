"""Unit tests for the Kimi coding-plan adapter."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

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


def test_prepare_messages_prepends_when_first_has_extra_keys() -> None:
    # A near-match carrying extra keys is not an exact match, so we prepend a
    # clean OpenCode system message to keep the leading message exact.
    adapter = KimiCodingAdapter(_make_cfg())
    out = adapter._prepare_messages(
        [
            {"role": "system", "content": "You are OpenCode", "name": "tool"},
            {"role": "user", "content": "hi"},
        ]
    )
    assert out[0] == {"role": "system", "content": "You are OpenCode"}
    assert out[1] == {"role": "system", "content": "You are OpenCode", "name": "tool"}


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


def _patch_identity_setting(value: bool):
    """Patch the runtime-settings singleton so the toggle resolves to ``value``.

    The async entrypoints resolve the toggle once via ``get_bool`` and snapshot
    it for the sync hooks.
    """
    rs = MagicMock()
    rs.get_bool = AsyncMock(return_value=value)
    return patch(
        "serving.config.runtime_settings.get_runtime_settings_instance",
        return_value=rs,
    )


async def _run_chat_capture(adapter: KimiCodingAdapter) -> dict[str, Any]:
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
    return captured


@pytest.mark.asyncio
async def test_identity_toggle_enabled_applies_injection() -> None:
    adapter = KimiCodingAdapter(_make_cfg())
    with _patch_identity_setting(True):
        captured = await _run_chat_capture(adapter)
    assert captured["headers"]["User-Agent"] == "claude-code/0.1.0"
    assert captured["payload"]["messages"][0] == {"role": "system", "content": "You are OpenCode"}


@pytest.mark.asyncio
async def test_identity_toggle_disabled_skips_injection() -> None:
    adapter = KimiCodingAdapter(_make_cfg())
    with _patch_identity_setting(False):
        captured = await _run_chat_capture(adapter)
    assert "User-Agent" not in captured["headers"]
    assert captured["payload"]["messages"][0] == {"role": "user", "content": "hi"}


@pytest.mark.asyncio
async def test_identity_disabled_forwards_client_user_agent() -> None:
    # When the toggle is off, forward the caller's own User-Agent (captured into
    # the request context) instead of the coding-tool identity, and skip the
    # OpenCode system message.
    from serving.utils import context as req_ctx

    adapter = KimiCodingAdapter(_make_cfg())
    with req_ctx.push(client_user_agent="my-client/2.0"), _patch_identity_setting(False):
        captured = await _run_chat_capture(adapter)
    assert captured["headers"]["User-Agent"] == "my-client/2.0"
    assert captured["payload"]["messages"][0] == {"role": "user", "content": "hi"}


@pytest.mark.asyncio
async def test_identity_enabled_ignores_client_user_agent() -> None:
    # When on, the coding-tool identity is used even if the caller sent its own.
    from serving.utils import context as req_ctx

    adapter = KimiCodingAdapter(_make_cfg())
    with req_ctx.push(client_user_agent="my-client/2.0"), _patch_identity_setting(True):
        captured = await _run_chat_capture(adapter)
    assert captured["headers"]["User-Agent"] == "claude-code/0.1.0"


@pytest.mark.asyncio
async def test_identity_disabled_without_client_ua_sets_no_user_agent() -> None:
    adapter = KimiCodingAdapter(_make_cfg())
    with _patch_identity_setting(False):
        captured = await _run_chat_capture(adapter)
    assert "User-Agent" not in captured["headers"]


@pytest.mark.asyncio
async def test_embeddings_respect_disabled_toggle() -> None:
    # Inherited request paths (embeddings) must also honour the toggle.
    adapter = KimiCodingAdapter(_make_cfg(model_type="embedding"))
    captured: dict[str, Any] = {}

    async def fake_json_post_with_retry(*, url, json, headers, timeout, retries):
        captured["headers"] = headers
        return {"data": [{"embedding": [0.1]}], "usage": {"prompt_tokens": 1, "total_tokens": 1}}

    with (
        _patch_identity_setting(False),
        patch.object(
            adapter.http, "json_post_with_retry", AsyncMock(side_effect=fake_json_post_with_retry)
        ),
    ):
        await adapter.embeddings("hello")
    assert "User-Agent" not in captured["headers"]


@pytest.mark.asyncio
async def test_resolve_identity_defaults_true_without_singleton() -> None:
    adapter = KimiCodingAdapter(_make_cfg())
    with patch(
        "serving.config.runtime_settings.get_runtime_settings_instance",
        side_effect=RuntimeError("not initialized"),
    ):
        assert await adapter._resolve_identity() is True


@pytest.mark.asyncio
async def test_resolve_identity_defaults_true_on_store_error() -> None:
    # An unexpected error (e.g. DB outage) when reading the setting must not
    # break inference — fall back to the default-on behaviour.
    adapter = KimiCodingAdapter(_make_cfg())
    rs = MagicMock()
    rs.get_bool = AsyncMock(side_effect=ConnectionError("db down"))
    with patch(
        "serving.config.runtime_settings.get_runtime_settings_instance",
        return_value=rs,
    ):
        assert await adapter._resolve_identity() is True


@pytest.mark.asyncio
async def test_identity_snapshot_consistent_within_request() -> None:
    # The toggle is resolved exactly once per request and both hooks read the
    # same snapshot — a mid-request setting flip cannot produce a torn read
    # (e.g. User-Agent without the OpenCode system message).
    adapter = KimiCodingAdapter(_make_cfg())
    kimi_reads = {"count": 0}

    def fake_get_bool(key):
        if key == "kimi_coding_identity_enabled":
            kimi_reads["count"] += 1
            # True on the first (only) resolve; a re-read would flip to False.
            return kimi_reads["count"] == 1
        return False  # e.g. log_full_payload

    rs = MagicMock()
    rs.get_bool = AsyncMock(side_effect=fake_get_bool)
    with _patch_runtime(rs):
        captured = await _run_chat_capture(adapter)
    # Snapshot resolved exactly once, and both hooks applied consistently.
    assert kimi_reads["count"] == 1
    assert captured["headers"]["User-Agent"] == "claude-code/0.1.0"
    assert captured["payload"]["messages"][0] == {"role": "system", "content": "You are OpenCode"}


def _patch_runtime(rs: MagicMock):
    return patch(
        "serving.config.runtime_settings.get_runtime_settings_instance",
        return_value=rs,
    )


def test_registry_has_kimi_coding_toggle() -> None:
    from serving.config.runtime_settings import RUNTIME_SETTINGS_REGISTRY

    entry = RUNTIME_SETTINGS_REGISTRY["kimi_coding_identity_enabled"]
    assert entry["type"] == "bool"
    assert entry["default"] is True
    assert entry.get("description")
