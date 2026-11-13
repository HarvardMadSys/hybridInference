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
from serving import quota
from serving.adapters.base import BaseAdapter, ModelConfig
from serving.servers.concurrency import (
    UserConcurrencyLimiter,
    _intake_slot_key,
    static_limits_provider,
)
from serving.servers.deps import AppServices, get_current_user
from serving.servers.routers import playground
from serving.spend_ledger import ledger_for
from serving.stream import done_sentinel
from serving.utils import context as req_ctx

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator

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
        self.params: list[dict[str, Any]] = []
        self.gate: asyncio.Event | None = None
        self.usage: dict[str, Any] | None = None

    async def chat_completion(self, messages: list[dict[str, Any]], **params) -> dict[str, Any]:
        return self.format_response(content="ok", model=self.config.id)

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params
    ) -> AsyncGenerator[str, None]:
        self.roles.append(req_ctx.get().get(req_ctx.USER_ROLE))
        self.params.append(dict(params))
        if self.gate is not None:
            await self.gate.wait()
        yield self.format_stream_chunk(model=self.config.id, content="ok")
        if self.usage is not None:
            yield f"data: {json.dumps({'choices': [], 'usage': self.usage})}\n\n"
        yield done_sentinel()


class _ReasoningAdapter(_Adapter):
    """A route that forwards reasoning effort and declares its public levels."""

    forwards_reasoning_effort = True

    def __init__(self) -> None:
        super().__init__("reasoning-model", "openai_compat", "https://reasoning.example/v1")
        self.config.supported_params = [
            *self.config.supported_params,
            "reasoning_effort",
        ]
        self.config.reasoning_efforts = ["low", "high"]


class _ReasoningDeployment:
    def __init__(self) -> None:
        self.adapter = _ReasoningAdapter()
        self.router = FixedRouter()
        self.router.register_route("reasoning-model", [(self.adapter, 1.0)])
        self.router._select_adapter = self._first  # type: ignore[assignment]

    def _first(self, model_id: str, **_: Any) -> Any:
        return self.router.routes[model_id].adapters[0][0]


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


class _Store:
    """The store surface the playground's quota gate and spend ledger use."""

    def __init__(self, *, daily_quota: float = 100.0, spent_today: float = 0.0) -> None:
        self.daily_quota = daily_quota
        self.spent_today = spent_today
        self.increments: list[dict[str, Any]] = []

    async def get_quota_context_for_user(self, user_id: str) -> list[dict[str, Any]]:
        return [{"quota_daily_cost_usd": self.daily_quota, "quota_monthly_cost_usd": None}]

    async def get_user_cost_today(self, user_id: str) -> float:
        return self.spent_today

    async def increment_user_cost(
        self,
        user_id: str,
        cost: float,
        *,
        day: str,
        api_key_id: int | None = None,
        requests: int = 1,
        **_: Any,
    ) -> None:
        self.increments.append(
            {"user_id": user_id, "cost": cost, "api_key_id": api_key_id, "day": day}
        )


class _ConcurrencyResolver:
    """A resolver that says no model is concurrency-exempt."""

    async def is_exempt(self, model: str) -> bool:
        return False


def _client(
    deployment: _Deployment,
    user: dict[str, Any],
    limit: int = 8,
    store: Any = None,
    limiter: UserConcurrencyLimiter | None = None,
    concurrency_resolver: Any = None,
) -> AsyncClient:
    app = FastAPI()
    app.include_router(playground.router)
    limits = {"free": limit, "pro": limit, "internal": limit, "admin": limit}
    app.state.services = AppServices(
        router=deployment.router,
        user_concurrency_limiter=limiter or UserConcurrencyLimiter(static_limits_provider(limits)),
        operational_store=store,
        model_concurrency_resolver=concurrency_resolver,
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


class _RecordingLimiter(UserConcurrencyLimiter):
    """Tracks the slots held at any moment, without depending on their keys."""

    def __init__(self) -> None:
        super().__init__(static_limits_provider({"internal": 8}))
        self.active: set[str] = set()

    async def try_acquire(
        self,
        user_id: str,
        role: str,
        is_admin: bool,
        max_concurrent_requests: int | None = None,
    ) -> tuple[bool, int, str]:
        granted, limit, label = await super().try_acquire(
            user_id, role, is_admin, max_concurrent_requests
        )
        if granted:
            self.active.add(user_id)
        return granted, limit, label

    def release(self, user_id: str) -> None:
        self.active.discard(user_id)
        super().release(user_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "payload"),
    [
        (
            "/internal/playground/chat",
            {"model": "open-model", "messages": [{"role": "user", "content": "hi"}]},
        ),
        (
            "/internal/playground/responses",
            {"model": "open-model", "input": [{"role": "user", "content": "hi"}]},
        ),
        (
            "/internal/playground/messages",
            {
                "model": "open-model",
                "max_tokens": 64,
                "messages": [{"role": "user", "content": "hi"}],
            },
        ),
    ],
)
async def test_the_body_is_read_while_the_intake_slot_is_held(
    path: str, payload: dict[str, Any]
) -> None:
    """Every format decodes its body under the admission, as the API does."""
    deployment = _Deployment()
    limiter = _RecordingLimiter()
    held: list[set[str]] = []

    async def body() -> AsyncIterator[bytes]:
        held.append(set(limiter.active))
        yield json.dumps(payload).encode()

    async with _client(
        deployment,
        INTERNAL,
        limiter=limiter,
        concurrency_resolver=_ConcurrencyResolver(),
    ) as client:
        response = await client.post(
            path,
            content=body(),
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 200
    assert held == [{_intake_slot_key("u-internal")}]


@pytest.mark.asyncio
async def test_an_over_quota_user_is_refused_before_a_malformed_body_is_validated() -> None:
    """The quota gate is the API's, and it wins over the schema's 422."""
    deployment = _Deployment()
    store = _Store(daily_quota=1.0, spent_today=2.0)
    async with _client(deployment, INTERNAL, store=store) as client:
        response = await client.post(
            "/internal/playground/chat",
            json={"model": "open-model", "messages": "not-a-list"},
        )

    assert response.status_code == 429


async def _responses(client: AsyncClient, model: str, **extra: Any) -> Any:
    body = {
        "model": model,
        "input": [{"role": "user", "content": "hi"}],
        **extra,
    }
    return await client.post("/internal/playground/responses", json=body)


async def _messages(client: AsyncClient, model: str, **extra: Any) -> Any:
    body = {
        "model": model,
        "max_tokens": 64,
        "messages": [{"role": "user", "content": "hi"}],
        **extra,
    }
    return await client.post("/internal/playground/messages", json=body)


@pytest.mark.asyncio
async def test_the_playground_serves_the_responses_and_messages_formats() -> None:
    deployment = _Deployment()
    async with _client(deployment, INTERNAL) as client:
        responses = await _responses(client, "open-model", stream=True)
        messages = await _messages(client, "open-model", stream=True)

    assert responses.status_code == 200
    assert "response.output_text.delta" in responses.text
    assert "ok" in responses.text
    assert messages.status_code == 200
    assert "content_block_delta" in messages.text
    assert "ok" in messages.text
    assert deployment.calls == 2


@pytest.mark.asyncio
async def test_the_new_formats_apply_the_same_model_access_rules() -> None:
    deployment = _Deployment()
    async with _client(deployment, INTERNAL) as client:
        responses = await _responses(client, "admin-model", stream=True)
        messages = await _messages(client, "admin-model", stream=True)

    assert responses.status_code == 404
    assert messages.status_code == 404
    assert deployment.calls == 0


@pytest.mark.asyncio
async def test_models_publish_reasoning_efforts_and_chat_forwards_the_choice() -> None:
    deployment = _ReasoningDeployment()
    async with _client(deployment, INTERNAL) as client:
        listing = await client.get("/internal/playground/models")
        chat = await _chat(client, "reasoning-model", reasoning_effort="high")

    models = {model["id"]: model for model in listing.json()["models"]}
    assert models["reasoning-model"]["reasoning_efforts"] == ["low", "high"]
    assert chat.status_code == 200
    assert deployment.adapter.params[-1]["reasoning_effort"] == "high"


@pytest.mark.asyncio
async def test_models_without_a_reasoning_domain_publish_an_empty_list() -> None:
    deployment = _Deployment()
    async with _client(deployment, INTERNAL) as client:
        listing = await client.get("/internal/playground/models")

    models = {model["id"]: model for model in listing.json()["models"]}
    assert models["open-model"]["reasoning_efforts"] == []


@pytest.mark.asyncio
async def test_an_over_quota_user_is_refused_in_every_format() -> None:
    """The console session's budget gates the playground as an API key gates `/v1`."""
    deployment = _Deployment()
    store = _Store(daily_quota=1.0, spent_today=2.0)
    async with _client(deployment, INTERNAL, store=store) as client:
        chat = await _chat(client, "open-model")
        responses = await _responses(client, "open-model", stream=True)
        messages = await _messages(client, "open-model", stream=True)

    assert (chat.status_code, responses.status_code, messages.status_code) == (429, 429, 429)
    assert deployment.calls == 0


@pytest.mark.asyncio
async def test_the_playground_bills_the_session_user() -> None:
    """A chat turn's cost is recorded against the signed-in user, with no API key."""
    deployment = _Deployment()
    deployment.lan.config.pricing = {"prompt": "1", "completion": "1"}
    deployment.lan.usage = {"prompt_tokens": 1000, "completion_tokens": 1000}
    store = _Store()
    async with _client(deployment, INTERNAL, store=store) as client:
        response = await _chat(client, "open-model")

    assert response.status_code == 200
    await ledger_for(store).flush()
    assert [increment["user_id"] for increment in store.increments] == ["u-internal"]
    assert store.increments[0]["api_key_id"] is None
    assert store.increments[0]["cost"] > 0


@pytest.mark.asyncio
async def test_the_playground_reserves_the_request_estimate() -> None:
    """The API reserves what a request may cost, not just the flat minimum."""
    deployment = _Deployment()
    deployment.lan.config.pricing = {"prompt": "1", "completion": "100"}
    deployment.lan.gate = asyncio.Event()
    store = _Store()
    async with _client(deployment, INTERNAL, store=store) as client:
        turn = asyncio.create_task(_chat(client, "open-model", max_tokens=32768))
        ledger = ledger_for(store)
        try:
            for _ in range(500):
                if ledger._reserved_users.get("u-internal"):
                    break
                await asyncio.sleep(0.01)
            reserved = ledger._reserved_users["u-internal"][0]
        finally:
            deployment.lan.gate.set()
        response = await turn

    assert response.status_code == 200
    assert reserved > quota.ESTIMATED_REQUEST_COST_USD


@pytest.mark.asyncio
async def test_the_new_formats_forward_reasoning_effort() -> None:
    deployment = _ReasoningDeployment()
    async with _client(deployment, INTERNAL) as client:
        responses = await _responses(
            client, "reasoning-model", stream=True, reasoning={"effort": "high"}
        )
        responses_params = deployment.adapter.params[-1]
        messages = await _messages(
            client, "reasoning-model", stream=True, output_config={"effort": "low"}
        )
        messages_params = deployment.adapter.params[-1]

    assert responses.status_code == 200
    assert responses_params["reasoning_effort"] == "high"
    assert messages.status_code == 200
    assert messages_params["reasoning_effort"] == "low"
