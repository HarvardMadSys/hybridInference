"""Anthropic serving paths must require evidence of model progress."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import Request

from routing.endpoint_health import EndpointHealthRegistry, _CircuitState
from serving.servers.routers import anthropic_messages


class _FakeRegistry(EndpointHealthRegistry):
    def __init__(self) -> None:
        super().__init__()
        self.success_outcomes: list[Any] = []

    def record_success(self, endpoint_id: str, *, outcome: Any = None) -> None:
        self.success_outcomes.append(outcome)
        super().record_success(endpoint_id, outcome=outcome)


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/messages",
            "headers": [],
            "query_string": b"",
            "app": None,
        }
    )


@pytest.mark.asyncio
async def test_nonstreaming_anthropic_success_requires_model_progress(monkeypatch) -> None:
    """A warmup response must not clear the Anthropic endpoint's breaker."""
    registry = _FakeRegistry()
    for _ in range(3):
        registry.record_failure("endpoint", reason="upstream_502")
    assert registry.snapshot()["endpoint"]["circuit_state"] == _CircuitState.OPEN

    adapter = AsyncMock()
    adapter.config.endpoint_id = "endpoint"
    adapter.native_format = "anthropic"
    adapter.messages.return_value = {
        "content": [{"type": "text", "text": "The model is starting up. Please wait..."}],
        "usage": {"output_tokens": 1},
    }
    monkeypatch.setattr(
        anthropic_messages,
        "_pick_dispatch_adapter",
        AsyncMock(return_value=(adapter, None, None)),
    )
    monkeypatch.setattr(
        anthropic_messages,
        "_apply_reasoning_effort",
        lambda *args, **kwargs: None,
    )

    await anthropic_messages.anthropic_messages(
        request=_request(),
        user_ctx={"authenticated": True},
        router_exec=AsyncMock(endpoint_health_registry=registry),
        log_store=None,
        op_store=None,
        model_visibility_resolver=AsyncMock(),
        _conc=None,
    )

    assert registry.success_outcomes == []
    assert registry.snapshot()["endpoint"]["circuit_state"] == _CircuitState.OPEN
