"""The playground applies the inference surfaces' access rules to internal users.

``/internal/playground/*`` is open to the ``internal`` role, not only to
administrators, yet it skipped the model access check, let any caller pin any
route (a pin skips circuit admission, and reaches a staging route no weighted
draw would pick), never told the key pool who the caller was, held no
concurrency slot, and showed every caller the upstream's host and port.

The rules under test: the model access check decides what an internal user may
list and call; only an administrator may list routes or pin one; the caller's
role reaches the key pool; the host is shown to administrators only; and each
request holds one of the caller's concurrency slots until its stream ends.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from routing.routers import FixedRouter
from serving.adapters.base import BaseAdapter, ModelConfig
from serving.servers.concurrency import UserConcurrencyLimiter, static_limits_provider
from serving.servers.deps import AppServices, get_current_user
from serving.servers.routers import playground
from serving.stream import done_sentinel
from serving.utils import context as req_ctx

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

INTERNAL = {"user_id": "u-internal", "role": "internal", "is_admin": False}
ADMIN = {"user_id": "u-admin", "role": "admin", "is_admin": True}


class _Adapter(BaseAdapter):
    """Streams one token; records the caller role the key pool would read."""

    def __init__(self, model_id: str, provider: str, base_url: str) -> None:
        super().__init__(
            ModelConfig(
                id=model_id,
                name=model_id,
                provider=provider,
                base_url=base_url,
                endpoint_id=f"{model_id}:{provider}",
            )
        )
        self.roles: list[Any] = []
        self.gate: asyncio.Event | None = None

    async def chat_completion(self, messages: list[dict[str, Any]], **params) -> dict[str, Any]:
        return self.format_response(content="ok", model=self.config.id)

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params
    ) -> AsyncGenerator[str, None]:
        self.roles.append(req_ctx.get().get(req_ctx.USER_ROLE))
        if self.gate is not None:
            await self.gate.wait()
        yield self.format_stream_chunk(model=self.config.id, content="ok")
        yield done_sentinel()


class _Deployment:
    def __init__(self) -> None:
        self.lan = _Adapter("open-model", "lan", "http://10.0.0.5:8000/v1")
        self.staging = _Adapter("open-model", "staging", "https://staging.example/v1")
        self.private = _Adapter("admin-model", "private", "https://private.example/v1")
        self.router = FixedRouter()
        self.router.register_route("open-model", [(self.lan, 1.0), (self.staging, 0.0001)])
        self.router.register_route("admin-model", [(self.private, 1.0)], required_role="admin")
        self.router._select_adapter = self._first  # type: ignore[assignment]

    def _first(self, model_id: str, *, pin_provider: str | None = None, **_: Any) -> Any:
        route = self.router.routes[model_id]
        for adapter, _weight in route.adapters:
            if pin_provider in (None, adapter.config.provider):
                return adapter
        return None

    @property
    def calls(self) -> int:
        return len(self.lan.roles) + len(self.staging.roles) + len(self.private.roles)


def _client(deployment: _Deployment, user: dict[str, Any], limit: int = 8) -> AsyncClient:
    app = FastAPI()
    app.include_router(playground.router)
    limits = {"free": limit, "pro": limit, "internal": limit, "admin": limit}
    app.state.services = AppServices(
        router=deployment.router,
        user_concurrency_limiter=UserConcurrencyLimiter(static_limits_provider(limits)),
    )
    app.dependency_overrides[get_current_user] = lambda: user
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _chat(client: AsyncClient, model: str, **extra: Any) -> Any:
    body = {"model": model, "messages": [{"role": "user", "content": "hi"}], **extra}
    return await client.post("/internal/playground/chat", json=body)


def _route_frames(response: Any) -> list[dict[str, Any]]:
    frames = [
        json.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    return [frame["_playground_route"] for frame in frames if "_playground_route" in frame]


@pytest.mark.asyncio
async def test_an_internal_user_neither_lists_nor_calls_an_admin_only_model() -> None:
    deployment = _Deployment()
    async with _client(deployment, INTERNAL) as client:
        listed = (await client.get("/internal/playground/models")).json()["models"]
        response = await _chat(client, "admin-model")
        unknown = await _chat(client, "no-such-model")

    assert [model["id"] for model in listed] == ["open-model"]
    # Answered exactly as an unknown model is.
    assert (response.status_code, unknown.status_code) == (404, 404)
    assert response.json()["detail"] == "Model 'admin-model' not found"
    assert deployment.calls == 0


@pytest.mark.asyncio
async def test_an_administrator_still_lists_and_calls_every_model() -> None:
    deployment = _Deployment()
    async with _client(deployment, ADMIN) as client:
        listed = (await client.get("/internal/playground/models")).json()["models"]
        response = await _chat(client, "admin-model")

    assert sorted(model["id"] for model in listed) == ["admin-model", "open-model"]
    assert response.status_code == 200
    assert deployment.private.roles == ["admin"]


@pytest.mark.asyncio
async def test_only_an_administrator_may_list_or_pin_a_route() -> None:
    deployment = _Deployment()
    async with _client(deployment, INTERNAL) as client:
        (listed,) = (await client.get("/internal/playground/models")).json()["models"]
        refused = await _chat(client, "open-model", provider="staging")
    assert listed["providers"] == []
    assert refused.status_code == 403
    assert deployment.calls == 0

    async with _client(deployment, ADMIN) as client:
        admin_listed = (await client.get("/internal/playground/models")).json()["models"]
        pinned = await _chat(client, "open-model", provider="staging")
    open_model = next(model for model in admin_listed if model["id"] == "open-model")
    assert [provider["id"] for provider in open_model["providers"]] == [
        "open-model:lan",
        "open-model:staging",
    ]
    assert pinned.status_code == 200
    assert deployment.staging.roles == ["admin"]


@pytest.mark.asyncio
async def test_the_caller_role_reaches_the_key_pool() -> None:
    deployment = _Deployment()
    async with _client(deployment, INTERNAL) as client:
        response = await _chat(client, "open-model")

    assert response.status_code == 200
    # Without it the key pool treated the caller as unrestricted.
    assert deployment.lan.roles == ["internal"]


@pytest.mark.asyncio
async def test_the_upstream_host_is_shown_to_administrators_only() -> None:
    deployment = _Deployment()
    async with _client(deployment, INTERNAL) as client:
        internal = await _chat(client, "open-model")
    async with _client(deployment, ADMIN) as client:
        admin = await _chat(client, "open-model")

    assert _route_frames(internal) == [{"provider": "lan", "endpoint_id": "open-model:lan"}]
    assert "10.0.0.5" not in internal.text
    assert _route_frames(admin) == [
        {"provider": "lan", "endpoint_id": "open-model:lan", "host": "10.0.0.5:8000"}
    ]


@pytest.mark.asyncio
async def test_a_stream_holds_one_of_the_callers_slots_until_it_ends() -> None:
    deployment = _Deployment()
    deployment.lan.gate = asyncio.Event()
    async with _client(deployment, INTERNAL, limit=1) as client:
        # Refusals hand their slot back as well.
        for _ in range(2):
            assert (await _chat(client, "admin-model")).status_code == 404

        first = asyncio.create_task(_chat(client, "open-model"))
        for _ in range(200):
            if deployment.lan.roles:
                break
            await asyncio.sleep(0)
        second = await _chat(client, "open-model")
        deployment.lan.gate.set()
        first_response = await first
        third = await _chat(client, "open-model")

    assert second.status_code == 429
    assert second.headers["retry-after"] == "1"
    assert first_response.status_code == 200
    assert third.status_code == 200
