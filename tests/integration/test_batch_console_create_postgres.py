"""Creating batches in the console, end to end over HTTP, against Postgres and real workers.

Everything below the session identity is production code: the console and
API routers, the batch and file stores, the scanner, two ``BatchScheduler``
workers re-authenticating the owners' real ``api_keys`` rows, and the
handlers. The upstream models are stubs on this test's event loop; the
console session is a header naming a synthetic user whose role is read from
the database on every request, as ``get_current_user`` does.

What this holds (design ``2026-10-28-batch-ui-create-design.zh.md``, A1-A25,
and the review findings): a file is identified and preflighted read only; a
submission runs the existing pipeline under the account's active key, bound to
the confirmed credential and canonical model; what is stored differs from the
source only through a derived copy adopted in the create transaction; a
submission key replays before the caps and after its record expired; and
everything is the signed-in user's only, administrators included.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI, Header, HTTPException
from httpx import ASGITransport, AsyncClient

from routing.routers import FixedRouter
from serving.adapters.base import BaseAdapter, ModelConfig, UsageInfo
from serving.batch import config as batch_config_module
from serving.batch.model_limits import BatchModelLimits
from serving.batch.scheduler import BatchScheduler, SchedulerTimings
from serving.config.settings import get_settings
from serving.servers import auth as auth_module
from serving.servers.auth import decrypt_api_key, verify_api_key, verify_batch_management_identity
from serving.servers.concurrency import UserConcurrencyLimiter, static_limits_provider
from serving.servers.deps import AppServices, get_current_user
from serving.servers.middleware.error import install_error_handlers
from serving.servers.routers import batch_console, batch_console_create, batches, files
from serving.servers.routers.batch_console_create import credential_revision
from serving.storage.batch_file_store import BatchFileStore
from serving.storage.batch_store import BatchInputUnavailable, BatchStore
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
#: Never echoed by an error: every test prompt carries it.
SECRET = "do-not-echo-7f3a"


class _Chat(BaseAdapter):
    def __init__(
        self, model_id: str, *, max_output: int = 4096, params: list[str] | None = None
    ) -> None:
        super().__init__(
            ModelConfig(
                id=model_id,
                name=model_id,
                provider="openai",
                base_url=f"https://{model_id}.test",
                pricing=_PRICING,
                max_output_length=max_output,
                **({"supported_params": params} if params is not None else {}),
            )
        )
        self.calls: list[dict[str, Any]] = []
        self.gate: asyncio.Event | None = None

    async def chat_completion(self, messages: list[dict[str, Any]], **params: Any) -> dict:
        self.calls.append({"messages": messages, **params})
        if self.gate is not None:
            await self.gate.wait()
        await asyncio.sleep(0.002)
        return self.format_response(
            content="ok",
            model=self.config.id,
            usage=UsageInfo(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )

    async def stream_chat_completion(self, messages, **params):  # pragma: no cover
        raise AssertionError("batch items never stream")
        yield


class _Embed(BaseAdapter):
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
        self.calls: list[dict[str, Any]] = []

    async def chat_completion(self, messages, **params):  # pragma: no cover
        raise AssertionError("an embedding model is not chatted with")

    async def stream_chat_completion(self, messages, **params):  # pragma: no cover
        raise AssertionError("an embedding model is not chatted with")
        yield

    async def embeddings(self, input_data, **params: Any) -> dict[str, Any]:
        self.calls.append({"input": input_data, **params})
        inputs = input_data if isinstance(input_data, list) else [input_data]
        return {
            "object": "list",
            "data": [
                {"object": "embedding", "index": index, "embedding": [0.1, 0.2]}
                for index, _ in enumerate(inputs)
            ],
            "model": self.config.id,
            "usage": {"prompt_tokens": 3, "total_tokens": 3},
        }


@pytest_asyncio.fixture
async def pool():
    pool = await postgres.create_pool(min_size=2, max_size=16)
    await BatchStore(pool).initialize()
    try:
        yield pool
    finally:
        await pool.close()


async def _make_user(
    pool: Any, op_store: PostgresOperationalStore, label: str, *, role: str, key: bool = True
) -> dict[str, Any]:
    tag = uuid.uuid4().hex[:10]
    user_id = f"u-bcc-{label}-{tag}"
    await op_store.create_user(
        user_id=user_id, email=f"{label}-{tag}@bcc.test", password_hash="x", email_verified=True
    )
    await op_store.update_user_fields(user_id, role=role)
    key_hash = None
    if key:
        key_hash = f"hash-bcc-{tag}"
        await op_store.create_key(
            key_hash=key_hash,
            key_prefix=f"hyi-{tag[:8]}",
            user_id=user_id,
            account_id=user_id,
            quota_daily_cost_usd=Decimal("100.00"),
        )
    return {"user_id": user_id, "key_hash": key_hash, "role": role}


@pytest_asyncio.fixture
async def users(pool):
    op_store = PostgresOperationalStore(pool)
    made = {
        "alice": await _make_user(pool, op_store, "alice", role="internal"),
        "bob": await _make_user(pool, op_store, "bob", role="internal"),
        "admin": await _make_user(pool, op_store, "admin", role="admin"),
        "nokey": await _make_user(pool, op_store, "nokey", role="internal", key=False),
    }
    try:
        yield {**made, "op_store": op_store}
    finally:
        for user in made.values():
            await BatchStore(pool).delete_user_batches(user["user_id"])
            async with pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM user_daily_cost WHERE user_id = $1", user["user_id"]
                )
                await conn.execute("DELETE FROM api_keys WHERE user_id = $1", user["user_id"])
                await conn.execute("DELETE FROM users WHERE id = $1", user["user_id"])


@pytest.fixture
def settings(monkeypatch) -> dict[str, str]:
    values = {"BATCH_SHARED_INTERACTIVE_LOAD": "0"}
    monkeypatch.setattr(
        batch_config_module, "config_value", lambda key, default=None: values.get(key, default)
    )
    synthetic = get_settings().model_copy(update={"api_key_secret": "batch-console-test-secret"})
    monkeypatch.setattr(auth_module, "get_settings", lambda: synthetic)
    return values


@pytest_asyncio.fixture
async def gateway(pool, users, settings):
    suffix = uuid.uuid4().hex[:6]
    chat = _Chat(f"bcc-chat-{suffix}", max_output=2048)
    other = _Chat(f"bcc-other-{suffix}")
    # Declares no sampling parameter at all, and one declaring top_p only.
    bare = _Chat(f"bcc-bare-{suffix}", params=[])
    top_p_only = _Chat(f"bcc-topp-{suffix}", params=["top_p"])
    embed = _Embed(f"bcc-embed-{suffix}")
    router = FixedRouter()
    router.register_route(chat.config.id, [(chat, 1.0)], aliases=[f"bcc-alias-{suffix}"])
    router.register_route(other.config.id, [(other, 1.0)])
    router.register_route(bare.config.id, [(bare, 1.0)])
    router.register_route(top_p_only.config.id, [(top_p_only, 1.0)])
    router.register_embedding_route(embed.config.id, [(embed, 1.0)])
    store = BatchStore(pool)
    services = AppServices(
        router=router,
        operational_store=users["op_store"],
        user_concurrency_limiter=UserConcurrencyLimiter(
            static_limits_provider({"free": 3, "pro": 3, "internal": 50, "admin": 50})
        ),
        batch_store=store,
        batch_model_limits=BatchModelLimits(store, ttl=0.05),
        batch_file_store=BatchFileStore(pool),
        batch_webhook_store=BatchWebhookStore(pool),
        embedding_adapters={embed.config.id: embed},
    )
    app = FastAPI()
    app.state.services = services
    op_store = users["op_store"]

    async def session_user(authorization: str | None = Header(None)):
        # The session of a synthetic user, role read fresh as get_current_user does.
        if not authorization or not authorization.startswith("Bearer session-"):
            raise HTTPException(401, "Missing authentication token.")
        user_id = authorization.removeprefix("Bearer session-")
        row = await op_store.get_user_by_id(user_id)
        if row is None:
            raise HTTPException(401, "User not found.")
        role = row["role"] or "free"
        return {"user_id": user_id, "role": role, "is_admin": role == "admin", "status": "active"}

    async def api_identity(authorization: str | None = Header(None)):
        key = (authorization or "").removeprefix("Bearer ")
        for user in (users["alice"], users["bob"]):
            if key == f"api-{user['user_id']}":
                return {
                    "user_id": user["user_id"],
                    "role": user["role"],
                    "authenticated": True,
                    "is_admin": False,
                    "auth_key_hash": user["key_hash"],
                    "quota_exceeded": False,
                    "quota_daily_cost_usd": 100.0,
                    "spent_today_usd": 0.0,
                }
        raise HTTPException(401, "Invalid or expired API key")

    app.dependency_overrides[get_current_user] = session_user
    app.dependency_overrides[verify_batch_management_identity] = api_identity
    app.dependency_overrides[verify_api_key] = api_identity
    install_error_handlers(app)
    for module in (batches, files, batch_console, batch_console_create):
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
                    "other": other,
                    "bare": bare,
                    "top_p_only": top_p_only,
                    "embed": embed,
                    "alias": f"bcc-alias-{suffix}",
                    "store": store,
                    "files": services.batch_file_store,
                    "pool": pool,
                    "users": users,
                    "settings": settings,
                },
            )
        finally:
            for worker in workers:
                await worker.stop()


def session(user: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer session-{user['user_id']}"}


async def until(check, *, timeout: float = 20.0, what: str = "condition"):
    deadline = time.monotonic() + timeout
    while True:
        value = await check()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.05)


def jsonl(rows: list[Any]) -> bytes:
    return (
        "\n".join(row if isinstance(row, str) else json.dumps(row) for row in rows) + "\n"
    ).encode()


def chat_row(custom_id: str, model: str, *, url: bool = True, **body: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "custom_id": custom_id,
        "body": {
            "model": model,
            "messages": [{"role": "user", "content": f"{SECRET} {custom_id}"}],
            **body,
        },
    }
    if url:
        row = {"custom_id": custom_id, "method": "POST", "url": "/v1/chat/completions", **row}
    return row


def embed_row(custom_id: str, model: str, *, url: bool = True, **body: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "custom_id": custom_id,
        "body": {"model": model, "input": f"{SECRET} {custom_id}", **body},
    }
    if url:
        row["url"] = "/v1/embeddings"
    return row


def multipart(payload: bytes, *, filename: str = "requests.jsonl") -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    body = (
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="purpose"\r\n\r\nbatch\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            "Content-Type: application/jsonl\r\n\r\n"
        ).encode()
        + payload
        + f"\r\n--{boundary}--\r\n".encode()
    )
    return body, f"multipart/form-data; boundary={boundary}"


async def upload(gw, user, payload: bytes, *, filename: str = "requests.jsonl") -> dict[str, Any]:
    body, content_type = multipart(payload, filename=filename)
    response = await gw.client.post(
        "/user/batch-files", headers={**session(user), "content-type": content_type}, content=body
    )
    assert response.status_code == 200, response.text
    return response.json()


async def preflight(gw, user, file_id: str, *, phase: str = "validate", **extra: Any):
    return await gw.client.post(
        "/user/batch-preflight",
        headers=session(user),
        json={"source_file_id": file_id, "phase": phase, **extra},
    )


async def context(gw, user) -> dict[str, Any]:
    response = await gw.client.get("/user/batch-create/context", headers=session(user))
    assert response.status_code == 200, response.text
    return response.json()


async def submission(gw, user, source: dict[str, Any], report: dict[str, Any], **extra: Any):
    """The body a confirmed submission sends, from a preflight report and the context."""
    ctx = await context(gw, user)
    credential = ctx["credential"] or {"key_id": "0", "revision": "none"}
    body = {
        "source_file_id": source["id"],
        "source_sha256": source["sha256"],
        "endpoint": report["detected"]["endpoint"],
        "overrides": {},
        "confirmed_model": report["model_resolution"],
        "credential": {"key_id": credential["key_id"], "revision": credential["revision"]},
        "name": "eval run",
        "description": "",
        "copied_from": None,
    }
    body.update(extra)
    return body


def code_of(response) -> str | None:
    return (response.json().get("error") or {}).get("code")


async def submit(gw, user, body: dict[str, Any], *, key: str | None = None):
    return await gw.client.post(
        "/user/batches",
        headers={**session(user), "Idempotency-Key": key or f"console-{uuid.uuid4()}"},
        json=body,
    )


async def finished(gw, user, batch_id: str, wanted: str = "completed") -> dict[str, Any]:
    async def reached():
        response = await gw.client.get(f"/user/batches/{batch_id}", headers=session(user))
        assert response.status_code == 200, response.text
        data = response.json()
        return data if data["status"] == wanted and data["results_available"] else None

    return await until(reached, what=f"{batch_id} {wanted}")


async def file_row(gw, file_id: str) -> dict[str, Any] | None:
    async with gw.pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM batch_files WHERE id = $1", file_id)
    return dict(row) if row is not None else None


async def derived_of(gw, file_id: str) -> list[dict[str, Any]]:
    async with gw.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM batch_files WHERE derived_from_file_id = $1 ORDER BY created_at",
            file_id,
        )
    return [dict(row) for row in rows]


async def jobs_of(gw, user) -> list[dict[str, Any]]:
    async with gw.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, input_file_id, idempotency_key FROM batch_jobs WHERE user_id = $1",
            user["user_id"],
        )
    return [dict(row) for row in rows]


# -- A1, A24: identify, preflight, submit as is ----------------------------------------


async def test_a_chat_file_is_identified_checked_and_run_from_its_own_bytes(gateway):
    gw, alice = gateway, gateway.users["alice"]
    source = await upload(
        gw, alice, jsonl([chat_row(f"q-{i}", gw.chat.config.id) for i in range(4)])
    )

    identified = await preflight(gw, alice, source["id"], phase="identify")
    assert identified.status_code == 200, identified.text
    detected = identified.json()["detected"]
    assert detected["type"] == "chat" and detected["source"] == "url"
    assert detected["needs_type_choice"] is False
    assert detected["models"] == [
        {"name": gw.chat.config.id, "rows": 4, "available": True, "canonical": gw.chat.config.id}
    ]
    # Identification checks no model and stores nothing.
    assert identified.json()["model_resolution"] is None

    report = (await preflight(gw, alice, source["id"])).json()
    assert report["valid"] is True, report["errors"]
    assert report["request_count"] == 4
    assert report["stores_copy"] is False
    assert report["model_resolution"] == {
        "requested": gw.chat.config.id,
        "canonical": gw.chat.config.id,
    }
    assert len(report["preview"]) == 3
    assert report["preview"][0]["request"]["url"] == "/v1/chat/completions"
    assert gw.chat.calls == []  # a preflight never calls a model
    assert await jobs_of(gw, alice) == []

    created = await submit(gw, alice, await submission(gw, alice, source, report, name="eval-0420"))
    assert created.status_code == 200, created.text
    batch = created.json()
    # No override and the canonical id already in the file: the batch reads the source itself.
    assert batch["input_file_id"] == source["id"]
    assert batch["created_via"] == "console" and batch["name"] == "eval-0420"
    assert batch["creation"]["source_file_id"] == source["id"]
    assert batch["creation"]["stores_copy"] is False
    assert batch["copy"] == {"state": "ready", "reason": None}
    assert await derived_of(gw, source["id"]) == []

    done = await finished(gw, alice, batch["id"])
    assert done["request_counts"] == {"total": 4, "completed": 4, "failed": 0}
    assert len(gw.chat.calls) == 4

    # The list finds it by name and by id, and by the status it shows.
    for query in ({"q": "eval-04"}, {"q": batch["id"]}, {"status": "completed"}):
        listed = await gw.client.get("/user/batches", headers=session(alice), params=query)
        assert [row["id"] for row in listed.json()["data"]] == [batch["id"]], query
    listed = await gw.client.get(
        "/user/batches", headers=session(alice), params={"q": "nothing-like-it"}
    )
    assert listed.json()["data"] == []
    listed = await gw.client.get("/user/batches", headers=session(alice), params={"q": "%"})
    assert listed.json()["data"] == []  # wildcards are taken literally

    stored = await gw.client.get(f"/user/batches/{batch['id']}/requests", headers=session(alice))
    assert stored.status_code == 200
    lines = [json.loads(line) for line in stored.text.splitlines()]
    assert [line["custom_id"] for line in lines] == [f"q-{i}" for i in range(4)]
    assert lines[0]["url"] == "/v1/chat/completions" and lines[0]["method"] == "POST"


async def test_a_file_without_urls_is_identified_from_its_shape(gateway):
    gw, alice = gateway, gateway.users["alice"]
    source = await upload(
        gw, alice, jsonl([embed_row(f"d-{i}", gw.embed.config.id, url=False) for i in range(3)])
    )
    report = (await preflight(gw, alice, source["id"])).json()
    assert report["detected"]["type"] == "embeddings"
    assert report["detected"]["source"] == "structure"
    assert report["detected"]["url_missing_rows"] == 3
    assert report["valid"] is True, report["errors"]
    # The url is filled in, so what is stored differs from the source.
    assert report["stores_copy"] is True
    assert report["preview"][0]["filled"][:1] == ["url"]
    assert report["params"] == {}  # no text parameters for Embeddings


# -- A2, A4: copy with overrides ---------------------------------------------------------


async def test_a_copy_overrides_every_row_through_an_adopted_stored_copy(gateway):
    gw, alice = gateway, gateway.users["alice"]
    rows = [
        chat_row("t-0", gw.chat.config.id, temperature=0),
        chat_row("t-1", gw.chat.config.id, temperature=0.7, max_tokens=64),
        chat_row("t-2", gw.chat.config.id, top_p=0.5),
    ]
    source = await upload(gw, alice, jsonl(rows))
    kept = (await preflight(gw, alice, source["id"])).json()
    assert kept["params"]["temperature"] == {"values": {"0": 1, "0.7": 1}, "other": 0, "unset": 1}
    assert kept["other_params"] == {"top_p": 1}
    first = await submit(gw, alice, await submission(gw, alice, source, kept))
    original = first.json()
    await finished(gw, alice, original["id"])
    expires_before = (await file_row(gw, source["id"]))["expires_at"]

    detail = (await gw.client.get(f"/user/batches/{original['id']}", headers=session(alice))).json()
    assert detail["copy"]["state"] == "ready"
    overrides = {"model": gw.other.config.id, "temperature": 0.2}
    report = (await preflight(gw, alice, source["id"], overrides=overrides)).json()
    assert report["valid"] is True, report["errors"]
    assert report["stores_copy"] is True
    assert all(entry["request"]["body"]["temperature"] == 0.2 for entry in report["preview"])
    assert all(
        entry["request"]["body"]["model"] == gw.other.config.id for entry in report["preview"]
    )
    calls_before = len(gw.other.calls)
    assert calls_before == 0  # neither the copy nor the preflight ran anything

    copy = await submit(
        gw,
        alice,
        await submission(
            gw,
            alice,
            source,
            report,
            overrides=overrides,
            copied_from=original["id"],
            name="eval-0420-copy",
        ),
    )
    assert copy.status_code == 200, copy.text
    batch = copy.json()
    assert batch["creation"]["copied_from"] == original["id"]
    assert batch["creation"]["overrides"] == overrides
    derived = await derived_of(gw, source["id"])
    assert [row["id"] for row in derived] == [batch["input_file_id"]]
    assert derived[0]["adopted_by_batch_id"] == batch["id"]
    assert derived[0]["status"] == "ready"
    assert derived[0]["expires_at"] <= expires_before
    # The source's own retention is untouched by a copy.
    assert (await file_row(gw, source["id"]))["expires_at"] == expires_before
    # A derived copy is not an upload against the hourly rate.
    assert (await context(gw, alice))["usage"]["uploads_last_hour"] == 1

    await finished(gw, alice, batch["id"])
    sent = gw.other.calls
    assert len(sent) == 3
    assert all(call.get("temperature") == 0.2 for call in sent)
    assert any(call.get("max_tokens") == 64 for call in sent)  # kept where not overridden
    assert any(call.get("top_p") == 0.5 for call in sent)


async def test_embeddings_keep_their_dimensions_and_take_no_text_parameters(gateway):
    gw, alice = gateway, gateway.users["alice"]
    source = await upload(
        gw,
        alice,
        jsonl(
            [
                embed_row("e-0", gw.embed.config.id, dimensions=8),
                embed_row("e-1", gw.embed.config.id),
            ]
        ),
    )
    refused = (await preflight(gw, alice, source["id"], overrides={"temperature": 0})).json()
    assert refused["valid"] is False
    assert refused["errors"][0]["field"] == "overrides.temperature"
    report = (await preflight(gw, alice, source["id"])).json()
    assert report["valid"] is True and report["stores_copy"] is False
    batch = (await submit(gw, alice, await submission(gw, alice, source, report))).json()
    await finished(gw, alice, batch["id"])
    assert sorted(call.get("dimensions") or 0 for call in gw.embed.calls) == [0, 8]


# -- A6, A9: strict checks, with useful errors and no prompt echoed ----------------------


async def test_every_problem_is_reported_by_line_and_nothing_is_created(gateway):
    gw, alice = gateway, gateway.users["alice"]
    model = gw.chat.config.id
    good = json.dumps(chat_row("ok-1", model))
    rows = [
        "﻿" + good,  # 1: byte-order mark
        "",  # 2: empty line
        "{not json " + SECRET,  # 3
        json.dumps(chat_row("dup", model)),  # 4
        json.dumps(chat_row("dup", model)),  # 5: duplicate custom_id
        json.dumps({"custom_id": "nomsg", "url": "/v1/chat/completions", "body": {"model": model}}),
        json.dumps(chat_row("stream", model, stream=True)),  # 7
        f'{{"custom_id":"twice","url":"/v1/chat/completions","body":{{"model":"{model}",'
        f'"model":"{model}","messages":[]}}}}',  # 8: repeated key
        json.dumps(chat_row("mct", model, max_completion_tokens=64)),  # 9
        json.dumps(chat_row("user", model, user="u-1")),  # 10
        json.dumps(
            {
                "custom_id": "audio",
                "url": "/v1/chat/completions",
                "body": {
                    "model": model,
                    "messages": [{"role": "user", "content": SECRET, "audio": {"id": "a"}}],
                },
            }
        ),  # 11
        f'{{"custom_id":"nan","url":"/v1/chat/completions","body":{{"model":"{model}",'
        '"messages":[{"role":"user","content":"x"}],"temperature":NaN}}',  # 12
        json.dumps(chat_row("coerce", model, max_tokens="64")),  # 13: no coercion
        json.dumps({**chat_row("method", model), "method": "GET"}),  # 14
        json.dumps({**chat_row("extra", model), "metadata": {"a": 1}}),  # 15
        json.dumps(
            chat_row(
                "free-form",
                model,
                tools=[{"type": "function", "function": {"name": "f", "anything": {"x": 1}}}],
                response_format={"type": "json_schema", "json_schema": {"any": {"key": 1}}},
            )
        ),  # 16: free-form JSON is not looked into
    ]
    source = await upload(gw, alice, jsonl(rows))
    report = (await preflight(gw, alice, source["id"])).json()
    assert report["valid"] is False
    by_line = {}
    for error in report["errors"]:
        by_line.setdefault(error["line"], []).append(error)
    codes = {line: [e["code"] for e in errors] for line, errors in by_line.items()}
    assert codes[1] == ["utf8_bom"]
    assert codes[2] == ["empty_line"]
    assert codes[3] == ["invalid_json_line"]
    assert codes[5] == ["duplicate_custom_id"] and "line 4" in by_line[5][0]["message"]
    assert codes[6] == ["invalid_request"] and by_line[6][0]["field"] == "body.messages"
    assert by_line[6][0]["custom_id"] == "nomsg"
    assert codes[7] == ["invalid_request"] and by_line[7][0]["field"] == "body.stream"
    assert codes[8] == ["duplicate_key"]
    assert codes[9] == ["unsupported_parameter"]
    assert by_line[9][0]["field"] == "body.max_completion_tokens"
    assert "max_tokens" in by_line[9][0]["message"]
    assert by_line[10][0]["field"] == "body.user"
    assert by_line[11][0]["field"] == "body.messages[0].audio"
    assert codes[12] == ["invalid_json_line"]
    assert codes[13] == ["invalid_request"] and by_line[13][0]["field"] == "body.max_tokens"
    assert codes[14] == ["invalid_method"]
    assert codes[15] == ["unsupported_parameter"] and by_line[15][0]["field"] == "metadata"
    assert 16 not in by_line and 4 not in by_line
    assert SECRET not in json.dumps(report["errors"])

    refused = await submit(
        gw,
        alice,
        await submission(
            gw,
            alice,
            source,
            report,
            endpoint="/v1/chat/completions",
            confirmed_model={"requested": model, "canonical": model},
        ),
    )
    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "invalid_file"
    assert await jobs_of(gw, alice) == []
    assert await derived_of(gw, source["id"]) == []


# -- A7, A25: one type per file; a choice only fills in what is missing ------------------


async def test_types_are_never_mixed_and_a_choice_never_overrides_the_file(gateway):
    gw, alice = gateway, gateway.users["alice"]
    chat, embed = gw.chat.config.id, gw.embed.config.id

    mixed = await upload(
        gw, alice, jsonl([chat_row("c-1", chat), chat_row("c-2", chat), embed_row("e-1", embed)])
    )
    report = (await preflight(gw, alice, mixed["id"], phase="identify")).json()
    assert report["detected"]["type"] is None and report["detected"]["needs_type_choice"] is False
    assert [(e["line"], e["code"]) for e in report["errors"]] == [(3, "mismatched_endpoint")]
    # A choice cannot make a mixed file pass.
    chosen = (await preflight(gw, alice, mixed["id"], endpoint="/v1/chat/completions")).json()
    assert chosen["valid"] is False and chosen["detected"]["type"] is None

    conflict = await upload(
        gw, alice, jsonl([{**embed_row("x-1", embed), "url": "/v1/chat/completions"}])
    )
    report = (await preflight(gw, alice, conflict["id"])).json()
    assert [(e["code"], e["field"]) for e in report["errors"]] == [("mismatched_endpoint", "url")]

    unsupported = await upload(
        gw,
        alice,
        jsonl(
            [{"custom_id": "r-1", "url": "/v1/responses", "body": {"model": chat, "input": "x"}}]
        ),
    )
    report = (await preflight(gw, alice, unsupported["id"])).json()
    assert report["errors"][0]["code"] == "unsupported_endpoint"
    assert "/v1/batches" in report["errors"][0]["message"]

    # A row whose shape names no type, in a file that otherwise does: validated as that type.
    partial = await upload(
        gw, alice, jsonl([chat_row("p-1", chat), {"custom_id": "p-2", "body": {"model": chat}}])
    )
    report = (await preflight(gw, alice, partial["id"])).json()
    assert report["detected"]["type"] == "chat"
    assert [(e["line"], e["field"]) for e in report["errors"]] == [(2, "body.messages")]

    # Nothing in the file says what it is: only then is a choice asked for.
    ambiguous = await upload(
        gw, alice, jsonl([{"custom_id": "a-1", "body": {"model": embed, "text": "x"}}])
    )
    report = (await preflight(gw, alice, ambiguous["id"], phase="identify")).json()
    assert report["detected"]["needs_type_choice"] is True and report["errors"] == []
    # The choice fills in the type; the row is still checked in full against it.
    chosen = (await preflight(gw, alice, ambiguous["id"], endpoint="/v1/embeddings")).json()
    assert chosen["detected"]["type"] == "embeddings" and chosen["detected"]["source"] == "choice"
    # The stray field, and the field the chosen type needs.
    assert {e["field"] for e in chosen["errors"]} == {"body.text", "body.input"}
    assert chosen["valid"] is False
    # A choice that contradicts what the file states is refused.
    contradicted = (await preflight(gw, alice, partial["id"], endpoint="/v1/embeddings")).json()
    assert any(e["field"] == "endpoint" for e in contradicted["errors"])


# -- A8, A10, A11 and the account's own model access -------------------------------------


async def test_an_unavailable_or_mixed_model_needs_one_explicit_model(gateway):
    gw, alice = gateway, gateway.users["alice"]
    rows = [
        chat_row("m-1", "retired-model"),
        chat_row("m-2", gw.chat.config.id),
        chat_row("m-3", gw.other.config.id),
    ]
    source = await upload(gw, alice, jsonl(rows))
    identified = (await preflight(gw, alice, source["id"], phase="identify")).json()
    assert identified["errors"] == []  # step 1 only marks the model
    available = {m["name"]: m["available"] for m in identified["detected"]["models"]}
    assert available == {"retired-model": False, gw.chat.config.id: True, gw.other.config.id: True}
    kept = (await preflight(gw, alice, source["id"])).json()
    assert {e["code"] for e in kept["errors"]} == {"model_not_found", "mismatched_model"}
    fixed = (
        await preflight(gw, alice, source["id"], overrides={"model": gw.chat.config.id})
    ).json()
    assert fixed["valid"] is True, fixed["errors"]


async def test_aliases_are_pinned_and_a_changed_resolution_is_refused(gateway):
    gw, alice = gateway, gateway.users["alice"]
    source = await upload(
        gw, alice, jsonl([chat_row("a-1", gw.alias), chat_row("a-2", gw.chat.config.id)])
    )
    report = (await preflight(gw, alice, source["id"])).json()
    # Two names for one canonical model are one model.
    assert report["valid"] is True, report["errors"]
    assert report["model_resolution"]["canonical"] == gw.chat.config.id
    assert report["stores_copy"] is True  # the alias is pinned in what is stored
    assert "body.model" in report["preview"][0]["filled"]

    # What the confirmation showed no longer holds (the alias now names another model).
    stale = await submission(
        gw,
        alice,
        source,
        report,
        confirmed_model={"requested": gw.chat.config.id, "canonical": gw.other.config.id},
    )
    refused = await submit(gw, alice, stale)
    assert refused.status_code == 409 and code_of(refused) == "model_resolution_changed"
    assert await jobs_of(gw, alice) == [] and await derived_of(gw, source["id"]) == []

    created = await submit(gw, alice, await submission(gw, alice, source, report))
    batch = created.json()
    async with gw.pool.acquire() as conn:
        first = await conn.fetchval(
            "SELECT convert_from(data, 'UTF8') FROM batch_file_chunks "
            "WHERE file_id = $1 AND seq = 0",
            batch["input_file_id"],
        )
    assert all(
        json.loads(line)["body"]["model"] == gw.chat.config.id for line in first.splitlines()
    )


async def test_a_disabled_or_hidden_model_is_refused_like_an_unknown_one(gateway):
    gw, alice = gateway, gateway.users["alice"]
    disabled = gw.other.config.id
    await gw.users["op_store"].set_user_preference(alice["user_id"], "disabled_models", [disabled])
    from_file = await upload(gw, alice, jsonl([chat_row("d-1", disabled)]))
    unknown = await upload(gw, alice, jsonl([chat_row("d-1", "no-such-model")]))

    refused = (await preflight(gw, alice, from_file["id"])).json()
    never = (await preflight(gw, alice, unknown["id"])).json()
    assert [e["code"] for e in refused["errors"]] == ["model_not_found"]
    # The same answer as for a model that does not exist: nothing hidden leaks.
    assert refused["errors"][0]["message"] == never["errors"][0]["message"].replace(
        "no-such-model", disabled
    )
    identified = (await preflight(gw, alice, from_file["id"], phase="identify")).json()
    assert identified["detected"]["models"][0]["available"] is False

    allowed = await upload(gw, alice, jsonl([chat_row("d-2", gw.chat.config.id)]))
    override = (await preflight(gw, alice, allowed["id"], overrides={"model": disabled})).json()
    assert [(e["code"], e["field"]) for e in override["errors"]] == [
        ("model_not_found", "overrides.model")
    ]
    catalog = (await context(gw, alice))["models"]["chat"]
    assert disabled not in {m["id"] for m in catalog}
    assert gw.chat.config.id in {m["id"] for m in catalog}

    # Another account's preferences are its own.
    bob = gw.users["bob"]
    bob_file = await upload(gw, bob, jsonl([chat_row("d-3", disabled)]))
    assert (await preflight(gw, bob, bob_file["id"])).json()["valid"] is True


async def test_access_lost_after_the_preflight_refuses_the_submission(gateway):
    gw, alice = gateway, gateway.users["alice"]
    source = await upload(gw, alice, jsonl([chat_row("r-1", gw.other.config.id)]))
    report = (await preflight(gw, alice, source["id"])).json()
    assert report["valid"] is True
    await gw.users["op_store"].set_user_preference(
        alice["user_id"], "disabled_models", [gw.other.config.id]
    )
    refused = await submit(gw, alice, await submission(gw, alice, source, report))
    assert refused.status_code == 422 and code_of(refused) == "invalid_file"
    assert await jobs_of(gw, alice) == []


async def test_max_tokens_and_temperature_overrides_follow_the_model_limits(gateway):
    gw, alice = gateway, gateway.users["alice"]
    source = await upload(gw, alice, jsonl([chat_row("l-1", gw.chat.config.id)]))
    over = (await preflight(gw, alice, source["id"], overrides={"max_tokens": 4096})).json()
    assert [(e["field"]) for e in over["errors"]] == ["overrides.max_tokens"]
    bad = (await preflight(gw, alice, source["id"], overrides={"temperature": 3})).json()
    assert [(e["field"]) for e in bad["errors"]] == ["overrides.temperature"]
    good = (
        await preflight(gw, alice, source["id"], overrides={"max_tokens": 2048, "temperature": 0})
    ).json()
    assert good["valid"] is True, good["errors"]


# -- A12, A13: the run credential, quota and role ----------------------------------------


async def test_the_run_credential_is_the_confirmed_active_key(gateway):
    gw, alice, nokey = gateway, gateway.users["alice"], gateway.users["nokey"]

    # No key: upload and preflight work; submitting is refused, and no key is created.
    source = await upload(gw, nokey, jsonl([chat_row("k-1", gw.chat.config.id)]))
    report = (await preflight(gw, nokey, source["id"])).json()
    assert report["valid"] is True
    ctx = await context(gw, nokey)
    assert ctx["credential"] is None
    refused = await submit(gw, nokey, await submission(gw, nokey, source, report))
    assert refused.status_code == 409 and code_of(refused) == "no_worker_credential"
    async with gw.pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM api_keys WHERE user_id = $1", nokey["user_id"]
            )
            == 0
        )

    # The context shows the key's prefix and an opaque revision, never the key or its hash.
    ctx = await context(gw, alice)
    assert set(ctx["credential"]) == {"key_id", "key_prefix", "revision"}
    assert alice["key_hash"] not in json.dumps(ctx)
    source = await upload(gw, alice, jsonl([chat_row("k-2", gw.chat.config.id)]))
    report = (await preflight(gw, alice, source["id"])).json()
    confirmed = await submission(gw, alice, source, report)

    # Regenerated in place since: same row, same id, a new hash.
    new_hash = f"{alice['key_hash']}-regenerated"
    await gw.users["op_store"].regenerate_key(
        alice["user_id"], new_key_hash=new_hash, new_key_prefix=ctx["credential"]["key_prefix"]
    )
    assert (await context(gw, alice))["credential"]["key_id"] == ctx["credential"]["key_id"]
    refused = await submit(gw, alice, confirmed)
    assert refused.status_code == 409 and code_of(refused) == "credential_changed"
    assert await jobs_of(gw, alice) == []

    created = await submit(gw, alice, await submission(gw, alice, source, report))
    assert created.status_code == 200, created.text
    async with gw.pool.acquire() as conn:
        stored = await conn.fetchval(
            "SELECT key_hash FROM batch_jobs WHERE id = $1", created.json()["id"]
        )
    assert stored == new_hash  # never NULL: the batch runs under the active key
    assert (
        credential_revision(ctx["credential"]["key_id"], new_hash) != ctx["credential"]["revision"]
    )


async def test_a_revoked_key_fails_an_unfinished_batch(gateway):
    gw, alice = gateway, gateway.users["alice"]
    gw.chat.gate = asyncio.Event()
    # One request at a time, so the rest are claimed after the key is revoked.
    await gw.store.set_model_limit(gw.chat.config.id, 1, updated_by="test")
    source = await upload(
        gw, alice, jsonl([chat_row(f"v-{i}", gw.chat.config.id) for i in range(6)])
    )
    report = (await preflight(gw, alice, source["id"])).json()
    batch = (await submit(gw, alice, await submission(gw, alice, source, report))).json()
    await until(lambda: asyncio.sleep(0, result=len(gw.chat.calls) > 0), what="a running request")
    await gw.users["op_store"].revoke_key(alice["user_id"])
    gw.chat.gate.set()
    try:
        done = await finished(gw, alice, batch["id"], wanted="failed")
    finally:
        await gw.store.delete_model_limit(gw.chat.config.id)
    assert done["error"]["code"] == "owner_unauthorized"
    assert done["request_counts"]["completed"] >= 1


async def test_zero_quota_still_uploads_and_checks_but_does_not_submit(gateway):
    gw, alice = gateway, gateway.users["alice"]
    async with gw.pool.acquire() as conn:
        await conn.execute(
            "UPDATE api_keys SET quota_daily_cost_usd = 0 WHERE user_id = $1", alice["user_id"]
        )
    source = await upload(gw, alice, jsonl([chat_row("z-1", gw.chat.config.id)]))
    report = (await preflight(gw, alice, source["id"])).json()
    assert report["valid"] is True
    refused = await submit(gw, alice, await submission(gw, alice, source, report))
    assert refused.status_code == 429 and code_of(refused) == "quota_exceeded"
    assert await jobs_of(gw, alice) == []


async def test_a_downgraded_role_keeps_managing_but_cannot_create(gateway):
    gw, alice = gateway, gateway.users["alice"]
    source = await upload(gw, alice, jsonl([chat_row("g-1", gw.chat.config.id)]))
    report = (await preflight(gw, alice, source["id"])).json()
    batch = (await submit(gw, alice, await submission(gw, alice, source, report))).json()
    await finished(gw, alice, batch["id"])
    await gw.users["op_store"].update_user_fields(alice["user_id"], role="free")

    assert (await preflight(gw, alice, source["id"])).status_code == 403
    assert (await submit(gw, alice, await submission(gw, alice, source, report))).status_code == 403
    body, content_type = multipart(jsonl([chat_row("g-2", gw.chat.config.id)]))
    upload_refused = await gw.client.post(
        "/user/batch-files", headers={**session(alice), "content-type": content_type}, content=body
    )
    assert upload_refused.status_code == 403
    assert (await gw.client.get("/user/batches", headers=session(alice))).json()["data"]
    assert (await gw.client.get("/user/batch-files", headers=session(alice))).status_code == 200
    downloaded = await gw.client.get(f"/user/batches/{batch['id']}/results", headers=session(alice))
    assert downloaded.status_code == 200
    assert (await context(gw, alice))["role_may_submit"] is False


# -- A14, findings 4 and 6: the source, its derived copy, and deletion -------------------


async def _derived_for(gw, user, source_id: str, payload: bytes) -> dict[str, Any]:
    """A provisional stored copy of *source_id*, as a submission writes one."""
    file_id = await gw.files.create_derived(
        user_id=user["user_id"],
        source_file_id=source_id,
        filename="x.stored.jsonl",
        max_total_bytes=1 << 30,
    )
    await gw.files.append_chunk(
        file_id=file_id, user_id=user["user_id"], seq=0, data=payload, max_total_bytes=1 << 30
    )
    import hashlib

    return {
        "file_id": file_id,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "line_count": payload.count(b"\n"),
    }


def _create_args(user, source, derived, *, key: str | None = None) -> dict[str, Any]:
    return {
        "batch_id": f"batch_{uuid.uuid4().hex}",
        "user_id": user["user_id"],
        "key_hash": user["key_hash"],
        "endpoint": "/v1/chat/completions",
        "target": "chat",
        "source_file_id": source["id"],
        "source_sha256": source["sha256"],
        "derived": derived,
        "file_retention_seconds": 30 * 86400,
        "completion_window_seconds": 86400,
        "retention_seconds": 7 * 86400,
        "idempotency": {"key": key or f"console-{uuid.uuid4()}", "digest": "d"},
        "max_active": 10,
        "max_queued": 100000,
        "console": {"display_name": "n", "console_spec": {"request_digest": "d"}},
    }


async def test_a_source_deleted_first_refuses_the_create_and_leaves_nothing_usable(gateway):
    gw, alice = gateway, gateway.users["alice"]
    payload = jsonl([chat_row("s-1", gw.chat.config.id)])
    source = await upload(gw, alice, payload)
    derived = await _derived_for(gw, alice, source["id"], payload)

    deleted = await gw.client.delete(f"/user/batch-files/{source['id']}", headers=session(alice))
    assert deleted.status_code == 200
    with pytest.raises(BatchInputUnavailable):
        await gw.store.create_console_batch(**_create_args(alice, source, derived))
    row = await file_row(gw, derived["file_id"])
    # Never made ready, hidden with its source, and gone at the next collection.
    assert row["status"] == "uploading" and row["adopted_by_batch_id"] is None
    assert row["deleted_at"] is not None
    assert await jobs_of(gw, alice) == []
    await gw.files.abort_upload(derived["file_id"])
    assert await file_row(gw, derived["file_id"]) is None

    # The same through the route: the deleted source is no longer a source.
    report_source = await upload(gw, alice, payload)
    report = (await preflight(gw, alice, report_source["id"], overrides={"temperature": 0})).json()
    await gw.client.delete(f"/user/batch-files/{report_source['id']}", headers=session(alice))
    refused = await submit(
        gw,
        alice,
        await submission(gw, alice, report_source, report, overrides={"temperature": 0}),
    )
    assert refused.status_code == 409 and code_of(refused) == "source_unavailable"
    assert all(row["status"] != "ready" for row in await derived_of(gw, report_source["id"]))


async def test_the_create_lock_holds_off_a_deletion_that_key_share_would_not(gateway):
    gw, alice = gateway, gateway.users["alice"]
    source = await upload(gw, alice, jsonl([chat_row("l-1", gw.chat.config.id)]))

    async def deletion_waits_on(lock_sql: str) -> bool:
        async with gw.pool.acquire() as conn:
            tx = conn.transaction()
            await tx.start()
            await conn.execute(lock_sql, source["id"])
            delete = asyncio.create_task(
                gw.files.delete_file(source["id"], user_id=alice["user_id"])
            )
            await asyncio.sleep(0.3)
            blocked = not delete.done()
            await tx.rollback()
            await delete
            return blocked

    # The pin a public create takes does not conflict with a deletion's update ...
    assert not await deletion_waits_on("SELECT id FROM batch_files WHERE id = $1 FOR KEY SHARE")
    # ... the console's source lock does, so a deletion and an adoption are ordered.
    source = await upload(gw, alice, jsonl([chat_row("l-2", gw.chat.config.id)]))
    assert await deletion_waits_on("SELECT id FROM batch_files WHERE id = $1 FOR UPDATE")


async def test_a_batch_created_first_keeps_running_when_its_source_is_deleted(gateway):
    gw, alice = gateway, gateway.users["alice"]
    rows = [chat_row(f"c-{i}", gw.chat.config.id) for i in range(5)]
    source = await upload(gw, alice, jsonl(rows))
    report = (await preflight(gw, alice, source["id"], overrides={"temperature": 0.1})).json()
    batch = (
        await submit(
            gw, alice, await submission(gw, alice, source, report, overrides={"temperature": 0.1})
        )
    ).json()
    deleted = await gw.client.delete(f"/user/batch-files/{source['id']}", headers=session(alice))
    assert deleted.status_code == 200
    copy = await file_row(gw, batch["input_file_id"])
    assert copy["deleted_at"] is not None  # hidden with its source: never reused ...
    done = await finished(gw, alice, batch["id"])  # ... but the batch it belongs to runs
    assert done["request_counts"]["completed"] == 5
    detail = (await gw.client.get(f"/user/batches/{batch['id']}", headers=session(alice))).json()
    assert detail["source"]["state"] in ("deleted", "missing")
    assert detail["copy"] == {"state": "reupload", "reason": "source_unavailable"}


async def test_validating_batches_pin_their_input_through_deletion_expiry_and_collection(
    pool, users
):
    store, files_ = BatchStore(pool), BatchFileStore(pool)
    alice = users["alice"]
    payload = jsonl([chat_row("p-1", "any-model")])
    source_id = await files_.create_upload(
        user_id=alice["user_id"],
        filename="s.jsonl",
        purpose="batch",
        max_total_bytes=1 << 30,
        max_uploads_per_hour=100,
    )
    await files_.append_chunk(
        file_id=source_id, user_id=alice["user_id"], seq=0, data=payload, max_total_bytes=1 << 30
    )
    import hashlib

    source = await files_.finish_upload(
        file_id=source_id,
        user_id=alice["user_id"],
        sha256=hashlib.sha256(payload).hexdigest(),
        line_count=1,
        retention_seconds=86400,
        check_owner=True,
    )
    gw = type("G", (), {"files": files_, "pool": pool})
    derived = await _derived_for(gw, alice, source_id, payload)
    batch = await store.create_console_batch(**_create_args(alice, source, derived))
    assert batch["input_file_id"] == derived["file_id"] and batch["phase"] == "validating"

    async def chunks(file_id: str) -> int:
        async with pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT count(*) FROM batch_file_chunks WHERE file_id = $1", file_id
            )

    # Manual deletion of the source hides the copy too; expiry passes as well.
    assert await files_.delete_file(source_id, user_id=alice["user_id"])
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE batch_files SET expires_at = now() - interval '1 second' WHERE id = $1",
            derived["file_id"],
        )
    await files_.collect_garbage()
    # The source had no pin and is collected; the validating batch's input is not.
    assert await chunks(source_id) == 0
    assert await chunks(derived["file_id"]) == 1
    # The public API never starts a batch from a derived copy, even an available one.
    assert (await files_.get_file(derived["file_id"], include_deleted=True))["derived_from_file_id"]

    # Deleting the batch releases it: the next collection removes the bytes.
    assert await store.delete_batch(batch["id"], user_id=alice["user_id"])
    await files_.collect_garbage()
    assert await chunks(derived["file_id"]) == 0


async def test_provisional_and_orphaned_copies_are_collected(pool, users):
    files_ = BatchFileStore(pool)
    alice = users["alice"]
    gw = type("G", (), {"files": files_, "pool": pool})
    payload = jsonl([chat_row("o-1", "m")])
    upload_id = await files_.create_upload(
        user_id=alice["user_id"],
        filename="s.jsonl",
        purpose="batch",
        max_total_bytes=1 << 30,
        max_uploads_per_hour=100,
    )
    provisional = await _derived_for(gw, alice, upload_id, payload)
    orphan = await _derived_for(gw, alice, upload_id, payload)
    async with pool.acquire() as conn:
        # One left provisional by a process that died; one made ready for a
        # batch an earlier version deleted without it.
        await conn.execute(
            "UPDATE batch_files SET created_at = now() - interval '2 hours' WHERE id = ANY($1)",
            [provisional["file_id"], orphan["file_id"]],
        )
        await conn.execute(
            "UPDATE batch_files SET status = 'ready', adopted_by_batch_id = 'batch_gone' "
            "WHERE id = $1",
            orphan["file_id"],
        )
    await files_.collect_garbage()
    async with pool.acquire() as conn:
        left = await conn.fetchval(
            "SELECT count(*) FROM batch_files WHERE id = ANY($1)",
            [provisional["file_id"], orphan["file_id"]],
        )
    assert left == 0


async def test_the_public_api_refuses_a_derived_copy_as_input(gateway):
    gw, alice = gateway, gateway.users["alice"]
    source = await upload(gw, alice, jsonl([chat_row("u-1", gw.chat.config.id, url=False)]))
    report = (await preflight(gw, alice, source["id"])).json()
    batch = (await submit(gw, alice, await submission(gw, alice, source, report))).json()
    assert batch["input_file_id"] != source["id"]
    refused = await gw.client.post(
        "/v1/batches",
        headers={"Authorization": f"Bearer api-{alice['user_id']}"},
        json={
            "input_file_id": batch["input_file_id"],
            "endpoint": "/v1/chat/completions",
            "completion_window": "24h",
        },
    )
    assert refused.status_code == 400
    assert "stored requests" in refused.text
    # The derived copy is not listed among the sources to reuse.
    listed = (await gw.client.get("/user/batch-files", headers=session(alice))).json()
    assert [row["id"] for row in listed["data"]] == [source["id"]]


# -- A15, A16, findings 1 and 2: one submission, one batch --------------------------------


async def test_a_submission_key_creates_one_batch_however_often_it_is_sent(gateway):
    gw, alice = gateway, gateway.users["alice"]
    source = await upload(
        gw, alice, jsonl([chat_row(f"i-{i}", gw.chat.config.id) for i in range(3)])
    )
    overrides = {"temperature": 0}
    report = (await preflight(gw, alice, source["id"], overrides=overrides)).json()
    body = await submission(gw, alice, source, report, overrides=overrides)
    key = f"console-{uuid.uuid4()}"

    # A retry sent while the first attempt is still being checked (a timeout): it either
    # replays, or is told to try again (scan_busy) -- never a second batch.
    first, second = await asyncio.gather(
        submit(gw, alice, body, key=key), submit(gw, alice, body, key=key)
    )
    outcomes = sorted(
        [
            (first.status_code, code_of(first) if first.status_code != 200 else None),
            (second.status_code, code_of(second) if second.status_code != 200 else None),
        ]
    )
    assert outcomes[0] == (200, None), (first.text, second.text)
    assert outcomes[1] in ((200, None), (429, "scan_busy")), outcomes
    winner = first if first.status_code == 200 else second
    if second.status_code == 200 and first.status_code == 200:
        assert first.json()["id"] == second.json()["id"]
    again = await submit(gw, alice, body, key=key)
    assert again.json()["id"] == winner.json()["id"]
    assert len(await jobs_of(gw, alice)) == 1
    copies = await derived_of(gw, source["id"])
    assert [row["status"] for row in copies] == ["ready"]  # the loser's copy was discarded

    # The same key for different content is refused.
    changed = await submit(gw, alice, {**body, "name": "another"}, key=key)
    assert changed.status_code == 409

    # A new submission (a new key) of the same content is a second batch, as intended.
    other = await submit(gw, alice, body)
    assert other.status_code == 200 and other.json()["id"] != winner.json()["id"]
    assert len(await jobs_of(gw, alice)) == 2


async def test_a_replay_is_answered_even_when_the_active_cap_is_full(gateway):
    gw, alice = gateway, gateway.users["alice"]
    gw.settings["BATCH_MAX_ACTIVE_PER_USER"] = "1"
    gw.chat.gate = asyncio.Event()  # keeps the first batch active
    try:
        source = await upload(gw, alice, jsonl([chat_row("cap-1", gw.chat.config.id)]))
        report = (await preflight(gw, alice, source["id"])).json()
        body = await submission(gw, alice, source, report)
        key = f"console-{uuid.uuid4()}"
        created = await submit(gw, alice, body, key=key)
        assert created.status_code == 200
        # The cap is full now, with the batch this very key created.
        replay = await submit(gw, alice, body, key=key)
        assert replay.status_code == 200 and replay.json()["id"] == created.json()["id"]
        other = await submit(gw, alice, body)
        assert other.status_code == 429
    finally:
        gw.chat.gate.set()
        del gw.settings["BATCH_MAX_ACTIVE_PER_USER"]


async def test_a_deleted_batch_is_reported_and_not_recreated(gateway):
    gw, alice = gateway, gateway.users["alice"]
    source = await upload(gw, alice, jsonl([chat_row("del-1", gw.chat.config.id)]))
    report = (await preflight(gw, alice, source["id"])).json()
    body = await submission(gw, alice, source, report)
    key = f"console-{uuid.uuid4()}"
    batch = (await submit(gw, alice, body, key=key)).json()
    await finished(gw, alice, batch["id"])
    assert (
        await gw.client.delete(f"/user/batches/{batch['id']}", headers=session(alice))
    ).status_code == 200
    gone = await submit(gw, alice, body, key=key)
    assert gone.status_code == 410
    found = (await gw.client.get(f"/user/batch-submissions/{key}", headers=session(alice))).json()
    assert found == {"state": "deleted", "batch": None}
    assert await jobs_of(gw, alice) == []


async def test_a_late_retry_after_the_record_expired_still_finds_its_batch(gateway):
    """Committed, response lost; the batch finished and its idempotency record was swept."""
    gw, alice = gateway, gateway.users["alice"]
    source = await upload(gw, alice, jsonl([chat_row("late-1", gw.chat.config.id)]))
    report = (await preflight(gw, alice, source["id"])).json()
    body = await submission(gw, alice, source, report)
    key = f"console-{uuid.uuid4()}"
    batch = (await submit(gw, alice, body, key=key)).json()  # the response "was lost"
    await finished(gw, alice, batch["id"])
    async with gw.pool.acquire() as conn:
        await conn.execute(
            "UPDATE batch_idempotency SET expires_at = now() - interval '1 second' "
            "WHERE idem_key = $1",
            key,
        )
    await gw.store.sweep(retention_days=7)
    async with gw.pool.acquire() as conn:
        assert (
            await conn.fetchval("SELECT count(*) FROM batch_idempotency WHERE idem_key = $1", key)
            == 0
        )

    # The browser comes back: a read-only lookup finds the batch ...
    found = (await gw.client.get(f"/user/batch-submissions/{key}", headers=session(alice))).json()
    assert found["state"] == "created" and found["batch"]["id"] == batch["id"]
    # ... and even a retry of the same submission returns it rather than a second batch.
    retried = await submit(gw, alice, body, key=key)
    assert retried.status_code == 200 and retried.json()["id"] == batch["id"]
    assert len(await jobs_of(gw, alice)) == 1
    conflicting = await submit(gw, alice, {**body, "name": "other"}, key=key)
    assert conflicting.status_code == 409

    # Once the batch itself is past its retention and purged, the outcome is unknown:
    # the lookup says so and never "not created" (the page then stops retrying).
    async with gw.pool.acquire() as conn:
        await conn.execute(
            "UPDATE batch_jobs SET retention_seconds = 1, sealed_at = now() - interval '3 days' "
            "WHERE id = $1",
            batch["id"],
        )
    await gw.store.sweep(retention_days=7)
    await gw.store.sweep(retention_days=7)
    found = (await gw.client.get(f"/user/batch-submissions/{key}", headers=session(alice))).json()
    assert found == {"state": "unknown", "batch": None}


# -- A17: cancel, partial results, the export of unfinished requests ---------------------


async def test_a_cancelled_batch_exports_its_unfinished_requests(gateway):
    gw, alice = gateway, gateway.users["alice"]
    gw.chat.gate = asyncio.Event()
    await gw.store.set_model_limit(gw.chat.config.id, 1, updated_by="test")
    try:
        source = await upload(
            gw, alice, jsonl([chat_row(f"x-{i}", gw.chat.config.id) for i in range(4)])
        )
        report = (await preflight(gw, alice, source["id"])).json()
        batch = (await submit(gw, alice, await submission(gw, alice, source, report))).json()
        await until(lambda: asyncio.sleep(0, result=len(gw.chat.calls) == 1), what="one running")
        early = await gw.client.get(
            f"/user/batches/{batch['id']}/unfinished-requests", headers=session(alice)
        )
        assert early.status_code == 409  # not final yet
        cancelled = await gw.client.post(
            f"/user/batches/{batch['id']}/cancel", headers=session(alice)
        )
        assert cancelled.json()["status"] == "cancelling"
        gw.chat.gate.set()
        done = await finished(gw, alice, batch["id"], wanted="cancelled")
    finally:
        await gw.store.delete_model_limit(gw.chat.config.id)
    assert done["request_counts"]["completed"] == 1 and done["cancelled"] == 3
    exported = await gw.client.get(
        f"/user/batches/{batch['id']}/unfinished-requests", headers=session(alice)
    )
    assert exported.status_code == 200
    lines = [json.loads(line) for line in exported.text.splitlines()]
    assert sorted(line["custom_id"] for line in lines) == ["x-1", "x-2", "x-3"]
    assert {line["url"] for line in lines} == {"/v1/chat/completions"}
    assert all(set(line) == {"custom_id", "method", "url", "body"} for line in lines)
    outputs = await gw.client.get(
        f"/user/batches/{batch['id']}/results", headers=session(alice), params={"kind": "output"}
    )
    assert len(outputs.text.splitlines()) == 1


# -- A19, finding 5: everything is the signed-in user's, administrators included ---------


async def test_another_user_or_an_administrator_finds_nothing_of_alices(gateway):
    gw, alice = gateway, gateway.users["alice"]
    source = await upload(gw, alice, jsonl([chat_row("own-1", gw.chat.config.id)]))
    report = (await preflight(gw, alice, source["id"])).json()
    key = f"console-{uuid.uuid4()}"
    batch = (await submit(gw, alice, await submission(gw, alice, source, report), key=key)).json()
    await finished(gw, alice, batch["id"])

    for intruder in (gw.users["bob"], gw.users["admin"]):
        headers = session(intruder)
        for path in (
            f"/user/batches/{batch['id']}",
            f"/user/batches/{batch['id']}/requests",
            f"/user/batches/{batch['id']}/unfinished-requests",
            f"/user/batches/{batch['id']}/results",
            f"/user/batch-files/{source['id']}/content",
            f"/user/batch-files/{batch['input_file_id']}/content",
        ):
            assert (await gw.client.get(path, headers=headers)).status_code == 404, path
        assert (
            await gw.client.delete(f"/user/batch-files/{source['id']}", headers=headers)
        ).status_code == 404
        assert (await preflight(gw, intruder, source["id"])).status_code == 409
        listed = await gw.client.get("/user/batches", headers=headers, params={"q": batch["id"]})
        assert listed.json()["data"] == []
        files_listed = (await gw.client.get("/user/batch-files", headers=headers)).json()
        assert source["id"] not in {row["id"] for row in files_listed["data"]}
        found = (await gw.client.get(f"/user/batch-submissions/{key}", headers=headers)).json()
        assert found["state"] == "unknown"
        # Their own file, copied "from" alice's batch.
        own = await upload(gw, intruder, jsonl([chat_row("own-2", gw.chat.config.id)]))
        own_report = (await preflight(gw, intruder, own["id"])).json()
        copied = await submit(
            gw,
            intruder,
            await submission(gw, intruder, own, own_report, copied_from=batch["id"]),
        )
        assert copied.status_code == 404
        # Alice's source as theirs.
        stolen = await submit(gw, intruder, await submission(gw, intruder, source, report))
        assert stolen.status_code == 409 and code_of(stolen) == "source_unavailable"
    assert (await file_row(gw, source["id"]))["deleted_at"] is None


# -- A3, A23: what can be copied ---------------------------------------------------------


async def test_api_batches_are_copied_by_input_method_and_type(gateway):
    gw, alice = gateway, gateway.users["alice"]
    api = {"Authorization": f"Bearer api-{alice['user_id']}"}
    body, content_type = multipart(jsonl([chat_row("api-1", gw.chat.config.id)]))
    uploaded = await gw.client.post(
        "/v1/files", headers={**api, "content-type": content_type}, content=body
    )
    file_id = uploaded.json()["id"]
    from_file = await gw.client.post(
        "/v1/batches",
        headers=api,
        json={
            "input_file_id": file_id,
            "endpoint": "/v1/chat/completions",
            "completion_window": "24h",
        },
    )
    inline = await gw.client.post(
        "/v1/batches",
        headers=api,
        json={"requests": [chat_row("api-2", gw.chat.config.id)]},
    )
    file_batch = from_file.json()["id"]
    detail = (await gw.client.get(f"/user/batches/{file_batch}", headers=session(alice))).json()
    assert detail["created_via"] == "api" and detail["copy"]["state"] == "ready"
    assert detail["source"]["id"] == file_id
    inline_detail = (
        await gw.client.get(f"/user/batches/{inline.json()['id']}", headers=session(alice))
    ).json()
    assert inline_detail["copy"] == {"state": "unavailable", "reason": "inline_input"}

    # The API batch's input is a source the console checks and runs (no overrides).
    report = (await preflight(gw, alice, file_id)).json()
    assert report["valid"] is True
    await gw.client.delete(f"/v1/files/{file_id}", headers=api)
    detail = (await gw.client.get(f"/user/batches/{file_batch}", headers=session(alice))).json()
    assert detail["copy"] == {"state": "reupload", "reason": "source_unavailable"}


# -- A21, A22: the switch, and the public API as it was ----------------------------------


async def test_batch_enabled_off_hides_every_console_create_route(gateway):
    gw, alice = gateway, gateway.users["alice"]
    source = await upload(gw, alice, jsonl([chat_row("off-1", gw.chat.config.id)]))
    gw.settings["BATCH_ENABLED"] = "0"
    try:
        for method, path in (
            ("GET", "/user/batch-create/context"),
            ("GET", "/user/batch-files"),
            ("POST", "/user/batch-files"),
            ("POST", "/user/batch-preflight"),
            ("POST", "/user/batches"),
            ("GET", f"/user/batch-submissions/console-{uuid.uuid4()}"),
            ("DELETE", f"/user/batch-files/{source['id']}"),
        ):
            response = await gw.client.request(
                method,
                path,
                headers=session(alice),
                json={"source_file_id": source["id"], "phase": "identify"}
                if path == "/user/batch-preflight"
                else None,
            )
            assert response.status_code in (404, 422), (path, response.status_code)
            if response.status_code == 422:
                # Body validation answered first; still nothing was written.
                assert path == "/user/batches"
    finally:
        del gw.settings["BATCH_ENABLED"]
    assert (await file_row(gw, source["id"]))["deleted_at"] is None


async def test_the_public_api_still_accepts_what_the_console_refuses(gateway):
    gw, alice = gateway, gateway.users["alice"]
    api = {"Authorization": f"Bearer api-{alice['user_id']}"}
    body, content_type = multipart(
        jsonl([chat_row("pub-1", gw.chat.config.id, max_completion_tokens=32)])
    )
    file_id = (
        await gw.client.post(
            "/v1/files", headers={**api, "content-type": content_type}, content=body
        )
    ).json()["id"]
    console_view = (await preflight(gw, alice, file_id)).json()
    assert [e["code"] for e in console_view["errors"]] == ["unsupported_parameter"]
    created = await gw.client.post(
        "/v1/batches",
        headers=api,
        json={
            "input_file_id": file_id,
            "endpoint": "/v1/chat/completions",
            "completion_window": "24h",
        },
    )
    assert created.status_code == 200
    batch_id = created.json()["id"]

    async def done():
        row = (await gw.client.get(f"/v1/batches/{batch_id}", headers=api)).json()
        return row if row["status"] == "completed" else None

    await until(done, what="the public file batch")


# -- review round 2: limits, numbers, capabilities, copy eligibility, expiry under a lock ---


async def test_a_huge_integer_is_a_row_error_not_a_server_error(gateway):
    gw, alice = gateway, gateway.users["alice"]
    row = json.dumps(chat_row("big-1", gw.chat.config.id)).replace(
        '"messages"', '"max_tokens": ' + "9" * 5000 + ', "messages"', 1
    )
    source = await upload(gw, alice, jsonl([row, chat_row("ok-2", gw.chat.config.id)]))
    for phase in ("identify", "validate"):
        response = await preflight(gw, alice, source["id"], phase=phase)
        assert response.status_code == 200, response.text
        errors = response.json()["errors"]
        assert [(e["line"], e["code"]) for e in errors] == [(1, "invalid_json_line")]
        assert "9999" not in errors[0]["message"]
    report = (await preflight(gw, alice, source["id"])).json()
    refused = await submit(
        gw,
        alice,
        await submission(
            gw,
            alice,
            source,
            report,
            endpoint="/v1/chat/completions",
            confirmed_model={"requested": gw.chat.config.id, "canonical": gw.chat.config.id},
        ),
    )
    assert refused.status_code == 422 and code_of(refused) == "invalid_file"
    assert await jobs_of(gw, alice) == []


async def test_a_model_declaring_no_sampling_parameter_takes_no_override(gateway):
    gw, alice = gateway, gateway.users["alice"]
    catalog = {m["id"]: m for m in (await context(gw, alice))["models"]["chat"]}
    assert catalog[gw.bare.config.id]["supported_sampling_parameters"] == []
    source = await upload(gw, alice, jsonl([chat_row("s-1", gw.chat.config.id)]))
    for model in (gw.bare.config.id, gw.top_p_only.config.id):
        report = (
            await preflight(gw, alice, source["id"], overrides={"model": model, "temperature": 0.7})
        ).json()
        assert report["valid"] is False
        assert [(e["code"], e["field"]) for e in report["errors"]] == [
            ("unsupported_parameter", "overrides.temperature")
        ]
    report = (
        await preflight(
            gw, alice, source["id"], overrides={"model": gw.bare.config.id, "max_tokens": 8}
        )
    ).json()
    assert [e["field"] for e in report["errors"]] == ["overrides.max_tokens"]
    # Without overrides the model is usable as the file has it.
    report = (
        await preflight(gw, alice, source["id"], overrides={"model": gw.bare.config.id})
    ).json()
    assert report["valid"] is True, report["errors"]


async def test_the_stored_file_is_held_to_the_file_size_limit_after_it_grows(gateway):
    gw, alice = gateway, gateway.users["alice"]
    gw.settings["BATCH_MAX_BODY_MB"] = "1"
    try:
        # Below 1 MiB as uploaded; every line gains a url and method when stored.
        rows = [
            {
                "custom_id": f"g-{i:05d}",
                "body": {
                    "model": gw.chat.config.id,
                    "messages": [{"role": "user", "content": "x" * 395}],
                },
            }
            for i in range(2000)
        ]
        payload = jsonl(rows)
        assert len(payload) < 1024 * 1024
        source = await upload(gw, alice, payload)
        report = (await preflight(gw, alice, source["id"])).json()
        assert report["stores_copy"] is True
        assert report["stored_bytes"] > 1024 * 1024
        assert [(e["code"], e["line"]) for e in report["errors"]] == [("file_too_large", None)]
        refused = await submit(
            gw,
            alice,
            await submission(
                gw,
                alice,
                source,
                report,
                endpoint="/v1/chat/completions",
                confirmed_model={"requested": gw.chat.config.id, "canonical": gw.chat.config.id},
            ),
        )
        assert refused.status_code == 422 and code_of(refused) == "invalid_file"
        assert await jobs_of(gw, alice) == []
        # The provisional copy written on the way was discarded.
        assert await derived_of(gw, source["id"]) == []
    finally:
        del gw.settings["BATCH_MAX_BODY_MB"]


async def test_a_copy_must_name_a_batch_the_console_can_copy(gateway):
    gw, alice = gateway, gateway.users["alice"]
    api = {"Authorization": f"Bearer api-{alice['user_id']}"}
    inline = await gw.client.post(
        "/v1/batches", headers=api, json={"requests": [chat_row("api-1", gw.chat.config.id)]}
    )
    source = await upload(gw, alice, jsonl([chat_row("c-1", gw.chat.config.id)]))
    report = (await preflight(gw, alice, source["id"])).json()
    refused = await submit(
        gw, alice, await submission(gw, alice, source, report, copied_from=inline.json()["id"])
    )
    assert refused.status_code == 422 and code_of(refused) == "copy_unsupported"
    assert [job["id"] for job in await jobs_of(gw, alice)] == [inline.json()["id"]]


async def _source_upload(files_, user, payload: bytes, *, retention: int = 86400) -> dict[str, Any]:
    import hashlib

    file_id = await files_.create_upload(
        user_id=user["user_id"],
        filename="s.jsonl",
        purpose="batch",
        max_total_bytes=1 << 30,
        max_uploads_per_hour=100,
    )
    await files_.append_chunk(
        file_id=file_id, user_id=user["user_id"], seq=0, data=payload, max_total_bytes=1 << 30
    )
    return await files_.finish_upload(
        file_id=file_id,
        user_id=user["user_id"],
        sha256=hashlib.sha256(payload).hexdigest(),
        line_count=payload.count(b"\n"),
        retention_seconds=retention,
        check_owner=True,
    )


async def _create_behind_a_lock(pool, users, *, expire_in: float, hold_until_expired: bool):
    """Hold the source's row lock while a create waits on it; release it untouched."""
    store, files_ = BatchStore(pool), BatchFileStore(pool)
    alice = users["alice"]
    payload = jsonl([chat_row("e-1", "m")])
    source = await _source_upload(files_, alice, payload)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE batch_files SET expires_at = clock_timestamp() + make_interval(secs => $2) "
            "WHERE id = $1",
            source["id"],
            expire_in,
        )
    gw = type("G", (), {"files": files_, "pool": pool})
    derived = await _derived_for(gw, alice, source["id"], payload)
    async with pool.acquire() as holder:
        tx = holder.transaction()
        await tx.start()
        await holder.execute("SELECT id FROM batch_files WHERE id = $1 FOR UPDATE", source["id"])
        holder_pid = await holder.fetchval("SELECT pg_backend_pid()")
        create = asyncio.create_task(
            store.create_console_batch(**_create_args(alice, source, derived))
        )

        async def waiting() -> bool:
            # The barrier: the create is blocked on this very lock.
            async with pool.acquire() as probe:
                return bool(
                    await probe.fetchval(
                        "SELECT EXISTS (SELECT 1 FROM pg_locks AS l "
                        "JOIN pg_stat_activity AS a ON a.pid = l.pid "
                        "WHERE NOT l.granted AND $1 = ANY(pg_blocking_pids(l.pid)))",
                        holder_pid,
                    )
                )

        await until(waiting, timeout=10, what="the create to wait on the source lock")
        if hold_until_expired:
            async with pool.acquire() as probe:
                remaining = await probe.fetchval(
                    "SELECT EXTRACT(EPOCH FROM expires_at - clock_timestamp()) "
                    "FROM batch_files WHERE id = $1",
                    source["id"],
                )
            await asyncio.sleep(max(float(remaining), 0) + 0.3)
        await tx.rollback()  # released without changing the row
        outcome = await asyncio.gather(create, return_exceptions=True)
    return outcome[0], source, derived


async def test_a_source_that_expires_while_its_create_waits_is_refused(pool, users):
    outcome, _source, derived = await _create_behind_a_lock(
        pool, users, expire_in=1.0, hold_until_expired=True
    )
    assert isinstance(outcome, BatchInputUnavailable), outcome
    async with pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM batch_jobs WHERE user_id = $1", users["alice"]["user_id"]
            )
            == 0
        )
        row = await conn.fetchrow(
            "SELECT status, adopted_by_batch_id FROM batch_files WHERE id = $1",
            derived["file_id"],
        )
    assert row["status"] == "uploading" and row["adopted_by_batch_id"] is None


async def test_a_source_still_live_after_the_wait_is_adopted(pool, users):
    outcome, _source, derived = await _create_behind_a_lock(
        pool, users, expire_in=60.0, hold_until_expired=False
    )
    assert isinstance(outcome, dict) and outcome["input_file_id"] == derived["file_id"]


async def test_a_full_storage_on_the_buffered_copy_is_a_413_and_leaves_nothing(gateway):
    gw, alice = gateway, gateway.users["alice"]
    gw.settings["BATCH_FILES_MAX_TOTAL_MB_PER_USER"] = "1"
    cap = 1024 * 1024
    try:
        # Rows without a url: what is stored differs from the source, in under a chunk.
        source = await upload(gw, alice, jsonl([chat_row("t-1", gw.chat.config.id, url=False)]))
        report = (await preflight(gw, alice, source["id"])).json()
        assert report["valid"] is True and report["stores_copy"] is True
        # Exactly one byte of storage left.
        async with gw.pool.acquire() as conn:
            stored = await conn.fetchval(
                "SELECT COALESCE(sum(bytes), 0) FROM batch_files "
                "WHERE user_id = $1 AND kind = 'upload'",
                alice["user_id"],
            )
            await conn.execute(
                "INSERT INTO batch_files (id, user_id, purpose, kind, filename, status, bytes) "
                "VALUES ($1, $2, 'batch', 'upload', 'filler', 'ready', $3)",
                f"file-filler-{uuid.uuid4().hex[:8]}",
                alice["user_id"],
                cap - 1 - int(stored),
            )
        refused = await submit(gw, alice, await submission(gw, alice, source, report))
        assert refused.status_code == 413, refused.text
        assert code_of(refused) == "storage_full"
        assert await jobs_of(gw, alice) == []
        assert await derived_of(gw, source["id"]) == []
    finally:
        del gw.settings["BATCH_FILES_MAX_TOTAL_MB_PER_USER"]
