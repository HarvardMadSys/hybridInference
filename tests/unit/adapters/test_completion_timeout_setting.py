"""Settings the configuration can change must be read when used, not when imported."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from serving.adapters.anthropic import AnthropicAdapter
from serving.adapters.base import ModelConfig
from serving.adapters.gemini import GeminiAdapter
from serving.config import app_config
from tests.fixtures.app_config_store import row

_BACKEND = Path(__file__).resolve().parents[3] / "apps" / "backend"

# Module constants recomputed by an app_config listener. Importing one by name
# binds the value it had at import, before the configuration loaded.
_CONFIGURED_CONSTANTS = {
    "_COMPLETION_TIMEOUT_S",
    "PREFILL_AWARE_ENABLED",
    "ELEPHANT_TOKENS",
    "ELEPHANT_LIMIT",
    "INTERVENE_TOKENS",
    "AFFINITY_BACKLOG_CEILING",
    "PRIORITY_INTERACTIVE",
    "PRIORITY_LARGE",
    "PRIORITY_ELEPHANT",
    "_PREFIX_HINT_TTL_SEC",
    "AFFINITY_ENABLED",
    "AFFINITY_MAX_AGE_SECONDS",
    "_MAX_STREAM_IDLE",
    "_MAX_FIRST_FRAME_IDLE",
    "_SMALL_MAXTOK_REROUTE_TARGET",
    "_SMALL_MAXTOK_THRESHOLD",
    "_SMALL_MAXTOK_FLOOR",
}


def test_no_backend_module_imports_a_configured_constant_by_name() -> None:
    offenders = []
    for path in sorted(_BACKEND.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name in _CONFIGURED_CONSTANTS:
                        offenders.append(f"{path.relative_to(_BACKEND)}:{node.lineno} {alias.name}")
    assert offenders == [], (
        "these imports freeze a configuration-driven value; read it through the "
        f"owning module at call time instead: {offenders}"
    )


def _set_timeout(seconds: str) -> None:
    app_config._apply(
        {"UPSTREAM_COMPLETION_TIMEOUT_S": row("UPSTREAM_COMPLETION_TIMEOUT_S", seconds)},
        boot=False,
    )


@pytest.mark.asyncio
async def test_the_anthropic_adapter_uses_the_current_timeout(monkeypatch) -> None:
    from serving.http import AsyncHTTPClient

    seen: list[float] = []

    async def fake_post(self, url, json=None, headers=None, timeout=None, retries=2) -> Any:
        seen.append(timeout.total)
        return {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "claude-test",
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }

    monkeypatch.setattr(AsyncHTTPClient, "json_post_with_retry", fake_post)
    adapter = AnthropicAdapter(
        ModelConfig(
            id="claude-test",
            name="Claude Test",
            provider="anthropic",
            base_url="https://api.anthropic.com",
            api_key="sk-ant-test",
        )
    )

    _set_timeout("42")
    await adapter.chat_completion(messages=[{"role": "user", "content": "hi"}], max_tokens=8)
    _set_timeout("77")
    await adapter.chat_completion(messages=[{"role": "user", "content": "hi"}], max_tokens=8)

    assert seen == [42.0, 77.0]


@pytest.mark.asyncio
async def test_the_gemini_adapter_uses_the_current_timeout() -> None:
    adapter = GeminiAdapter(
        ModelConfig(
            id="gemini-test",
            name="Gemini Test",
            provider="gemini",
            base_url="https://mock",
            api_key="test-key",
        )
    )
    adapter.http = MagicMock()
    adapter.http.json_post_with_retry = AsyncMock(
        return_value={"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}
    )

    _set_timeout("42")
    await adapter.chat_completion([{"role": "user", "content": "hi"}])

    assert adapter.http.json_post_with_retry.await_args.kwargs["timeout"].total == 42.0
