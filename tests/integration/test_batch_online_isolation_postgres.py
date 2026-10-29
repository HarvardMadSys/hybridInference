"""Online admission and Batch admission stay apart for Chat, Embeddings and Responses.

The Messages target is held to this by ``test_batch_expansion_http_postgres``;
these are the same two properties for the other three targets, through the real
scheduler, handlers and PostgreSQL (issue #1690): with the owner's only online
slot held by an online request, a batch item still runs, without touching the
online slot or the interactive load; and with the model's batch capacity taken
up by interactive load, the item waits until that load is released.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from fastapi import FastAPI, Header, HTTPException
from httpx import ASGITransport, AsyncClient

from routing.routers import FixedRouter
from serving.adapters.base import BaseAdapter, ModelConfig, UsageInfo
from serving.batch.interactive_load import INTERACTIVE_LOAD
from serving.batch.model_limits import BatchModelLimits
from serving.batch.scheduler import BatchScheduler
from serving.servers.auth import decrypt_api_key, verify_api_key, verify_batch_management_identity
from serving.servers.concurrency import UserConcurrencyLimiter, static_limits_provider
from serving.servers.deps import AppServices
from serving.servers.middleware.error import install_error_handlers
from serving.servers.routers import batches, completions, embeddings, responses
from serving.storage.batch_file_store import BatchFileStore
from serving.storage.batch_store import BatchStore
from serving.storage.batch_webhook_store import BatchWebhookStore
from tests.integration import test_batch_expansion_http_postgres as _expansion

# The PostgreSQL pool, the owner and the Batch settings of the Messages tests.
pool = _expansion.pool
owner = _expansion.owner
settings = _expansion.settings
_PRICING = _expansion._PRICING
_TIMINGS = _expansion._TIMINGS
_until = _expansion._until

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

KEY = {"Authorization": "Bearer test-key"}


class _Gate:
    """Per-prompt holds, so a test knows when a real handler reached the upstream."""

    def __init__(self) -> None:
        self.entered: dict[str, asyncio.Event] = {}
        self.gates: dict[str, asyncio.Event] = {}
        self.calls: list[str] = []

    def hold(self, text: str) -> asyncio.Event:
        self.entered.setdefault(text, asyncio.Event())
        return self.gates.setdefault(text, asyncio.Event())

    async def wait_entered(self, text: str) -> None:
        await asyncio.wait_for(self.entered.setdefault(text, asyncio.Event()).wait(), timeout=10)

    async def pass_through(self, text: str) -> None:
        self.calls.append(text)
        if text in self.entered:
            self.entered[text].set()
        gate = self.gates.get(text)
        if gate is not None:
            await gate.wait()


class _ChatUpstream(BaseAdapter):
    def __init__(self, model_id: str, gate: _Gate) -> None:
        super().__init__(
            ModelConfig(
                id=model_id,
                name=model_id,
                provider="openai",
                base_url=f"https://{model_id}.test",
                pricing=_PRICING,
            )
        )
        self.gate = gate

    async def chat_completion(self, messages: list[dict[str, Any]], **params: Any) -> dict:
        prompt = messages[-1]["content"]
        await self.gate.pass_through(prompt)
        return self.format_response(
            content=f"echo {prompt}",
            model=self.config.id,
            usage=UsageInfo(prompt_tokens=100, completion_tokens=50, total_tokens=150),
        )

    async def stream_chat_completion(self, messages, **params):  # pragma: no cover
        raise AssertionError("not streamed here")
        yield


class _Embedder:
    def __init__(self, model_id: str, gate: _Gate) -> None:
        self.config = ModelConfig(
            id=model_id,
            name=model_id,
            provider="openai",
            base_url=f"https://{model_id}.test",
            pricing=_PRICING,
        )
        self.gate = gate

    async def embeddings(self, inputs: Any, **params: Any) -> dict[str, Any]:
        await self.gate.pass_through(inputs if isinstance(inputs, str) else str(inputs))
        return {
            "object": "list",
            "data": [{"object": "embedding", "index": 0, "embedding": [0.1]}],
            "model": self.config.id,
            "usage": {"prompt_tokens": 10, "total_tokens": 10},
        }


@pytest_asyncio.fixture
async def gateway(pool, owner, settings):
    gate = _Gate()
    chat = _ChatUpstream(f"bxi-{uuid.uuid4().hex[:6]}", gate)
    embedder = _Embedder(f"bxe-{uuid.uuid4().hex[:6]}", gate)
    router = FixedRouter()
    router.register_route(chat.config.id, [(chat, 1.0)])
    router.register_embedding_route(
        embedder.config.id, [(embedder, 1.0)], aliases=[], required_role="free"
    )
    store = BatchStore(pool)
    log_store = MagicMock()
    log_store.log_request = AsyncMock()
    services = AppServices(
        router=router,
        log_store=log_store,
        operational_store=owner["op_store"],
        embedding_adapters={embedder.config.id: embedder},
        user_concurrency_limiter=UserConcurrencyLimiter(
            static_limits_provider({"free": 3, "pro": 3, "internal": 50, "admin": 0})
        ),
        batch_store=store,
        batch_model_limits=BatchModelLimits(store, ttl=0.05),
        batch_file_store=BatchFileStore(pool),
        batch_webhook_store=BatchWebhookStore(pool),
    )
    app = FastAPI()
    app.state.services = services
    caller = {
        "user_id": owner["user_id"],
        "role": "internal",
        "authenticated": True,
        "is_admin": False,
        "auth_key_hash": owner["key_hash"],
        "quota_exceeded": False,
        "quota_daily_cost_usd": 100.0,
        "spent_today_usd": 0.0,
    }

    async def identity(
        authorization: str | None = Header(None), x_api_key: str | None = Header(None)
    ):
        key = (authorization or "").removeprefix("Bearer ") or x_api_key
        if key != "test-key":
            raise HTTPException(401, "Invalid or expired API key")
        return dict(caller)

    app.dependency_overrides[verify_batch_management_identity] = identity
    app.dependency_overrides[verify_api_key] = identity
    install_error_handlers(app)
    for module in (completions, embeddings, responses, batches):
        app.include_router(module.router)

    workers: list[BatchScheduler] = []
    for name in ("one", "two"):
        worker = BatchScheduler(
            app=app,
            services=services,
            store=store,
            limits=services.batch_model_limits,
            file_store=services.batch_file_store,
            webhook_store=services.batch_webhook_store,
            decrypt_secret=decrypt_api_key,
            worker_id=f"{name}-{uuid.uuid4().hex[:6]}",
            timings=_TIMINGS,
        )
        worker.start()
        workers.append(worker)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gw.test") as client:
        try:
            yield type(
                "Gateway",
                (),
                {
                    "client": client,
                    "chat": chat,
                    "embedder": embedder,
                    "gate": gate,
                    "caller": caller,
                    "owner": owner,
                    "workers": workers,
                    "services": services,
                },
            )
        finally:
            for worker in workers:
                await worker.stop()


#: (target, online path, online body for *text*, batch url, batch body for *text*)
def _online(target: str, gateway, text: str) -> tuple[str, dict[str, Any]]:
    if target == "embeddings":
        return "/v1/embeddings", {"model": gateway.embedder.config.id, "input": text}
    return "/v1/chat/completions", {
        "model": gateway.chat.config.id,
        "messages": [{"role": "user", "content": text}],
    }


def _item(target: str, gateway, text: str) -> dict[str, Any]:
    if target == "embeddings":
        url, body = "/v1/embeddings", {"model": gateway.embedder.config.id, "input": text}
    elif target == "responses":
        url, body = "/v1/responses", {"model": gateway.chat.config.id, "input": text}
    else:
        url, body = (
            "/v1/chat/completions",
            {
                "model": gateway.chat.config.id,
                "messages": [{"role": "user", "content": text}],
            },
        )
    return {"custom_id": "item", "method": "POST", "url": url, "body": body}


def _canonical(target: str, gateway) -> str:
    return gateway.embedder.config.id if target == "embeddings" else gateway.chat.config.id


@asynccontextmanager
async def _held_online(gateway, target: str, text: str):
    gate = gateway.gate.hold(text)
    path, body = _online(target, gateway, text)
    online = asyncio.create_task(gateway.client.post(path, headers=KEY, json=body))
    try:
        await gateway.gate.wait_entered(text)
        yield online, gate
    finally:
        gate.set()
        await asyncio.wait_for(online, timeout=5)


async def _status(client: AsyncClient, batch_id: str, wanted: str) -> dict[str, Any]:
    async def reached():
        body = (await client.get(f"/v1/batches/{batch_id}", headers=KEY)).json()
        return body if body["status"] == wanted else None

    return await _until(reached, what=f"batch {batch_id} to be {wanted}")


@pytest.mark.parametrize("target", ["chat", "embeddings", "responses"])
@pytest.mark.parametrize(
    "settings",
    [
        {
            "BATCH_ROLE_CONCURRENCY_INTERNAL": "1",
            "BATCH_MODEL_CONCURRENCY": "2",
            "BATCH_YIELD_TO_INTERACTIVE": "1",
            "BATCH_INTERACTIVE_RESERVE": "0",
        }
    ],
    indirect=True,
    ids=["separate-user-quotas"],
)
async def test_an_online_request_does_not_starve_a_batch_item_of_the_same_owner(
    gateway, settings, target
):
    client, user_id = gateway.client, gateway.owner["user_id"]
    model = _canonical(target, gateway)
    limiter = gateway.services.user_concurrency_limiter
    gateway.caller["max_concurrent_requests"] = 1
    await gateway.owner["op_store"].update_user_fields(user_id, max_concurrent_requests=1)
    await gateway.workers[1].stop()
    batch_limiter = gateway.workers[0]._role_slots
    online_target = "embeddings" if target == "embeddings" else "chat"

    async with _held_online(gateway, online_target, "online-one") as (online, _gate):
        assert limiter._slots[user_id].in_use == 1
        assert INTERACTIVE_LOAD.in_flight(model) == 1
        path, body = _online(online_target, gateway, "online-two")
        refused = await client.post(path, headers=KEY, json=body)
        assert refused.status_code == 429, refused.text

        batch_gate = gateway.gate.hold("batch-one")
        try:
            created = await client.post(
                "/v1/batches",
                headers=KEY,
                json={"requests": [_item(target, gateway, "batch-one")]},
            )
            assert created.status_code == 200, created.text
            batch_id = created.json()["id"]
            await gateway.gate.wait_entered("batch-one")

            # The real handler is in flight: Batch holds its own quota slot and
            # leaves the online slot and the interactive load as they were.
            assert batch_limiter._slots[user_id].in_use == 1
            assert limiter._slots[user_id].in_use == 1
            assert INTERACTIVE_LOAD.in_flight(model) == 1

            batch_gate.set()
            final = await _status(client, batch_id, "completed")
            assert final["request_counts"] == {"total": 1, "completed": 1, "failed": 0}
            results = (await client.get(f"/v1/batches/{batch_id}/results", headers=KEY)).json()
            assert results["data"][0]["status"] == "completed"
            assert batch_limiter._slots[user_id].in_use == 0
            assert limiter._slots[user_id].in_use == 1
            assert INTERACTIVE_LOAD.in_flight(model) == 1
        finally:
            batch_gate.set()

    assert online.result().status_code == 200
    assert limiter._slots[user_id].in_use == 0
    assert INTERACTIVE_LOAD.in_flight(model) == 0


@pytest.mark.parametrize("target", ["chat", "embeddings", "responses"])
@pytest.mark.parametrize(
    "settings",
    [
        {
            "BATCH_MODEL_CONCURRENCY": "1",
            "BATCH_INTERACTIVE_RESERVE": "0",
            "BATCH_YIELD_TO_INTERACTIVE": "1",
        }
    ],
    indirect=True,
    ids=["interactive-load"],
)
async def test_a_full_model_capacity_holds_a_batch_item_until_the_online_load_ends(
    gateway, settings, target, monkeypatch
):
    client = gateway.client
    model = _canonical(target, gateway)
    online_target = "embeddings" if target == "embeddings" else "chat"
    rounds: asyncio.Queue[str] = asyncio.Queue()
    for worker in gateway.workers:
        real_top_up = worker._top_up

        async def top_up(model_id, cfg, now, interactive, real_top_up=real_top_up):
            await real_top_up(model_id, cfg, now, interactive)
            if model_id == model:
                rounds.put_nowait(model_id)

        monkeypatch.setattr(worker, "_top_up", top_up)

    async with _held_online(gateway, online_target, "capacity-online") as (online, online_gate):
        assert INTERACTIVE_LOAD.in_flight(model) == 1
        created = await client.post(
            "/v1/batches",
            headers=KEY,
            json={"requests": [_item(target, gateway, "capacity-batch")]},
        )
        assert created.status_code == 200, created.text
        batch_id = created.json()["id"]
        await asyncio.wait_for(rounds.get(), timeout=10)
        assert "capacity-batch" not in gateway.gate.calls

        online_gate.set()
        released = await asyncio.wait_for(asyncio.shield(online), timeout=5)
        assert released.status_code == 200, released.text
        assert INTERACTIVE_LOAD.in_flight(model) == 0
        final = await _status(client, batch_id, "completed")
        assert final["request_counts"] == {"total": 1, "completed": 1, "failed": 0}
        assert "capacity-batch" in gateway.gate.calls
