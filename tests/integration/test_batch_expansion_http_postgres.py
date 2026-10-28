"""The batch API end to end over HTTP, against Postgres and real workers.

The routers, the store, the handlers, cost accounting and the owner's
re-authentication from real ``users``/``api_keys`` rows are the production code;
only the upstream model is a stub, and the caller's management identity is
resolved from a test key table (the key-to-account checks themselves are covered
by the auth tests). It all runs in this one test process: one FastAPI app, one
``AppServices``, reached in process through ``ASGITransport``, with two
``BatchScheduler`` workers on the same event loop sharing one real Postgres
database -- several workers claiming against each other through the database,
not several gateway processes (that is exercised by running separate gateway
processes, outside this suite). What this
holds: a Messages batch -- inline or from a file -- runs on ``/v1/batches``
under the ``anthropic-version`` it was created with, every request accounted for; a
file batch is validated, run, sealed and downloaded byte-for-byte as its
manifest says, and an invalid file runs nothing; idempotent creates replay
before the quota and role checks, and the version is part of what they
compare; batches the retired Message Batches API stored are read, listed,
cancelled, downloaded and deleted here, by their owner only; and a webhook
arrives, signed, only after the results are readable.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from aiohttp import web
from fastapi import FastAPI, Header, HTTPException
from fastapi.exceptions import HTTPException as FastAPIHTTPException
from httpx import ASGITransport, AsyncClient
from starlette.exceptions import HTTPException as StarletteHTTPException

from routing.routers import FixedRouter
from serving.adapters.anthropic import AnthropicAdapter
from serving.adapters.base import BaseAdapter, ModelConfig, UsageInfo
from serving.batch import config as batch_config_module, webhooks
from serving.batch.interactive_load import INTERACTIVE_LOAD
from serving.batch.model_limits import BatchModelLimits
from serving.batch.scheduler import BatchScheduler, SchedulerTimings
from serving.config.settings import get_settings
from serving.servers import auth as auth_module
from serving.servers.auth import decrypt_api_key, verify_api_key, verify_batch_management_identity
from serving.servers.concurrency import UserConcurrencyLimiter, static_limits_provider
from serving.servers.deps import (
    AppServices,
    get_admin_audit_actor,
    get_current_user,
    verify_admin_access,
)
from serving.servers.middleware.error import install_error_handlers
from serving.servers.routers import (
    anthropic_messages,
    batch_console,
    batches,
    files,
    retired_message_batches,
)
from serving.servers.routers.admin import batch as admin_batch
from serving.storage.batch_file_store import BatchFileStore
from serving.storage.batch_store import BatchStore
from serving.storage.batch_webhook_store import BatchWebhookStore
from serving.storage.postgres_operational import PostgresOperationalStore
from tests.fixtures import postgres

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_PRICING = {
    "prompt": "1",
    "completion": "2",
    "input_cache_reads": "0",
    "input_cache_writes": "0",
    "image": "0",
    "request": "0",
}
_TIMINGS = SchedulerTimings(
    poll_seconds=0.02,
    idle_poll_seconds=0.05,
    sweep_seconds=0.2,
    heartbeat_seconds=0.2,
    lease_seconds=10,
    max_attempts=3,
    backoff_base_seconds=0.01,
    backoff_cap_seconds=0.02,
    slot_retry_seconds=0.05,
    owner_retry_seconds=0.05,
    stop_grace_seconds=1.0,
    work_lease_seconds=10,
    webhook_poll_seconds=0.05,
)


class _Upstream(BaseAdapter):
    def __init__(self, model_id: str) -> None:
        super().__init__(
            ModelConfig(
                id=model_id,
                name=model_id,
                provider="openai",
                base_url=f"https://{model_id}.test",
                pricing=_PRICING,
            )
        )
        self.calls: list[str] = []

    async def chat_completion(self, messages: list[dict[str, Any]], **params: Any) -> dict:
        prompt = messages[-1]["content"]
        self.calls.append(prompt if isinstance(prompt, str) else json.dumps(prompt))
        await asyncio.sleep(0.005)
        return self.format_response(
            content=f"echo {prompt}",
            model=self.config.id,
            usage=UsageInfo(prompt_tokens=100, completion_tokens=50, total_tokens=150),
        )

    async def stream_chat_completion(self, messages, **params):  # pragma: no cover
        raise AssertionError("batch items never stream")
        yield


class _NativeUpstream(AnthropicAdapter):
    """A native Anthropic route whose upstream is a stub: it records what it was sent.

    ``_api_version`` is the adapter's own choice of the ``anthropic-version``
    header it would send, so the recorded value is what a real upstream gets.
    """

    def __init__(self, model_id: str) -> None:
        super().__init__(
            ModelConfig(
                id=model_id,
                name=model_id,
                provider="anthropic",
                base_url=f"https://{model_id}.test",
                pricing=_PRICING,
            )
        )
        self.calls: list[tuple[str, dict[str, Any]]] = []
        # Per-prompt events, so a test can hold one upstream call in flight and
        # know exactly when the real handler reached it.
        self.entered: dict[str, asyncio.Event] = {}
        self.gates: dict[str, asyncio.Event] = {}

    def hold(self, text: str) -> asyncio.Event:
        """Make the call carrying *text* wait until the returned event is set."""
        self.entered.setdefault(text, asyncio.Event())
        return self.gates.setdefault(text, asyncio.Event())

    async def wait_entered(self, text: str) -> None:
        """Wait until the real handler has handed the upstream a call with *text*."""
        await asyncio.wait_for(self.entered.setdefault(text, asyncio.Event()).wait(), timeout=10)

    async def messages(self, body, *, request_id, extra_headers=None):
        text = body["messages"][-1]["content"]
        self.calls.append((self._api_version(), json.loads(json.dumps(body))))
        if isinstance(text, str) and text in self.entered:
            self.entered[text].set()
        gate = self.gates.get(text) if isinstance(text, str) else None
        if gate is not None:
            await gate.wait()
        else:
            await asyncio.sleep(0.005)
        return {
            "id": f"msg_{uuid.uuid4().hex[:8]}",
            "type": "message",
            "role": "assistant",
            "model": self.config.id,
            "content": [{"type": "text", "text": f"echo {text}"}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 100, "output_tokens": 50},
        }


@pytest_asyncio.fixture
async def pool():
    pool = await postgres.create_pool(min_size=2, max_size=16)
    await BatchStore(pool).initialize()
    try:
        yield pool
    finally:
        await pool.close()


@pytest_asyncio.fixture
async def owner(pool):
    tag = uuid.uuid4().hex[:10]
    user_id, key_hash = f"u-bxhttp-{tag}", f"hash-bxhttp-{tag}"
    op_store = PostgresOperationalStore(pool)
    await op_store.create_user(
        user_id=user_id, email=f"{tag}@bxhttp.test", password_hash="x", email_verified=True
    )
    await op_store.update_user_fields(user_id, role="internal")
    await op_store.create_key(
        key_hash=key_hash,
        key_prefix=f"sk-bxhttp-{tag}",
        user_id=user_id,
        account_id=user_id,
        quota_daily_cost_usd=Decimal("100.00"),
    )
    try:
        yield {"user_id": user_id, "key_hash": key_hash, "op_store": op_store}
    finally:
        await BatchStore(pool).delete_user_batches(user_id)
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM user_daily_cost WHERE user_id = $1", user_id)
            await conn.execute("DELETE FROM api_keys WHERE user_id = $1", user_id)
            await conn.execute("DELETE FROM users WHERE id = $1", user_id)


@pytest.fixture
def settings(monkeypatch, request) -> dict[str, str]:
    values = {
        # Left over from a version that held the newer kinds back: it controls
        # nothing now, so everything below runs with it off.
        "BATCH_EXPANSION_ENABLED": "0",
        "BATCH_WEBHOOK_ALLOW_PRIVATE_TARGETS": "1",
        "BATCH_SHARED_INTERACTIVE_LOAD": "0",
    }
    # Configure admission before gateway starts its workers: an already-running
    # scheduling round may otherwise still hold the previous config snapshot.
    values.update(getattr(request, "param", {}))
    monkeypatch.setattr(
        batch_config_module, "config_value", lambda key, default=None: values.get(key, default)
    )
    # The webhook secret is stored encrypted under API_KEY_SECRET: give this
    # test a synthetic one rather than relying on the ambient environment.
    synthetic = get_settings().model_copy(
        update={"api_key_secret": "batch-expansion-test-only-secret"}
    )
    monkeypatch.setattr(auth_module, "get_settings", lambda: synthetic)
    return values


@pytest_asyncio.fixture
async def gateway(pool, owner, settings):
    model = _Upstream(f"bx-{uuid.uuid4().hex[:6]}")
    native = _NativeUpstream(f"bxn-{uuid.uuid4().hex[:6]}")
    router = FixedRouter()
    router.register_route(model.config.id, [(model, 1.0)])
    router.register_route(native.config.id, [(native, 1.0)])
    store = BatchStore(pool)
    # The Messages surface bills through its log path, so a log store is wired.
    log_store = MagicMock()
    log_store.log_request = AsyncMock()
    services = AppServices(
        router=router,
        log_store=log_store,
        operational_store=owner["op_store"],
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

    # Someone else, holding a key of their own: reads and manages only theirs.
    stranger = {**caller, "user_id": f"stranger-{uuid.uuid4().hex[:6]}", "auth_key_hash": None}

    async def identity(
        authorization: str | None = Header(None), x_api_key: str | None = Header(None)
    ):
        key = (authorization or "").removeprefix("Bearer ") or x_api_key
        if key == "stranger-key":
            return dict(stranger)
        if key != "test-key":
            raise HTTPException(401, "Invalid or expired API key")
        return dict(caller)

    async def console_user(authorization: str | None = Header(None)):
        if authorization != "Bearer console":
            raise HTTPException(401, "no session")
        return {"user_id": owner["user_id"], "role": "internal"}

    app.dependency_overrides[verify_batch_management_identity] = identity
    app.dependency_overrides[verify_api_key] = identity
    app.dependency_overrides[get_current_user] = console_user
    app.dependency_overrides[verify_admin_access] = lambda: "admin-test"
    app.dependency_overrides[get_admin_audit_actor] = lambda: None
    install_error_handlers(app)
    for exc_type in (StarletteHTTPException, FastAPIHTTPException):
        app.add_exception_handler(
            exc_type, anthropic_messages.anthropic_aware_http_exception_handler
        )
    for module in (
        anthropic_messages,
        batches,
        retired_message_batches,
        files,
        batch_console,
        admin_batch,
    ):
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
                    "model": model,
                    "native": native,
                    "store": store,
                    "caller": caller,
                    "pool": pool,
                    "owner": owner,
                    "workers": workers,
                    "services": services,
                },
            )
        finally:
            for worker in workers:
                await worker.stop()


KEY = {"Authorization": "Bearer test-key"}
STRANGER = {"Authorization": "Bearer stranger-key"}


def _v(version: str, **extra: str) -> dict[str, str]:
    """The owner's headers for a Messages create: the version is the anthropic-version header."""
    return {**KEY, "anthropic-version": version, **extra}


async def _until(check, *, timeout: float = 20.0, what: str = "condition"):
    deadline = time.monotonic() + timeout
    while True:
        value = await check()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.05)


def _messages_request(custom_id: str, model: str, text: str, **extra: Any) -> dict[str, Any]:
    return {
        "custom_id": custom_id,
        "method": "POST",
        "url": "/v1/messages",
        "body": {
            "model": model,
            "max_tokens": 32,
            "messages": [{"role": "user", "content": text}],
            **extra,
        },
    }


async def _status(client: AsyncClient, batch_id: str, wanted: str, what: str) -> dict[str, Any]:
    async def reached():
        body = (await client.get(f"/v1/batches/{batch_id}", headers=KEY)).json()
        return body if body["status"] == wanted else None

    return await _until(reached, what=what)


def _online_messages(model: str, text: str) -> dict[str, Any]:
    """A direct (non-batch) Messages request body."""
    return {
        "model": model,
        "max_tokens": 32,
        "messages": [{"role": "user", "content": text}],
    }


def _native_versions(native: _NativeUpstream) -> dict[str, str]:
    """The protocol version observed by the native adapter for each prompt."""
    return {body["messages"][-1]["content"]: version for version, body in native.calls}


@asynccontextmanager
async def _held_online_request(gateway, text: str):
    """Hold real online admission and always finish the request, including on assertion failure."""
    gate = gateway.native.hold(text)
    online = asyncio.create_task(
        gateway.client.post(
            "/v1/messages",
            headers=_v("2023-06-01"),
            json=_online_messages(gateway.native.config.id, text),
        )
    )
    try:
        await gateway.native.wait_entered(text)
        yield online, gate
    finally:
        gate.set()
        await asyncio.wait_for(online, timeout=5)


def _observe_top_up(monkeypatch, workers, model: str) -> asyncio.Queue[str]:
    """Observe complete production scheduling rounds for this test's unique model."""
    rounds: asyncio.Queue[str] = asyncio.Queue()
    for worker in workers:
        real_top_up = worker._top_up

        async def top_up(model_id, cfg, now, interactive, real_top_up=real_top_up):
            await real_top_up(model_id, cfg, now, interactive)
            if model_id == model:
                rounds.put_nowait(model_id)

        monkeypatch.setattr(worker, "_top_up", top_up)
    return rounds


@pytest.mark.parametrize("input_method", ["direct", "file"])
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
async def test_an_online_request_does_not_starve_a_messages_batch_for_the_same_owner(
    gateway, settings, input_method
):
    """Real online and Batch execution retain independent user slots and load accounting."""
    client, native, model = gateway.client, gateway.native, gateway.native.config.id
    user_id = gateway.owner["user_id"]
    limiter = gateway.services.user_concurrency_limiter
    gateway.caller["max_concurrent_requests"] = 1
    # The worker reauthenticates from PostgreSQL, so both paths see the same limit.
    await gateway.owner["op_store"].update_user_fields(user_id, max_concurrent_requests=1)
    # A single active worker makes the per-process Batch quota unambiguous.
    await gateway.workers[1].stop()
    batch_limiter = gateway.workers[0]._role_slots

    async with _held_online_request(gateway, "online-one") as (online, _online_gate):
        assert limiter._slots[user_id].in_use == 1
        assert INTERACTIVE_LOAD.in_flight(model) == 1
        refused = await client.post(
            "/v1/messages", headers=KEY, json=_online_messages(model, "online-two")
        )
        assert refused.status_code == 429, refused.text
        assert "online-two" not in _native_versions(native)

        rows = [_messages_request("first", model, "batch-one")]
        if input_method == "file":
            uploaded = await _upload(client, (json.dumps(rows[0]) + "\n").encode())
            payload = {
                "input_file_id": uploaded["id"],
                "endpoint": "/v1/messages",
                "completion_window": "24h",
            }
        else:
            payload = {"requests": rows}

        batch_gate = native.hold("batch-one")
        try:
            created = await client.post("/v1/batches", headers=_v("2026-10-21"), json=payload)
            assert created.status_code == 200, created.text
            batch_id = created.json()["id"]
            await native.wait_entered("batch-one")

            # The real handler is in flight: Batch owns its slot without
            # changing the online quota or publishing interactive load.
            assert batch_limiter._slots[user_id].in_use == 1
            assert limiter._slots[user_id].in_use == 1
            assert INTERACTIVE_LOAD.in_flight(model) == 1
            held = (await client.get(f"/v1/batches/{batch_id}", headers=KEY)).json()
            assert held["status"] == "in_progress"
            assert held["request_counts"] == {"total": 1, "completed": 0, "failed": 0}

            batch_gate.set()
            final = await _status(client, batch_id, "completed", "the Messages batch to complete")
            assert final["request_counts"] == {"total": 1, "completed": 1, "failed": 0}
            assert final["anthropic_version"] == "2026-10-21"
            results = (await client.get(f"/v1/batches/{batch_id}/results", headers=KEY)).json()
            result = results["data"][0]
            assert result["status"] == "completed"
            assert result["response"]["body"]["type"] == "message"
            assert result["response"]["body"]["content"][0]["text"] == "echo batch-one"
            assert _native_versions(native)["batch-one"] == "2026-10-21"

            assert batch_limiter._slots[user_id].in_use == 0
            assert not online.done()
            assert limiter._slots[user_id].in_use == 1
            assert INTERACTIVE_LOAD.in_flight(model) == 1
            refused = await client.post(
                "/v1/messages", headers=KEY, json=_online_messages(model, "online-three")
            )
            assert refused.status_code == 429, refused.text
        finally:
            batch_gate.set()

    response = online.result()
    assert response.status_code == 200, response.text
    assert response.json()["content"][0]["text"] == "echo online-one"
    assert limiter._slots[user_id].in_use == 0
    assert INTERACTIVE_LOAD.in_flight(model) == 0
    freed = await client.post(
        "/v1/messages", headers=KEY, json=_online_messages(model, "online-four")
    )
    assert freed.status_code == 200, freed.text
    assert limiter._slots[user_id].in_use == 0
    assert INTERACTIVE_LOAD.in_flight(model) == 0


@pytest.mark.parametrize(
    "settings",
    [
        {
            "BATCH_MODEL_CONCURRENCY": "1",
            "BATCH_INTERACTIVE_RESERVE": "0",
            "BATCH_YIELD_TO_INTERACTIVE": "1",
        },
        {
            "BATCH_MODEL_CONCURRENCY": "2",
            "BATCH_INTERACTIVE_RESERVE": "1",
            "BATCH_YIELD_TO_INTERACTIVE": "1",
        },
    ],
    indirect=True,
    ids=["interactive-load", "interactive-reserve"],
)
async def test_a_full_model_capacity_holds_a_messages_batch_until_pressure_is_released(
    gateway, settings, monkeypatch
):
    """A complete real scheduling round respects interactive load and reserved capacity."""
    client, native, model = gateway.client, gateway.native, gateway.native.config.id
    async with _held_online_request(gateway, "capacity-online") as (online, online_gate):
        assert INTERACTIVE_LOAD.in_flight(model) == 1
        rounds = _observe_top_up(monkeypatch, gateway.workers, model)
        created = await client.post(
            "/v1/batches",
            headers=_v("2023-06-01"),
            json={"requests": [_messages_request("capacity", model, "capacity-batch")]},
        )
        assert created.status_code == 200, created.text
        batch_id = created.json()["id"]
        # This unique model has no other batches. A completed top-up round
        # therefore proves the real scheduler saw this item and left it queued.
        await asyncio.wait_for(rounds.get(), timeout=10)
        assert "capacity-batch" not in _native_versions(native)
        results = (await client.get(f"/v1/batches/{batch_id}/results", headers=KEY)).json()
        assert results["data"][0]["status"] == "queued"

        online_gate.set()
        released = await asyncio.wait_for(asyncio.shield(online), timeout=5)
        assert released.status_code == 200, released.text
        assert INTERACTIVE_LOAD.in_flight(model) == 0
        final = await _status(
            client, batch_id, "completed", "the batch after capacity was released"
        )
        assert final["request_counts"] == {"total": 1, "completed": 1, "failed": 0}
        assert _native_versions(native)["capacity-batch"] == "2023-06-01"
        results = (await client.get(f"/v1/batches/{batch_id}/results", headers=KEY)).json()
        result = results["data"][0]
        assert result["response"]["body"]["type"] == "message"
        assert result["response"]["body"]["content"][0]["text"] == "echo capacity-batch"


async def test_an_inline_messages_batch_runs_on_v1_batches_under_its_version(gateway):
    client, native = gateway.client, gateway.native
    model = native.config.id
    created = await client.post(
        "/v1/batches",
        headers=_v("2026-10-21"),
        json={
            "requests": [
                _messages_request("ok-1", model, "one"),
                _messages_request("ok-2", model, "two"),
            ],
        },
    )
    assert created.status_code == 200, created.text
    batch = created.json()
    assert batch["object"] == "batch" and batch["endpoint"] == "/v1/messages"
    assert batch["id"].startswith("batch_") and batch["anthropic_version"] == "2026-10-21"

    final = await _status(client, batch["id"], "completed", "the messages batch to complete")
    assert final["request_counts"] == {"total": 2, "completed": 2, "failed": 0}
    assert final["usage"] == {"input_tokens": 200, "output_tokens": 100, "total_tokens": 300}
    assert final["cost_usd"] == pytest.approx(0.0004)
    # Each request reached the native route as the Messages body it was, under
    # the batch's version -- not the adapter's default, not a chat translation.
    assert sorted(version for version, _body in native.calls) == ["2026-10-21"] * 2
    assert sorted(body["messages"][0]["content"] for _v, body in native.calls) == ["one", "two"]
    assert all(body["max_tokens"] == 32 for _v, body in native.calls)

    page = (await client.get(f"/v1/batches/{batch['id']}/results", headers=KEY)).json()
    assert [row["custom_id"] for row in page["data"]] == ["ok-1", "ok-2"]
    assert page["data"][0]["response"]["body"]["type"] == "message"
    export = await client.get(f"/v1/batches/{batch['id']}/output.jsonl", headers=KEY)
    lines = [json.loads(line) for line in export.text.splitlines()]
    assert [set(line) for line in lines] == [{"id", "custom_id", "response", "error"}] * 2
    assert lines[1]["response"]["body"]["content"][0]["text"] == "echo two"

    deleted = await client.delete(f"/v1/batches/{batch['id']}", headers=KEY)
    assert deleted.json() == {"id": batch["id"], "object": "batch.deleted", "deleted": True}
    assert (await client.get(f"/v1/batches/{batch['id']}", headers=KEY)).status_code == 404


async def test_messages_batches_need_a_known_version_and_refuse_what_they_cannot_run(gateway):
    client, native = gateway.client, gateway.native
    model = native.config.id
    one = [_messages_request("a", model, "x")]
    refused = [
        await client.post("/v1/batches", headers=KEY, json={"requests": one}),
        await client.post("/v1/batches", headers=_v("2099-01-01"), json={"requests": one}),
        await client.post(
            "/v1/batches",
            headers=_v("2023-06-01", **{"anthropic-beta": "x"}),
            json={"requests": one},
        ),
        # The version's own rules, for every request.
        await client.post(
            "/v1/batches",
            headers=_v("2026-10-21"),
            json={
                "requests": [
                    _messages_request("fine", model, "x"),
                    _messages_request("temp", model, "x", temperature=0.2),
                ],
            },
        ),
        # A server tool: refused, never silently dropped.
        await client.post(
            "/v1/batches",
            headers=_v("2023-06-01"),
            json={
                "requests": [
                    _messages_request(
                        "tool", model, "x", tools=[{"type": "web_search_20250305", "name": "w"}]
                    )
                ],
            },
        ),
        # A malformed content block refuses the whole batch, naming the request.
        await client.post(
            "/v1/batches",
            headers=_v("2023-06-01"),
            json={
                "requests": [
                    _messages_request("good", model, "x"),
                    {
                        **_messages_request("bad", model, "x"),
                        "body": {
                            "model": model,
                            "max_tokens": 8,
                            "messages": [{"role": "user", "content": [{"type": []}]}],
                        },
                    },
                ],
            },
        ),
        # The obsolete body field is refused, even beside a valid header.
        await client.post(
            "/v1/batches",
            headers=_v("2023-06-01"),
            json={"requests": one, "anthropic_version": "2023-06-01"},
        ),
    ]
    assert [response.status_code for response in refused] == [400] * len(refused)
    assert "temp" in refused[3].json()["error"]["message"]
    assert "temperature" in refused[3].json()["error"]["message"]
    assert "'bad'" in refused[5].json()["error"]["message"]
    assert "not accepted in the body" in refused[6].json()["error"]["message"]
    assert native.calls == []
    assert await gateway.store.list_batches(user_id=gateway.owner["user_id"], limit=5) == []


async def test_a_messages_file_batch_is_validated_whole_and_run_under_its_version(gateway):
    client, native = gateway.client, gateway.native
    model = native.config.id
    rows = [_messages_request(f"line-{i}", model, f"file {i}") for i in range(3)]
    uploaded = await _upload(client, ("\n".join(json.dumps(row) for row in rows) + "\n").encode())
    created = await client.post(
        "/v1/batches",
        headers=_v("2023-06-01"),
        json={
            "input_file_id": uploaded["id"],
            "endpoint": "/v1/messages",
            "completion_window": "24h",
        },
    )
    assert created.status_code == 200, created.text
    assert created.json()["status"] == "validating"
    final = await _status(client, created.json()["id"], "completed", "the messages file batch")
    assert final["anthropic_version"] == "2023-06-01"
    assert final["request_counts"] == {"total": 3, "completed": 3, "failed": 0}
    assert final["output_file_id"] and final["error_file_id"] is None
    assert {version for version, _body in native.calls} == {"2023-06-01"}
    meta = (await client.get(f"/v1/files/{final['output_file_id']}", headers=KEY)).json()
    content = await client.get(f"/v1/files/{final['output_file_id']}/content", headers=KEY)
    assert len(content.content) == meta["bytes"]
    assert hashlib.sha256(content.content).hexdigest() == content.headers["etag"].strip('"')
    outputs = [json.loads(line) for line in content.text.splitlines()]
    assert [line["custom_id"] for line in outputs] == ["line-0", "line-1", "line-2"]
    assert all(line["response"]["body"]["type"] == "message" for line in outputs)


async def test_a_messages_file_with_a_line_its_version_refuses_runs_nothing(gateway):
    client, native = gateway.client, gateway.native
    model = native.config.id
    rows = [
        _messages_request("ok", model, "x"),
        _messages_request("ok-2", model, "y"),
        _messages_request("temp", model, "z", temperature=0.3),
    ]
    uploaded = await _upload(client, ("\n".join(json.dumps(row) for row in rows) + "\n").encode())
    created = await client.post(
        "/v1/batches",
        headers=_v("2026-10-21"),
        json={
            "input_file_id": uploaded["id"],
            "endpoint": "/v1/messages",
            "completion_window": "24h",
        },
    )
    assert created.status_code == 200, created.text
    final = await _status(client, created.json()["id"], "failed", "the invalid messages file")
    (problem,) = final["errors"]["data"]
    assert problem["line"] == 3 and "temperature" in problem["message"]
    assert final["request_counts"]["total"] == 0
    assert native.calls == []


async def test_a_messages_batch_is_cancelled_and_owned_like_any_batch(gateway):
    client, native = gateway.client, gateway.native
    model = native.config.id
    # Paused: nothing of this model is claimed, so its requests stay queued.
    await gateway.store.set_model_limit(model, 0, "test")
    created = await client.post(
        "/v1/batches",
        headers=_v("2023-06-01"),
        json={"requests": [_messages_request(f"q-{i}", model, "x") for i in range(3)]},
    )
    batch_id = created.json()["id"]
    for method, path in (
        ("GET", f"/v1/batches/{batch_id}"),
        ("POST", f"/v1/batches/{batch_id}/cancel"),
        ("DELETE", f"/v1/batches/{batch_id}"),
        ("GET", f"/v1/batches/{batch_id}/results"),
        ("GET", f"/v1/batches/{batch_id}/output.jsonl"),
    ):
        response = await client.request(method, path, headers=STRANGER)
        assert response.status_code == 404, (method, path)
    assert (await client.get("/v1/batches", headers=STRANGER)).json()["data"] == []

    cancelled = (await client.post(f"/v1/batches/{batch_id}/cancel", headers=KEY)).json()
    assert cancelled["status"] == "cancelled" and cancelled["cancelled_at"]
    assert cancelled["request_counts"] == {"total": 3, "completed": 0, "failed": 0}
    lines = (await client.get(f"/v1/batches/{batch_id}/output.jsonl", headers=KEY)).text
    assert {json.loads(line)["error"]["code"] for line in lines.splitlines()} == {"batch_cancelled"}
    assert native.calls == []


async def test_an_idempotency_key_compares_the_version_too(gateway, settings):
    client, native = gateway.client, gateway.native
    await gateway.store.set_model_limit(native.config.id, 0, "test")
    body = {"requests": [_messages_request("only", native.config.id, "x")]}
    key = {"Idempotency-Key": "messages-version"}
    first = await client.post("/v1/batches", headers=_v("2026-10-21", **key), json=body)
    replay = await client.post("/v1/batches", headers=_v(" 2026-10-21 ", **key), json=body)
    other_version = await client.post("/v1/batches", headers=_v("2023-06-01", **key), json=body)
    assert first.status_code == replay.status_code == 200, (first.text, replay.text)
    assert replay.json()["id"] == first.json()["id"]
    assert other_version.status_code == 409
    # A replay needs the version too: without it the request is not the same one.
    unversioned = await client.post("/v1/batches", headers={**KEY, **key}, json=body)
    assert unversioned.status_code == 400 and "needs this header" in unversioned.text

    # Once the operator stops allowing the version, the earlier create still
    # replays, but nothing new can be created under it.
    settings["BATCH_ANTHROPIC_VERSIONS"] = "2023-06-01"
    still = await client.post("/v1/batches", headers=_v("2026-10-21", **key), json=body)
    fresh = await client.post(
        "/v1/batches", headers=_v("2026-10-21", **{"Idempotency-Key": "new-key"}), json=body
    )
    assert (still.status_code, still.json()["id"]) == (200, first.json()["id"])
    assert fresh.status_code == 400 and "use one of 2023-06-01" in fresh.text
    unknown = await client.post("/v1/batches", headers=_v("2099-01-01", **key), json=body)
    assert unknown.status_code == 400


async def _legacy_message_batch(gateway, *, model: str, texts: list[str]) -> str:
    """A batch as the retired Message Batches API stored it (its items, as it admitted them)."""
    batch_id = f"msgbatch_{uuid.uuid4().hex}"
    await gateway.store.create_batch(
        batch_id=batch_id,
        user_id=gateway.owner["user_id"],
        key_hash=gateway.owner["key_hash"],
        endpoint="/v1/messages",
        model_ids=[model],
        items=[
            {
                "custom_id": f"legacy-{i}",
                "model_id": model,
                "request": {
                    "model": model,
                    "max_tokens": 8,
                    "messages": [{"role": "user", "content": text}],
                },
            }
            for i, text in enumerate(texts)
        ],
        metadata=None,
        completion_window_seconds=24 * 3600,
        api_surface="anthropic",
        target="messages",
        protocol_profile="2026-10-21",
        retention_seconds=7 * 86400,
    )
    return batch_id


async def test_a_legacy_message_batch_drains_and_is_read_through_v1_batches(gateway):
    client, native = gateway.client, gateway.native
    model = native.config.id
    batch_id = await _legacy_message_batch(gateway, model=model, texts=["old one", "old two"])

    final = await _status(client, batch_id, "completed", "the legacy message batch")
    assert final["object"] == "batch" and final["endpoint"] == "/v1/messages"
    assert final["anthropic_version"] == "2026-10-21"
    assert final["request_counts"] == {"total": 2, "completed": 2, "failed": 0}
    assert {version for version, _body in native.calls} == {"2026-10-21"}
    listed = (await client.get("/v1/batches", headers=KEY)).json()["data"]
    assert batch_id in [row["id"] for row in listed]
    lines = (await client.get(f"/v1/batches/{batch_id}/output.jsonl", headers=KEY)).text
    first = json.loads(lines.splitlines()[0])
    assert first["custom_id"] == "legacy-0" and first["response"]["body"]["type"] == "message"
    for method, path in (
        ("GET", f"/v1/batches/{batch_id}"),
        ("DELETE", f"/v1/batches/{batch_id}"),
        ("GET", f"/v1/batches/{batch_id}/results"),
    ):
        assert (await client.request(method, path, headers=STRANGER)).status_code == 404

    console = {"Authorization": "Bearer console"}
    rows = (await client.get("/user/batches", headers=console)).json()["data"]
    row = next(row for row in rows if row["id"] == batch_id)
    assert (row["status"], row["input"], row["target"]) == ("completed", "inline", "messages")
    assert row["request_counts"] == {"total": 2, "completed": 2, "failed": 0}
    download = await client.get(f"/user/batches/{batch_id}/results", headers=console)
    assert download.text == lines

    admin = (await client.get("/admin/batch/jobs?status=completed", headers=KEY)).json()["data"]
    assert batch_id in [row["id"] for row in admin]
    assert (await client.get("/admin/batch/jobs?status=ended", headers=KEY)).status_code == 422
    deleted = await client.delete(f"/v1/batches/{batch_id}", headers=KEY)
    assert deleted.json()["deleted"] is True


async def test_a_queued_legacy_message_batch_is_cancelled_through_v1_batches(gateway):
    client, native = gateway.client, gateway.native
    await gateway.store.set_model_limit(native.config.id, 0, "test")
    batch_id = await _legacy_message_batch(gateway, model=native.config.id, texts=["a", "b"])

    cancelled = (await client.post(f"/v1/batches/{batch_id}/cancel", headers=KEY)).json()
    assert cancelled["status"] == "cancelled"
    console = {"Authorization": "Bearer console"}
    rows = (await client.get("/user/batches", headers=console)).json()["data"]
    assert next(row for row in rows if row["id"] == batch_id)["status"] == "cancelled"
    assert native.calls == []


def _multipart(
    payload: bytes, *, purpose: str = "batch", filename: str = "in.jsonl"
) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    body = (
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="purpose"\r\n\r\n{purpose}\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            "Content-Type: application/jsonl\r\n\r\n"
        ).encode()
        + payload
        + f"\r\n--{boundary}--\r\n".encode()
    )
    return body, f"multipart/form-data; boundary={boundary}"


async def _upload(client: AsyncClient, payload: bytes) -> dict[str, Any]:
    body, content_type = _multipart(payload)
    response = await client.post(
        "/v1/files", headers={**KEY, "content-type": content_type}, content=body
    )
    assert response.status_code == 200, response.text
    return response.json()


def _lines(model: str, n: int, *, bad_last: bool = False) -> bytes:
    rows = [
        json.dumps(
            {
                "custom_id": f"line-{i}",
                "method": "POST",
                "url": "/v1/chat/completions",
                "body": {"model": model, "messages": [{"role": "user", "content": f"file {i}"}]},
            }
        )
        for i in range(n)
    ]
    if bad_last:
        rows[-1] = rows[-1][:-1]
    return ("\n".join(rows) + "\n").encode()


async def test_a_file_batch_is_validated_run_sealed_and_downloaded_as_its_manifest_says(gateway):
    client, model = gateway.client, gateway.model
    uploaded = await _upload(client, _lines(model.config.id, 5))
    assert uploaded["purpose"] == "batch" and uploaded["bytes"] > 0
    created = await client.post(
        "/v1/batches",
        headers=KEY,
        json={
            "input_file_id": uploaded["id"],
            "endpoint": "/v1/chat/completions",
            "completion_window": "24h",
        },
    )
    assert created.status_code == 200, created.text
    assert created.json()["status"] == "validating"
    batch_id = created.json()["id"]

    async def completed():
        body = (await client.get(f"/v1/batches/{batch_id}", headers=KEY)).json()
        return body if body["status"] == "completed" else None

    final = await _until(completed, what="the file batch to complete")
    assert final["request_counts"] == {"total": 5, "completed": 5, "failed": 0}
    assert final["output_file_id"] and final["error_file_id"] is None
    assert final["usage"] == {"input_tokens": 500, "output_tokens": 250, "total_tokens": 750}
    meta = (await client.get(f"/v1/files/{final['output_file_id']}", headers=KEY)).json()
    first = await client.get(f"/v1/files/{final['output_file_id']}/content", headers=KEY)
    second = await client.get(f"/v1/files/{final['output_file_id']}/content", headers=KEY)
    assert first.content == second.content
    assert len(first.content) == meta["bytes"] == int(first.headers["content-length"])
    assert first.headers["etag"].strip('"') == hashlib.sha256(first.content).hexdigest()
    outputs = [json.loads(line) for line in first.text.splitlines()]
    assert [line["custom_id"] for line in outputs] == [f"line-{i}" for i in range(5)]
    assert all(line["response"]["status_code"] == 200 for line in outputs)
    # Deleting the input changes nothing for the batch; deleting the output is for good.
    assert (await client.delete(f"/v1/files/{uploaded['id']}", headers=KEY)).status_code == 200
    assert (
        await client.delete(f"/v1/files/{final['output_file_id']}", headers=KEY)
    ).status_code == 200
    assert (
        await client.get(f"/v1/files/{final['output_file_id']}/content", headers=KEY)
    ).status_code == 404
    again = (await client.get(f"/v1/batches/{batch_id}", headers=KEY)).json()
    assert again["output_file_id"] == final["output_file_id"]


async def test_a_file_with_a_bad_last_line_runs_nothing(gateway):
    client, model = gateway.client, gateway.model
    uploaded = await _upload(client, _lines(model.config.id, 4, bad_last=True))
    created = await client.post(
        "/v1/batches",
        headers=KEY,
        json={
            "input_file_id": uploaded["id"],
            "endpoint": "/v1/chat/completions",
            "completion_window": "24h",
        },
    )
    batch_id = created.json()["id"]

    async def failed():
        body = (await client.get(f"/v1/batches/{batch_id}", headers=KEY)).json()
        return body if body["status"] == "failed" else None

    final = await _until(failed, what="the invalid file batch to fail")
    assert final["errors"]["data"][0]["line"] == 4
    assert final["request_counts"]["total"] == 0
    assert final["output_file_id"] is None and final["error_file_id"] is None
    assert model.calls == []


async def test_an_idempotent_replay_comes_before_quota_and_role(gateway):
    client, model = gateway.client, gateway.model
    body = {
        "requests": [
            {
                "custom_id": "one",
                "url": "/v1/chat/completions",
                "body": {
                    "model": model.config.id,
                    "messages": [{"role": "user", "content": "idem"}],
                },
            }
        ]
    }
    headers = {**KEY, "Idempotency-Key": "same-request"}
    first = await client.post("/v1/batches", headers=headers, json=body)
    assert first.status_code == 200, first.text
    gateway.caller.update(quota_exceeded=True, role="free")
    replay = await client.post("/v1/batches", headers=headers, json=body)
    assert (replay.status_code, replay.json()["id"]) == (200, first.json()["id"])
    fresh = await client.post("/v1/batches", headers={**KEY, "Idempotency-Key": "other"}, json=body)
    assert fresh.status_code == 403
    gateway.caller.update(role="internal")
    fresh = await client.post("/v1/batches", headers={**KEY, "Idempotency-Key": "other"}, json=body)
    assert fresh.status_code == 429
    gateway.caller.update(quota_exceeded=False)
    changed = {**body, "metadata": {"x": "y"}}
    assert (await client.post("/v1/batches", headers=headers, json=changed)).status_code == 409
    # Management keeps working with the quota spent.
    gateway.caller.update(quota_exceeded=True)
    assert (await client.get("/v1/batches", headers=KEY)).status_code == 200
    gateway.caller.update(quota_exceeded=False)

    async def finished():
        row = (await client.get(f"/v1/batches/{first.json()['id']}", headers=KEY)).json()
        return row["status"] == "completed"

    await _until(finished, what="the inline batch to complete")
    assert (
        await client.delete(f"/v1/batches/{first.json()['id']}", headers=KEY)
    ).status_code == 200
    assert (await client.post("/v1/batches", headers=headers, json=body)).status_code == 410
    assert model.calls.count("idem") == 1


async def test_a_webhook_arrives_signed_after_the_results_are_readable(gateway):
    client = gateway.client
    received: list[tuple[dict[str, str], bytes, bool]] = []

    async def receive(request: web.Request) -> web.Response:
        payload = await request.read()
        event = json.loads(payload)
        batch = await gateway.store.get_batch(event["data"]["id"])
        received.append(
            (dict(request.headers), payload, batch is not None and batch["phase"] == "sealed")
        )
        return web.Response(status=204)

    app = web.Application()
    app.router.add_post("/hook", receive)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    try:
        saved = await client.put(
            "/v1/batch_webhook",
            headers=KEY,
            json={"url": f"http://127.0.0.1:{port}/hook"},
        )
        assert saved.status_code == 200, saved.text
        secret = saved.json()["secret"]
        assert (await client.get("/v1/batch_webhook", headers=KEY)).json().get("secret") is None
        created = await client.post(
            "/v1/batches",
            headers=_v("2023-06-01"),
            json={"requests": [_messages_request("w", gateway.native.config.id, "hook")]},
        )
        batch_id = created.json()["id"]

        async def delivered():
            return received or None

        await _until(delivered, what="the webhook delivery")
        headers, payload, sealed_when_sent = received[0]
        event = json.loads(payload)
        # A Messages batch created through /v1/batches ends as every new batch does.
        assert event["type"] == "batch.completed" and event["data"] == {"id": batch_id}
        assert sealed_when_sent, "the event must not arrive before the results are readable"
        lowered = {k.lower(): v for k, v in headers.items()}
        assert webhooks.verify(secret, lowered, payload)

        # A batch the retired Message Batches API stored, drained by this
        # version, still ends with the event its owner subscribed to.
        legacy_id = await _legacy_message_batch(
            gateway, model=gateway.native.config.id, texts=["hook legacy"]
        )

        async def delivered_twice():
            return received if len(received) >= 2 else None

        await _until(delivered_twice, what="the legacy batch's webhook delivery")
        headers, payload, sealed_when_sent = received[1]
        event = json.loads(payload)
        assert event["type"] == "message_batch.ended" and event["data"] == {"id": legacy_id}
        assert sealed_when_sent
        assert webhooks.verify(secret, {k.lower(): v for k, v in headers.items()}, payload)
        events = (await client.get("/v1/batch_webhook/events", headers=KEY)).json()["data"]
        assert {(e["batch_id"], e["type"], e["status"]) for e in events} == {
            (batch_id, "batch.completed", "delivered"),
            (legacy_id, "message_batch.ended", "delivered"),
        }
    finally:
        await runner.cleanup()


async def test_the_console_lists_and_downloads_a_users_batches(gateway):
    client, model = gateway.client, gateway.model
    created = await client.post(
        "/v1/batches",
        headers=_v("2023-06-01"),
        json={"requests": [_messages_request("c", model.config.id, "console")]},
    )
    batch_id = created.json()["id"]
    console = {"Authorization": "Bearer console"}

    async def listed():
        rows = (await client.get("/user/batches", headers=console)).json()["data"]
        row = next((r for r in rows if r["id"] == batch_id), None)
        return row if row and row["status"] == "completed" else None

    row = await _until(listed, what="the console to show the completed batch")
    assert (row["target"], row["input"], row["api_surface"]) == (
        "messages",
        "inline",
        "openai_inline",
    )
    assert row["request_counts"] == {"total": 1, "completed": 1, "failed": 0}
    assert row["usage"]["total_tokens"] == 150
    assert row["cost_usd"] == pytest.approx(0.0002)
    download = await client.get(f"/user/batches/{batch_id}/results", headers=console)
    assert json.loads(download.text.splitlines()[0])["custom_id"] == "c"


@pytest.mark.parametrize(
    "url", ["https://[broken/h", "https://example.com:bad/hook", "https://example.com:99999/h"]
)
async def test_a_malformed_webhook_url_is_a_400_not_a_server_error(gateway, url):
    response = await gateway.client.put("/v1/batch_webhook", headers=KEY, json={"url": url})
    assert response.status_code == 400, response.text
    assert (await gateway.client.get("/v1/batch_webhook", headers=KEY)).json() == {
        "configured": False
    }


async def test_an_invalid_idna_webhook_host_is_a_400_without_any_lookup(gateway, monkeypatch):
    loop = asyncio.get_running_loop()
    real_getaddrinfo = loop.getaddrinfo
    host = "a" * 64 + ".example.com"
    webhook_lookups: list[Any] = []

    async def guarded_lookup(name: Any, *args: Any, **kwargs: Any) -> Any:
        # Only the webhook's own host is watched: the database pool (whose
        # host may be a name, as in CI) and the background workers keep
        # resolving normally.
        text = name.decode() if isinstance(name, bytes) else str(name)
        if text.rstrip(".").lower() == host:
            webhook_lookups.append(text)
            raise AssertionError("a malformed host must not reach DNS")
        return await real_getaddrinfo(name, *args, **kwargs)

    with monkeypatch.context() as patched:
        patched.setattr(loop, "getaddrinfo", guarded_lookup)
        response = await gateway.client.put(
            "/v1/batch_webhook", headers=KEY, json={"url": f"https://{host}/hook"}
        )
        assert response.status_code == 400, response.text
        assert "malformed" in response.text
        assert webhook_lookups == []
    # Restored before reading the state back through the database.
    assert loop.getaddrinfo == real_getaddrinfo
    assert (await gateway.client.get("/v1/batch_webhook", headers=KEY)).json() == {
        "configured": False
    }


async def test_a_file_responses_batch_with_a_malformed_line_runs_nothing(gateway):
    client, model = gateway.client, gateway.model
    rows = [
        {
            "custom_id": "ok",
            "method": "POST",
            "url": "/v1/responses",
            "body": {"model": model.config.id, "input": "hello"},
        },
        {
            "custom_id": "bad",
            "method": "POST",
            "url": "/v1/responses",
            "body": {"model": model.config.id, "input": [{"type": {}}]},
        },
    ]
    uploaded = await _upload(client, ("\n".join(json.dumps(row) for row in rows) + "\n").encode())
    created = await client.post(
        "/v1/batches",
        headers=KEY,
        json={
            "input_file_id": uploaded["id"],
            "endpoint": "/v1/responses",
            "completion_window": "24h",
        },
    )
    assert created.status_code == 200, created.text
    batch_id = created.json()["id"]

    async def failed():
        body = (await client.get(f"/v1/batches/{batch_id}", headers=KEY)).json()
        return body if body["status"] == "failed" else None

    final = await _until(failed, what="the malformed Responses file batch to fail")
    (problem,) = final["errors"]["data"]
    assert problem["line"] == 2 and "input item type" in problem["message"]
    assert final["request_counts"]["total"] == 0
    assert model.calls == []


async def test_batch_enabled_alone_switches_every_surface(gateway, settings):
    """Off, every batch surface is 404; on, all of them work with nothing else to turn on.

    ``settings`` keeps the retired BATCH_EXPANSION_ENABLED at "0" throughout.
    """
    client, model = gateway.client, gateway.model
    payload = _lines(model.config.id, 1)
    messages = {"requests": [_messages_request("one", model.config.id, "x")]}
    body, content_type = _multipart(payload)
    hook = {"url": "http://127.0.0.1:9/hook"}

    settings["BATCH_ENABLED"] = "0"
    off = [
        await client.post("/v1/files", headers={**KEY, "content-type": content_type}, content=body),
        await client.get("/v1/files", headers=KEY),
        await client.post(
            "/v1/batches",
            headers=KEY,
            json={
                "input_file_id": "file-x",
                "endpoint": "/v1/chat/completions",
                "completion_window": "24h",
            },
        ),
        await client.post("/v1/batches", headers=_v("2023-06-01"), json=messages),
        await client.put("/v1/batch_webhook", headers=KEY, json=hook),
        # The retired routes are behind the same switch.
        await client.post("/v1/messages/batches", headers=KEY, json=messages),
        await client.get("/anthropic/v1/messages/batches/msgbatch_x", headers=KEY),
    ]
    assert [response.status_code for response in off] == [404] * len(off)

    del settings["BATCH_ENABLED"]
    uploaded = await _upload(client, payload)
    created = await client.post(
        "/v1/batches",
        headers=KEY,
        json={
            "input_file_id": uploaded["id"],
            "endpoint": "/v1/chat/completions",
            "completion_window": "24h",
        },
    )
    messages_batch = await client.post("/v1/batches", headers=_v("2023-06-01"), json=messages)
    retired = await client.post("/v1/messages/batches", headers=KEY, json=messages)
    assert retired.status_code == 410
    assert retired.json()["error"]["type"] == "invalid_request_error"
    saved = await client.put("/v1/batch_webhook", headers=KEY, json=hook)
    assert (created.status_code, messages_batch.status_code, saved.status_code) == (
        200,
        200,
        200,
    ), (
        created.text,
        messages_batch.text,
        saved.text,
    )
