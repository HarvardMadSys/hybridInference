"""The batch API surfaces end to end over HTTP, against Postgres and real workers.

The routers, the store, two workers (two gateway processes), the handlers, cost
accounting and the owner's re-authentication from real ``users``/``api_keys``
rows are the production code; only the upstream model is a stub, and the
caller's management identity is resolved from a test key table (the
key-to-account checks themselves are covered by the auth tests). What this
holds: an Anthropic Message Batch ends with every request accounted for and its
results downloadable; a file batch is validated, run, sealed and downloaded
byte-for-byte as its manifest says, and an invalid file runs nothing;
idempotent creates replay before the quota and role checks; protocol families
do not see each other's batches; and a webhook arrives, signed, only after the
results are readable.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
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
from serving.adapters.base import BaseAdapter, ModelConfig, UsageInfo
from serving.batch import config as batch_config_module, webhooks
from serving.batch.model_limits import BatchModelLimits
from serving.batch.scheduler import BatchScheduler, SchedulerTimings
from serving.config.settings import get_settings
from serving.servers import auth as auth_module
from serving.servers.auth import decrypt_api_key, verify_api_key, verify_batch_management_identity
from serving.servers.concurrency import UserConcurrencyLimiter, static_limits_provider
from serving.servers.deps import AppServices, get_current_user
from serving.servers.middleware.error import install_error_handlers
from serving.servers.routers import (
    anthropic_messages,
    batch_console,
    batches,
    files,
    message_batches,
)
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
def settings(monkeypatch) -> dict[str, str]:
    values = {
        "BATCH_EXPANSION_ENABLED": "1",
        "BATCH_WEBHOOK_ALLOW_PRIVATE_TARGETS": "1",
        "BATCH_SHARED_INTERACTIVE_LOAD": "0",
    }
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
    router = FixedRouter()
    router.register_route(model.config.id, [(model, 1.0)])
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

    async def identity(
        authorization: str | None = Header(None), x_api_key: str | None = Header(None)
    ):
        key = (authorization or "").removeprefix("Bearer ") or x_api_key
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
    install_error_handlers(app)
    for exc_type in (StarletteHTTPException, FastAPIHTTPException):
        app.add_exception_handler(
            exc_type, anthropic_messages.anthropic_aware_http_exception_handler
        )
    for module in (batches, message_batches, files, batch_console):
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
                {"client": client, "model": model, "store": store, "caller": caller, "pool": pool},
            )
        finally:
            for worker in workers:
                await worker.stop()


KEY = {"Authorization": "Bearer test-key"}
ANTHROPIC = {**KEY, "anthropic-version": "2023-06-01"}


async def _until(check, *, timeout: float = 20.0, what: str = "condition"):
    deadline = time.monotonic() + timeout
    while True:
        value = await check()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.05)


async def test_an_anthropic_message_batch_ends_with_every_request_accounted_for(gateway):
    client, model = gateway.client, gateway.model
    params = {"model": model.config.id, "max_tokens": 32}
    created = await client.post(
        "/v1/messages/batches",
        headers=ANTHROPIC,
        json={
            "requests": [
                {
                    "custom_id": "ok-1",
                    "params": {**params, "messages": [{"role": "user", "content": "one"}]},
                },
                {
                    "custom_id": "ok-2",
                    "params": {**params, "messages": [{"role": "user", "content": "two"}]},
                },
                {
                    "custom_id": "hidden",
                    "params": {
                        **params,
                        "model": "no-such-model",
                        "messages": [{"role": "user", "content": "x"}],
                    },
                },
            ]
        },
    )
    assert created.status_code == 200, created.text
    batch = created.json()
    assert batch["type"] == "message_batch" and batch["processing_status"] == "in_progress"
    assert batch["results_url"] is None

    async def ended():
        response = await client.get(f"/v1/messages/batches/{batch['id']}", headers=ANTHROPIC)
        body = response.json()
        return body if body["processing_status"] == "ended" else None

    final = await _until(ended, what="the message batch to end")
    assert final["request_counts"] == {
        "processing": 0,
        "succeeded": 2,
        "errored": 1,
        "canceled": 0,
        "expired": 0,
    }
    assert final["results_url"].endswith(f"/v1/messages/batches/{batch['id']}/results")
    results = await client.get(f"/v1/messages/batches/{batch['id']}/results", headers=ANTHROPIC)
    lines = [json.loads(line) for line in results.text.splitlines()]
    by_id = {line["custom_id"]: line["result"] for line in lines}
    assert by_id["ok-1"]["type"] == "succeeded"
    assert by_id["ok-1"]["message"]["type"] == "message"
    assert by_id["hidden"]["error"]["error"]["type"] == "not_found_error"
    assert sorted(model.calls) == ["one", "two"]
    # Another protocol family does not see it; the delete needs no running request.
    assert (await client.get(f"/v1/batches/{batch['id']}", headers=KEY)).status_code == 404
    deleted = await client.delete(f"/v1/messages/batches/{batch['id']}", headers=ANTHROPIC)
    assert deleted.json() == {"id": batch["id"], "type": "message_batch_deleted"}
    missing = await client.get(f"/v1/messages/batches/{batch['id']}", headers=ANTHROPIC)
    assert missing.status_code == 404
    assert missing.json()["error"]["type"] == "not_found_error"


async def test_message_batches_need_a_known_version_and_no_beta(gateway):
    client = gateway.client
    assert (await client.get("/v1/messages/batches", headers=KEY)).status_code == 400
    bad = await client.get(
        "/v1/messages/batches", headers={**KEY, "anthropic-version": "2099-01-01"}
    )
    assert bad.status_code == 400
    beta = await client.get("/v1/messages/batches", headers={**ANTHROPIC, "anthropic-beta": "x"})
    assert beta.status_code == 400
    assert beta.json()["type"] == "error"


async def test_2026_version_errors_items_its_version_refuses(gateway):
    client, model = gateway.client, gateway.model
    created = await client.post(
        "/v1/messages/batches",
        headers={**KEY, "anthropic-version": "2026-10-21"},
        json={
            "requests": [
                {
                    "custom_id": "temp",
                    "params": {
                        "model": model.config.id,
                        "max_tokens": 8,
                        "temperature": 0.2,
                        "messages": [{"role": "user", "content": "x"}],
                    },
                }
            ]
        },
    )
    assert created.json()["processing_status"] == "ended"
    lines = (
        await client.get(
            f"/v1/messages/batches/{created.json()['id']}/results",
            headers={**KEY, "anthropic-version": "2026-10-21"},
        )
    ).text.splitlines()
    error = json.loads(lines[0])["result"]["error"]["error"]
    assert error["type"] == "invalid_request_error"
    assert "temperature" in error["message"]
    assert model.calls == []


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
    client, model = gateway.client, gateway.model
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
            "/v1/messages/batches",
            headers=ANTHROPIC,
            json={
                "requests": [
                    {
                        "custom_id": "w",
                        "params": {
                            "model": model.config.id,
                            "max_tokens": 8,
                            "messages": [{"role": "user", "content": "hook"}],
                        },
                    }
                ]
            },
        )
        batch_id = created.json()["id"]

        async def delivered():
            return received or None

        await _until(delivered, what="the webhook delivery")
        headers, payload, sealed_when_sent = received[0]
        event = json.loads(payload)
        assert event["type"] == "message_batch.ended" and event["data"] == {"id": batch_id}
        assert sealed_when_sent, "the event must not arrive before the results are readable"
        lowered = {k.lower(): v for k, v in headers.items()}
        assert webhooks.verify(secret, lowered, payload)
        events = (await client.get("/v1/batch_webhook/events", headers=KEY)).json()["data"]
        assert events[0]["status"] == "delivered"
    finally:
        await runner.cleanup()


async def test_the_console_lists_and_downloads_a_users_batches(gateway):
    client, model = gateway.client, gateway.model
    created = await client.post(
        "/v1/messages/batches",
        headers=ANTHROPIC,
        json={
            "requests": [
                {
                    "custom_id": "c",
                    "params": {
                        "model": model.config.id,
                        "max_tokens": 8,
                        "messages": [{"role": "user", "content": "console"}],
                    },
                }
            ]
        },
    )
    batch_id = created.json()["id"]
    console = {"Authorization": "Bearer console"}

    async def listed():
        rows = (await client.get("/user/batches", headers=console)).json()["data"]
        row = next((r for r in rows if r["id"] == batch_id), None)
        return row if row and row["status"] == "ended" else None

    row = await _until(listed, what="the console to show the ended batch")
    assert row["api_surface"] == "anthropic"
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
