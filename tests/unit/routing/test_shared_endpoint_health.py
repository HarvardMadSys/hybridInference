"""Behavior contracts for sharing endpoint health across router strategies."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest

from routing.endpoint_health import EndpointHealthRegistry, _CircuitState
from routing.routers import FixedRouter
from routing.routewise.config import RouteWiseConfig
from routing.routewise.router import RouteWiseRouter
from serving.adapters.base import BaseAdapter, ModelConfig

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

_MODEL_ID = "shared-health-model"
_MESSAGES = [{"role": "user", "content": "hello"}]
_PRIMARY_ENDPOINT = f"{_MODEL_ID}:primary"
_BACKUP_ENDPOINT = f"{_MODEL_ID}:backup"


class _BehaviorAdapter(BaseAdapter):
    def __init__(
        self,
        config: ModelConfig,
        *,
        chat_error: BaseException | None = None,
        stream_error: BaseException | None = None,
    ) -> None:
        super().__init__(config)
        self.chat_error = chat_error
        self.stream_error = stream_error
        self.chat_calls = 0
        self.stream_calls = 0

    async def chat_completion(
        self,
        messages: list[dict[str, Any]],
        **params: Any,
    ) -> dict[str, Any]:
        self.chat_calls += 1
        if self.chat_error is not None:
            raise self.chat_error
        return self.format_response(content="ok", model=self.config.id)

    async def stream_chat_completion(
        self,
        messages: list[dict[str, Any]],
        **params: Any,
    ) -> AsyncGenerator[str, None]:
        self.stream_calls += 1
        if self.stream_error is not None:
            raise self.stream_error
        yield self.format_stream_chunk(model=self.config.id, content="ok")


def _adapter(
    provider: str,
    *,
    endpoint_id: str,
    price: str,
    chat_error: BaseException | None = None,
    stream_error: BaseException | None = None,
) -> _BehaviorAdapter:
    return _BehaviorAdapter(
        ModelConfig(
            id=_MODEL_ID,
            name=_MODEL_ID,
            provider=provider,
            base_url=f"https://{provider}.example/v1",
            endpoint_id=endpoint_id,
            pricing={"prompt": price, "completion": price},
        ),
        chat_error=chat_error,
        stream_error=stream_error,
    )


def _shared_router_pair(
    registry: EndpointHealthRegistry,
    primary: _BehaviorAdapter,
    backup: _BehaviorAdapter,
) -> tuple[FixedRouter, RouteWiseRouter]:
    fixed = FixedRouter(health_registry=registry)
    fixed.register_route(_MODEL_ID, [(primary, 0.5), (backup, 0.5)])
    routewise = RouteWiseRouter(
        route_table=fixed,
        config=RouteWiseConfig(
            budget_alpha=0.0,
            random_seed=0,
            fallback_mode="strict",
            db_bootstrap_enabled=False,
        ),
        health_registry=registry,
    )
    return fixed, routewise


@pytest.fixture(autouse=True)
def _single_failure_opens_circuit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "1")
    monkeypatch.setenv("CIRCUIT_COOLDOWN_SECONDS", "3600")
    monkeypatch.setenv("CIRCUIT_MIN_AVAILABILITY", "0.0")


def test_default_fixed_and_routewise_registries_are_isolated() -> None:
    fixed = FixedRouter()
    routewise = RouteWiseRouter(config=RouteWiseConfig(db_bootstrap_enabled=False))

    fixed._health_registry.record_success(_PRIMARY_ENDPOINT)

    assert fixed._health_registry is not routewise._health_registry
    assert _PRIMARY_ENDPOINT in fixed.get_provider_status()
    assert routewise.get_provider_status() == {}


@pytest.mark.asyncio
async def test_fixed_pinned_failure_excludes_endpoint_from_routewise() -> None:
    registry = EndpointHealthRegistry()
    primary = _adapter(
        "primary",
        endpoint_id=_PRIMARY_ENDPOINT,
        price="1",
        chat_error=ConnectionError("primary unavailable"),
    )
    backup = _adapter("backup", endpoint_id=_BACKUP_ENDPOINT, price="100")
    fixed, routewise = _shared_router_pair(registry, primary, backup)

    with patch("routing.endpoint_health.alert_slack", new=AsyncMock()):
        with pytest.raises(ConnectionError, match="primary unavailable"):
            await fixed.chat_completion(
                _MODEL_ID,
                _MESSAGES,
                pin_provider=_PRIMARY_ENDPOINT,
            )
        await asyncio.sleep(0)

    assert primary.chat_calls == 1
    assert backup.chat_calls == 0
    assert routewise.get_provider_status()[_PRIMARY_ENDPOINT]["circuit_state"] == (
        _CircuitState.OPEN
    )
    assert routewise._select_adapter(_MODEL_ID, {}) is backup


@pytest.mark.asyncio
async def test_routewise_failure_makes_fixed_initial_selection_skip_endpoint() -> None:
    registry = EndpointHealthRegistry()
    primary = _adapter(
        "primary",
        endpoint_id=_PRIMARY_ENDPOINT,
        price="1",
        chat_error=ConnectionError("routewise primary unavailable"),
    )
    backup = _adapter("backup", endpoint_id=_BACKUP_ENDPOINT, price="100")
    fixed, routewise = _shared_router_pair(registry, primary, backup)

    with patch("routing.endpoint_health.alert_slack", new=AsyncMock()):
        with pytest.raises(ConnectionError, match="routewise primary unavailable"):
            await routewise.chat_completion(_MODEL_ID, _MESSAGES)
        await asyncio.sleep(0)

        response = await fixed.chat_completion(_MODEL_ID, _MESSAGES)

    assert registry.snapshot()[_PRIMARY_ENDPOINT]["circuit_state"] == _CircuitState.OPEN
    assert response["_routing"]["endpoint_id"] == _BACKUP_ENDPOINT
    assert primary.chat_calls == 1
    assert backup.chat_calls == 1


@pytest.mark.asyncio
async def test_fixed_chat_fallback_skips_endpoint_with_open_shared_circuit() -> None:
    registry = EndpointHealthRegistry()
    primary = _adapter(
        "primary",
        endpoint_id=_PRIMARY_ENDPOINT,
        price="1",
        chat_error=ConnectionError("primary unavailable"),
    )
    blocked = _adapter(
        "blocked",
        endpoint_id=f"{_MODEL_ID}:blocked",
        price="2",
    )
    backup = _adapter("backup", endpoint_id=_BACKUP_ENDPOINT, price="3")
    fixed = FixedRouter(health_registry=registry)
    fixed.register_route(
        _MODEL_ID,
        [(primary, 1.0), (blocked, 1.0), (backup, 1.0)],
    )

    with (
        patch("routing.endpoint_health.alert_slack", new=AsyncMock()),
        patch("routing.routers.random.random", return_value=0.0),
    ):
        registry.record_failure(blocked.config.endpoint_id, reason="routewise_failure")
        await asyncio.sleep(0)
        response = await fixed.chat_completion(_MODEL_ID, _MESSAGES)

    assert response["_routing"]["endpoint_id"] == _BACKUP_ENDPOINT
    assert primary.chat_calls == 1
    assert blocked.chat_calls == 0
    assert backup.chat_calls == 1


@pytest.mark.asyncio
async def test_fixed_stream_fallback_skips_endpoint_with_open_shared_circuit() -> None:
    registry = EndpointHealthRegistry()
    primary = _adapter(
        "primary",
        endpoint_id=_PRIMARY_ENDPOINT,
        price="1",
        stream_error=ConnectionError("primary unavailable"),
    )
    blocked = _adapter(
        "blocked",
        endpoint_id=f"{_MODEL_ID}:blocked",
        price="2",
    )
    backup = _adapter("backup", endpoint_id=_BACKUP_ENDPOINT, price="3")
    fixed = FixedRouter(health_registry=registry)
    fixed.register_route(
        _MODEL_ID,
        [(primary, 1.0), (blocked, 1.0), (backup, 1.0)],
    )

    with (
        patch("routing.endpoint_health.alert_slack", new=AsyncMock()),
        patch("routing.routers.random.random", return_value=0.0),
    ):
        registry.record_failure(blocked.config.endpoint_id, reason="routewise_failure")
        await asyncio.sleep(0)
        chunks = [
            chunk
            async for chunk in fixed.stream_chat_completion(
                _MODEL_ID,
                _MESSAGES,
            )
        ]

    assert any(_BACKUP_ENDPOINT in chunk for chunk in chunks)
    assert primary.stream_calls == 1
    assert blocked.stream_calls == 0
    assert backup.stream_calls == 1


@pytest.mark.asyncio
async def test_fixed_pin_bypasses_open_circuit_and_updates_shared_health() -> None:
    registry = EndpointHealthRegistry()
    primary = _adapter("primary", endpoint_id=_PRIMARY_ENDPOINT, price="1")
    backup = _adapter("backup", endpoint_id=_BACKUP_ENDPOINT, price="100")
    fixed, routewise = _shared_router_pair(registry, primary, backup)

    with patch("routing.endpoint_health.alert_slack", new=AsyncMock()):
        registry.record_failure(_PRIMARY_ENDPOINT, reason="preopen")
        await asyncio.sleep(0)
        availability_while_open = routewise.get_provider_status()[_PRIMARY_ENDPOINT]["availability"]

        response = await fixed.chat_completion(
            _MODEL_ID,
            _MESSAGES,
            pin_provider=_PRIMARY_ENDPOINT,
        )

        status_after_success = routewise.get_provider_status()[_PRIMARY_ENDPOINT]
        assert response["_routing"]["endpoint_id"] == _PRIMARY_ENDPOINT
        assert status_after_success["circuit_state"] == _CircuitState.CLOSED
        assert status_after_success["availability"] > availability_while_open
        assert primary.chat_calls == 1
        assert backup.chat_calls == 0

        registry.record_failure(_PRIMARY_ENDPOINT, reason="preopen-again")
        await asyncio.sleep(0)
        availability_before_pinned_failure = routewise.get_provider_status()[_PRIMARY_ENDPOINT][
            "availability"
        ]
        primary.chat_error = ConnectionError("pinned endpoint failed")

        with pytest.raises(ConnectionError, match="pinned endpoint failed"):
            await fixed.chat_completion(
                _MODEL_ID,
                _MESSAGES,
                pin_provider=_PRIMARY_ENDPOINT,
            )
        await asyncio.sleep(0)

    status_after_failure = routewise.get_provider_status()[_PRIMARY_ENDPOINT]
    assert status_after_failure["circuit_state"] == _CircuitState.OPEN
    assert status_after_failure["availability"] < availability_before_pinned_failure
    assert primary.chat_calls == 2
    assert backup.chat_calls == 0
