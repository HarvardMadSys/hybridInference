"""Tests for the batch API router (``/v1/batches``), every target and input.

The store is an in-memory stand-in for ``serving.storage.batch_store`` that keeps
the half of its contract the router uses; the settings are answered by a dict in
place of the database-backed configuration. Running the requests is the worker's
job and is not exercised here.
"""

from __future__ import annotations

import hashlib
import itertools
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI, Header, HTTPException
from httpx import ASGITransport, AsyncClient

from routing.routers import FixedRouter
from serving.adapters.base import BaseAdapter, ModelConfig
from serving.batch import admission, config as batch_config_module, targets
from serving.servers.auth import verify_api_key, verify_batch_management_identity
from serving.servers.deps import AppServices
from serving.servers.middleware.error import install_error_handlers
from serving.servers.routers import batches
from serving.storage.batch_store import BatchDeleteInProgress, BatchOwnerUnavailable

MODEL = "glm-4.7"
ADMIN_MODEL = "admin-model"
CANONICAL_MODEL = "canon-model"
ALIAS_MODEL = "alias-model"

ACTIVE = {"validating", "in_progress", "cancelling"}
OPENAI_BATCH_FIELDS = {
    "id",
    "object",
    "endpoint",
    "errors",
    "input_file_id",
    "completion_window",
    "status",
    "output_file_id",
    "error_file_id",
    "created_at",
    "in_progress_at",
    "expires_at",
    "finalizing_at",
    "completed_at",
    "failed_at",
    "expired_at",
    "cancelling_at",
    "cancelled_at",
    "request_counts",
    "metadata",
}


class _Adapter(BaseAdapter):
    async def chat_completion(self, messages, **params):  # pragma: no cover - never dispatched
        raise AssertionError("the router must not run requests")

    async def stream_chat_completion(self, messages, **params):  # pragma: no cover
        raise AssertionError("the router must not run requests")
        yield ""


def _adapter(model_id: str) -> _Adapter:
    return _Adapter(
        ModelConfig(
            id=model_id,
            name=model_id,
            provider="test",
            base_url="http://test",
            context_length=8192,
            max_output_length=4096,
            pricing={"prompt": "0", "completion": "0"},
        )
    )


class FakeBatchStore:
    """In-memory batch store with the router-facing half of the store contract."""

    def __init__(self) -> None:
        self.batches: dict[str, dict[str, Any]] = {}
        self.items: dict[str, list[dict[str, Any]]] = {}
        self.created: list[dict[str, Any]] = []
        self._item_ids = itertools.count(1)
        self._ticks = itertools.count()

    async def create_batch(
        self,
        *,
        batch_id: str,
        user_id: str,
        key_hash: str | None,
        endpoint: str,
        model_ids: list[str],
        items: list[dict[str, Any]],
        metadata: dict[str, Any] | None,
        completion_window_seconds: int,
        **options: Any,
    ) -> dict[str, Any]:
        self.options = options
        self.created.append(
            {
                "batch_id": batch_id,
                "user_id": user_id,
                "key_hash": key_hash,
                "endpoint": endpoint,
                "model_ids": model_ids,
                "items": items,
                "metadata": metadata,
                "completion_window_seconds": completion_window_seconds,
            }
        )
        now = datetime(2026, 10, 25, 12, 0, tzinfo=UTC) + timedelta(seconds=next(self._ticks))
        row = {
            "id": batch_id,
            "user_id": user_id,
            "key_hash": key_hash,
            "endpoint": endpoint,
            "status": "validating",
            "model_ids": list(model_ids),
            "request_count": len(items),
            "completed_count": 0,
            "failed_count": 0,
            "metadata": metadata,
            "error": None,
            "cost_usd": 0.0,
            "created_at": now,
            "in_progress_at": None,
            "completed_at": None,
            "failed_at": None,
            "expired_at": None,
            "cancelling_at": None,
            "cancelled_at": None,
            "expires_at": now + timedelta(seconds=completion_window_seconds),
            "paused_until": None,
            "api_surface": options.get("api_surface", "openai_inline"),
            "target": options.get("target", "chat"),
            "schema_version": 2,
            "phase": options.get("phase", "runnable"),
            "protocol_profile": options.get("protocol_profile"),
        }
        self.batches[batch_id] = row
        self.items[batch_id] = [
            {
                "id": next(self._item_ids),
                "custom_id": item["custom_id"],
                "model_id": item["model_id"],
                "request": item["request"],
                "status": "queued",
                "status_code": None,
                "response": None,
                "error": None,
                "prompt_tokens": None,
                "completion_tokens": None,
                "cost_usd": None,
                "request_id": None,
                "completed_at": None,
            }
            for item in items
        ]
        return dict(row)

    async def count_active_batches(self, user_id: str) -> int:
        return sum(
            1
            for row in self.batches.values()
            if row["user_id"] == user_id and row["status"] in ACTIVE
        )

    async def find_idempotent(self, *, owner_id: str, api_surface: str, key: str) -> None:
        return None

    async def get_batch(self, batch_id: str, *, user_id: str | None = None) -> dict | None:
        row = self.batches.get(batch_id)
        if row is None or (user_id is not None and row["user_id"] != user_id):
            return None
        return dict(row)

    async def list_batches(
        self, *, user_id: str, limit: int, after: str | None = None
    ) -> list[dict[str, Any]]:
        rows = sorted(
            (row for row in self.batches.values() if row["user_id"] == user_id),
            key=lambda row: (row["created_at"], row["id"]),
            reverse=True,
        )
        if after is not None:
            ids = [row["id"] for row in rows]
            if after not in ids:
                return []
            rows = rows[ids.index(after) + 1 :]
        return [dict(row) for row in rows[:limit]]

    async def list_results(
        self, batch_id: str, *, after: int | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        items = [i for i in self.items.get(batch_id, []) if after is None or i["id"] > after]
        return [
            {key: value for key, value in item.items() if key not in ("model_id", "request")}
            for item in items[:limit]
        ]

    async def cancel_batch(self, batch_id: str, *, user_id: str | None = None) -> dict | None:
        row = self.batches.get(batch_id)
        if row is None or (user_id is not None and row["user_id"] != user_id):
            return None
        if row["status"] in ("validating", "in_progress"):
            now = row["created_at"] + timedelta(minutes=5)
            row["status"] = "cancelling"
            row["cancelling_at"] = now
            for item in self.items[batch_id]:
                if item["status"] == "queued":
                    item["status"] = "cancelled"
            if not any(item["status"] == "running" for item in self.items[batch_id]):
                row["status"] = "cancelled"
                row["cancelled_at"] = now
        return dict(row)

    async def delete_batch(self, batch_id: str, *, user_id: str | None = None) -> bool:
        row = self.batches.get(batch_id)
        if row is None or (user_id is not None and row["user_id"] != user_id):
            return False
        del self.batches[batch_id]
        del self.items[batch_id]
        return True


class FakeScheduler:
    def __init__(self) -> None:
        self.notified = 0

    def notify(self) -> None:
        self.notified += 1


@pytest.fixture
def settings(monkeypatch) -> dict[str, str]:
    """Batch settings as stored values; empty means every default applies."""
    values: dict[str, str] = {}

    def fake_config_value(key: str, default: str | None = None) -> str | None:
        return values.get(key, default)

    monkeypatch.setattr(batch_config_module, "config_value", fake_config_value)
    return values


@pytest.fixture
def users() -> dict[str, dict[str, Any]]:
    """The callers, by API key. Tests change a caller's role in place."""
    return {
        "key-alice": {"user_id": "alice", "role": "internal", "auth_key_hash": "hash-alice"},
        "key-bob": {"user_id": "bob", "role": "internal", "auth_key_hash": "hash-bob"},
        "key-carol": {"user_id": "carol", "role": "pro", "auth_key_hash": "hash-carol"},
        "key-dave": {"user_id": "dave", "role": "free", "auth_key_hash": "hash-dave"},
        "key-root": {"user_id": "root", "role": "admin", "auth_key_hash": "hash-root"},
    }


@pytest.fixture
def store() -> FakeBatchStore:
    return FakeBatchStore()


@pytest.fixture
def scheduler() -> FakeScheduler:
    return FakeScheduler()


@pytest.fixture
def services(store, scheduler) -> AppServices:
    router = FixedRouter()
    router.register_route(MODEL, [(_adapter(MODEL), 1.0)])
    # Embedding admission reads its own access gate, not the chat route above.
    router.register_embedding_route(MODEL, [(_adapter(MODEL), 1.0)], required_role="free")
    router.register_route(ADMIN_MODEL, [(_adapter(ADMIN_MODEL), 1.0)], required_role="admin")
    router.register_route(
        CANONICAL_MODEL, [(_adapter(CANONICAL_MODEL), 1.0)], aliases=[ALIAS_MODEL]
    )
    return AppServices(router=router, batch_store=store, batch_scheduler=scheduler)


@pytest.fixture
def app(services, users, settings) -> FastAPI:
    app = FastAPI(title="Batches Test App")
    app.state.services = services

    async def fake_verify(authorization: str | None = Header(None)) -> dict[str, Any]:
        key = (authorization or "").removeprefix("Bearer ")
        if key not in users:
            raise HTTPException(status_code=401, detail="Invalid API key")
        return {"authenticated": True, **users[key]}

    app.dependency_overrides[verify_api_key] = fake_verify
    # Every batch route authenticates without the daily quota gate.
    app.dependency_overrides[verify_batch_management_identity] = fake_verify
    install_error_handlers(app)
    app.include_router(batches.router)
    return app


@pytest_asyncio.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


def _auth(key: str = "key-alice") -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _chat(model: str = MODEL, **extra: Any) -> dict[str, Any]:
    return {"model": model, "messages": [{"role": "user", "content": "hello"}], **extra}


def _request(custom_id: str, body: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
    return {
        "custom_id": custom_id,
        "method": "POST",
        "url": "/v1/chat/completions",
        "body": body if body is not None else _chat(),
        **extra,
    }


def _body(count: int = 1, **extra: Any) -> dict[str, Any]:
    return {"requests": [_request(f"req-{i}") for i in range(count)], **extra}


def _message(response) -> str:
    return response.json()["error"]["message"]


async def _create(client, key: str = "key-alice", body: dict[str, Any] | None = None):
    response = await client.post("/v1/batches", json=body or _body(), headers=_auth(key))
    assert response.status_code == 200, response.text
    return response.json()


# --- the surface exists only with a store and BATCH_ENABLED on ----------------

ROUTES = [
    ("POST", "/v1/batches"),
    ("GET", "/v1/batches"),
    ("GET", "/v1/batches/batch_x"),
    ("POST", "/v1/batches/batch_x/cancel"),
    ("DELETE", "/v1/batches/batch_x"),
    ("GET", "/v1/batches/batch_x/results"),
]


@pytest.mark.parametrize(("method", "path"), ROUTES)
async def test_every_route_is_404_while_batches_are_disabled(client, settings, method, path):
    settings["BATCH_ENABLED"] = "0"

    response = await client.request(method, path, json=_body(), headers=_auth("key-root"))

    assert response.status_code == 404
    assert "not available" in _message(response)


@pytest.mark.parametrize(("method", "path"), ROUTES)
async def test_every_route_is_404_without_a_batch_store(client, services, method, path):
    services.batch_store = None

    response = await client.request(method, path, json=_body(), headers=_auth("key-root"))

    assert response.status_code == 404


@pytest.mark.parametrize("unavailable", ["disabled", "missing-store"])
@pytest.mark.parametrize(("method", "path"), ROUTES)
async def test_unavailable_surface_is_404_before_authentication(
    client, settings, services, unavailable, method, path
):
    if unavailable == "disabled":
        settings["BATCH_ENABLED"] = "0"
    else:
        services.batch_store = None

    response = await client.request(method, path)

    assert response.status_code == 404
    assert _message(response) == "Batch inference is not available on this gateway"


@pytest.mark.parametrize("unavailable", ["disabled", "missing-store"])
@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    [
        ("POST", "/v1/batches", {"content": b"{not json"}),
        ("GET", "/v1/batches", {"params": {"limit": "not-an-integer"}}),
        (
            "GET",
            "/v1/batches/batch_x/results",
            {"params": {"limit": "not-an-integer", "after": "not-a-result-id"}},
        ),
    ],
    ids=["malformed-create-body", "invalid-list-query", "invalid-results-query"],
)
async def test_unavailable_surface_is_404_before_request_validation(
    client, settings, services, unavailable, method, path, kwargs
):
    if unavailable == "disabled":
        settings["BATCH_ENABLED"] = "0"
    else:
        services.batch_store = None

    response = await client.request(method, path, headers=_auth(), **kwargs)

    assert response.status_code == 404
    assert _message(response) == "Batch inference is not available on this gateway"


async def test_disabled_surface_is_404_before_create_quota(client, settings, store, monkeypatch):
    settings["BATCH_ENABLED"] = "0"

    async def unexpected_quota_check(_user_id):
        raise AssertionError("create quota must not run for an unavailable surface")

    monkeypatch.setattr(store, "count_active_batches", unexpected_quota_check)

    response = await client.post("/v1/batches", json=_body(), headers=_auth())

    assert response.status_code == 404


async def test_enabled_surface_still_requires_authentication(client):
    response = await client.get("/v1/batches")

    assert response.status_code == 401
    assert _message(response) == "Invalid API key"


# --- who may create -------------------------------------------------------------


@pytest.mark.parametrize("key", ["key-carol", "key-dave"])
async def test_creating_needs_the_internal_role_by_default(client, store, key):
    response = await client.post("/v1/batches", json=_body(), headers=_auth(key))

    assert response.status_code == 403
    assert "'internal'" in _message(response)
    assert store.created == []


@pytest.mark.parametrize("key", ["key-alice", "key-root"])
async def test_internal_and_admin_callers_may_create(client, key):
    batch = await _create(client, key)

    assert batch["status"] == "validating"


async def test_a_lower_min_role_lets_pro_callers_create(client, settings):
    settings["BATCH_MIN_ROLE"] = "pro"

    await _create(client, "key-carol")
    response = await client.post("/v1/batches", json=_body(), headers=_auth("key-dave"))

    assert response.status_code == 403


async def test_a_demoted_owner_can_still_read_cancel_and_delete(client, users):
    batch = await _create(client, "key-alice")
    users["key-alice"]["role"] = "free"
    path = f"/v1/batches/{batch['id']}"

    assert (await client.post("/v1/batches", json=_body(), headers=_auth())).status_code == 403
    assert (await client.get(path, headers=_auth())).status_code == 200
    listed = await client.get("/v1/batches", headers=_auth())
    assert [b["id"] for b in listed.json()["data"]] == [batch["id"]]
    assert (await client.get(f"{path}/results", headers=_auth())).status_code == 200
    cancelled = await client.post(f"{path}/cancel", headers=_auth())
    assert cancelled.json()["status"] == "cancelled"
    assert (await client.delete(path, headers=_auth())).status_code == 200


@pytest.mark.parametrize(
    ("method", "path"), [("POST", "/v1/batches"), ("GET", "/v1/batches")], ids=["create", "list"]
)
async def test_x_on_behalf_of_is_refused(client, store, method, path):
    response = await client.request(
        method, path, json=_body(), headers={**_auth(), "X-On-Behalf-Of": "bob"}
    )

    assert response.status_code == 400
    assert "X-On-Behalf-Of" in _message(response)
    assert store.created == []


# --- the body cap -----------------------------------------------------------------


def _oversized_body() -> bytes:
    """A valid create body just over 1 MiB, the smallest cap BATCH_MAX_BODY_MB allows."""
    padding = "x" * (1024 * 1024)
    return json.dumps(_body(1, metadata={"note": "n"}) | {"padding": padding}).encode()


async def test_a_declared_length_over_the_cap_is_refused_unread(app, settings):
    settings["BATCH_MAX_BODY_MB"] = "1"
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        raise AssertionError("the body was read")

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/batches",
        "raw_path": b"/v1/batches",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"authorization", b"Bearer key-alice"),
            (b"content-type", b"application/json"),
            (b"content-length", str(1024 * 1024 + 1).encode()),
        ],
        "client": ("127.0.0.1", 50000),
        "server": ("test", 80),
    }
    await app(scope, receive, send)

    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    assert start["status"] == 413
    assert "1 MiB" in json.loads(body)["error"]["message"]


async def test_a_body_with_a_length_over_the_cap_is_refused(client, settings, store):
    settings["BATCH_MAX_BODY_MB"] = "1"

    response = await client.post("/v1/batches", content=_oversized_body(), headers=_auth())

    assert response.status_code == 413
    assert store.created == []


async def test_a_streamed_body_over_the_cap_is_refused_without_reading_it_all(
    client, settings, store
):
    settings["BATCH_MAX_BODY_MB"] = "1"
    raw = _oversized_body()
    chunk = 64 * 1024
    yielded = 0

    async def stream():
        nonlocal yielded
        for start in range(0, len(raw), chunk):
            yielded += 1
            yield raw[start : start + chunk]
        # Twice as much again, which a reader that stops at the cap never asks for.
        for _ in range(len(raw) // chunk):
            yielded += 1
            yield b" " * chunk

    response = await client.post("/v1/batches", content=stream(), headers=_auth())

    assert response.status_code == 413
    assert yielded <= len(raw) // chunk + 2
    assert store.created == []


async def test_a_body_under_the_cap_is_accepted(client, settings):
    settings["BATCH_MAX_BODY_MB"] = "1"

    await _create(client)


# --- validating the body ----------------------------------------------------------


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (b"{not json", "not valid JSON"),
        (b'{"requests": [], "x": NaN}', "not valid JSON"),
        (b'{"requests": [], "x": -Infinity}', "not valid JSON"),
        (b"\xff\xfe\xfd", "not valid JSON"),
        (b"[]", "Invalid batch request body"),
        (b'{"requests": []}', "requests"),
        (b"{}", "requests: Field required"),
    ],
    ids=["syntax", "nan", "infinity", "not-unicode", "not-an-object", "empty", "no-requests"],
)
async def test_an_unreadable_body_is_400(client, store, content, expected):
    response = await client.post("/v1/batches", content=content, headers=_auth())

    assert response.status_code == 400
    assert expected in _message(response)
    assert store.created == []


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda r: r.update(custom_id=""), "custom_id"),
        (lambda r: r.pop("custom_id"), "custom_id: Field required"),
        (lambda r: r.update(method="GET"), "method"),
        (lambda r: r.update(body="hello"), "body"),
    ],
    ids=["empty-custom-id", "no-custom-id", "method", "body-not-object"],
)
async def test_a_malformed_request_is_400_and_names_its_position(client, store, mutate, expected):
    body = _body(3)
    mutate(body["requests"][1])

    response = await client.post("/v1/batches", json=body, headers=_auth())

    assert response.status_code == 400
    assert _message(response).startswith("requests[1]")
    assert expected in _message(response)
    assert store.created == []


@pytest.mark.parametrize(
    ("custom_id", "expected"),
    [("x" * 513, "at most 512 characters"), ("a\u0000b", "NUL character")],
    ids=["too-long", "nul"],
)
async def test_an_unstorable_custom_id_is_400(client, store, custom_id, expected):
    body = {"requests": [_request("ok"), _request(custom_id)]}

    response = await client.post("/v1/batches", json=body, headers=_auth())

    assert response.status_code == 400
    assert _message(response).startswith("requests[1]")
    assert "custom_id" in _message(response)
    assert expected in _message(response)
    assert store.created == []


async def test_a_custom_id_of_512_characters_is_accepted(client, store):
    await _create(client, body={"requests": [_request("x" * 512)]})

    assert store.created[0]["items"][0]["custom_id"] == "x" * 512


async def test_a_body_the_store_refuses_is_400(client, store, monkeypatch):
    async def refusing_create(**kwargs):
        raise ValueError("custom_id 'a' appears more than once")

    monkeypatch.setattr(store, "create_batch", refusing_create)

    response = await client.post("/v1/batches", json=_body(), headers=_auth())

    assert response.status_code == 400
    assert _message(response).startswith("Invalid batch: a custom_id is repeated")
    # The store's own words stay in the log, not the response.
    assert "appears more than once" not in response.text


async def test_create_is_401_if_the_owner_becomes_unavailable(client, store, monkeypatch):
    async def owner_unavailable(**kwargs):
        raise BatchOwnerUnavailable(kwargs["user_id"])

    monkeypatch.setattr(store, "create_batch", owner_unavailable)

    response = await client.post("/v1/batches", json=_body(), headers=_auth())

    assert response.status_code == 401
    assert _message(response) == "Invalid or expired API key"
    assert store.created == []


async def test_a_duplicate_custom_id_is_400(client, store):
    body = {"requests": [_request("same"), _request("other"), _request("same")]}

    response = await client.post("/v1/batches", json=body, headers=_auth())

    assert response.status_code == 400
    assert _message(response).startswith("requests[2] (custom_id 'same')")
    assert "not unique" in _message(response)
    assert store.created == []


async def test_every_request_must_name_the_batchs_one_url(client, store):
    body = {"requests": [_request("a"), _request("b", url="/v1/embeddings")]}

    response = await client.post("/v1/batches", json=body, headers=_auth())

    assert response.status_code == 400
    assert "custom_id 'b'" in _message(response)
    assert "/v1/chat/completions" in _message(response)


async def test_an_unknown_url_lists_every_target(client, store):
    body = {"requests": [_request("a", url="/v1/completions")]}

    response = await client.post("/v1/batches", json=body, headers=_auth())

    assert response.status_code == 400
    for url in ("/v1/chat/completions", "/v1/embeddings", "/v1/messages", "/v1/responses"):
        assert url in _message(response)


async def test_method_and_url_default_to_a_chat_completion(client, store):
    body = {"requests": [{"custom_id": "a", "body": _chat()}]}

    await _create(client, body=body)

    assert store.created[0]["endpoint"] == "/v1/chat/completions"


async def test_a_streaming_request_is_400(client, store):
    body = {"requests": [_request("a", _chat(stream=True))]}

    response = await client.post("/v1/batches", json=body, headers=_auth())

    assert response.status_code == 400
    assert "custom_id 'a'" in _message(response)
    assert "stream" in _message(response)


@pytest.mark.parametrize(
    "chat",
    [
        {"model": MODEL},
        {"model": MODEL, "messages": "hello"},
        {"model": MODEL, "messages": [{"role": "robot", "content": "hi"}]},
        _chat(temperature=5),
        {"messages": [{"role": "user", "content": "hi"}]},
    ],
    ids=["no-messages", "messages-not-list", "bad-role", "temperature", "no-model"],
)
async def test_an_invalid_chat_body_is_400(client, store, chat):
    body = {"requests": [_request("ok"), _request("bad", chat)]}

    response = await client.post("/v1/batches", json=body, headers=_auth())

    assert response.status_code == 400
    assert _message(response).startswith("requests[1] (custom_id 'bad')")
    assert "not a valid chat completion request" in _message(response)
    assert store.created == []


async def test_an_unknown_model_is_400(client, store):
    body = {"requests": [_request("a", _chat("no-such-model"))]}

    response = await client.post("/v1/batches", json=body, headers=_auth())

    assert response.status_code == 400
    assert _message(response) == "requests[0] (custom_id 'a'): Model 'no-such-model' not found"


async def test_a_model_above_the_callers_role_reads_as_unknown(client, store):
    body = {"requests": [_request("a", _chat(ADMIN_MODEL))]}

    response = await client.post("/v1/batches", json=body, headers=_auth())

    assert response.status_code == 400
    assert _message(response) == f"requests[0] (custom_id 'a'): Model '{ADMIN_MODEL}' not found"
    assert store.created == []


async def test_a_model_the_caller_disabled_reads_as_unknown(client, users, store):
    users["key-alice"]["disabled_models"] = [MODEL]

    response = await client.post("/v1/batches", json=_body(), headers=_auth())

    assert response.status_code == 400
    assert f"Model '{MODEL}' not found" in _message(response)


async def test_an_admin_may_batch_an_admin_model(client, store):
    await _create(client, "key-root", {"requests": [_request("a", _chat(ADMIN_MODEL))]})

    assert store.created[0]["model_ids"] == [ADMIN_MODEL]


@pytest.mark.parametrize("model", ["auto", "auto-fast"])
async def test_auto_models_are_refused_with_their_own_message(client, store, model):
    body = {"requests": [_request("a", _chat(model))]}

    response = await client.post("/v1/batches", json=body, headers=_auth())

    assert response.status_code == 400
    assert "auto models" in _message(response)
    assert store.created == []


async def test_an_auto_name_served_by_auto_mode_is_refused_even_if_registered(
    client, services, monkeypatch
):
    services.router.register_route("auto-legacy", [(_adapter("auto-legacy"), 1.0)])
    body = {"requests": [_request("a", _chat("auto-legacy"))]}

    async def served_by_auto(model, _services):
        return object()

    # Model admission (auto models included) lives with the targets now.
    monkeypatch.setattr(targets, "auto_mode_for", served_by_auto)
    refused = await client.post("/v1/batches", json=body, headers=_auth())

    async def not_served_by_auto(model, _services):
        return None

    monkeypatch.setattr(targets, "auto_mode_for", not_served_by_auto)
    accepted = await client.post("/v1/batches", json=body, headers=_auth())

    assert refused.status_code == 400
    assert "auto models" in _message(refused)
    assert accepted.status_code == 200


async def test_too_many_requests_is_400(client, settings, store):
    settings["BATCH_MAX_REQUESTS"] = "2"

    response = await client.post("/v1/batches", json=_body(3), headers=_auth())

    assert response.status_code == 400
    assert "at most 2 requests" in _message(response)
    assert store.created == []


async def test_metadata_follows_openai_limits(client, store):
    too_many = {f"k{i}": "v" for i in range(17)}
    long_value = {"k": "v" * 513}
    not_a_string = {"k": 1}

    for metadata in (too_many, long_value, not_a_string):
        response = await client.post("/v1/batches", json=_body(metadata=metadata), headers=_auth())
        assert response.status_code == 400, metadata
        assert "metadata" in _message(response)
    assert store.created == []


# --- what is stored, and what is returned --------------------------------------------


async def test_create_stores_every_request_as_sent(client, store, scheduler):
    body = {
        "requests": [
            _request("first", _chat(temperature=0.2, metadata={"session_id": "s"})),
            _request("second", _chat(ALIAS_MODEL)),
        ],
        "metadata": {"project": "eval"},
    }

    batch = await _create(client, body=body)

    (created,) = store.created
    assert created["batch_id"] == batch["id"]
    assert batch["id"].startswith("batch_")
    assert created["user_id"] == "alice"
    assert created["endpoint"] == "/v1/chat/completions"
    assert created["metadata"] == {"project": "eval"}
    assert created["completion_window_seconds"] == 24 * 3600
    assert [item["custom_id"] for item in created["items"]] == ["first", "second"]
    assert [item["request"] for item in created["items"]] == [r["body"] for r in body["requests"]]
    assert scheduler.notified == 1


async def test_an_alias_is_stored_under_its_canonical_model(client, store):
    body = {"requests": [_request("a", _chat(ALIAS_MODEL)), _request("b", _chat(CANONICAL_MODEL))]}

    await _create(client, body=body)

    (created,) = store.created
    assert [item["model_id"] for item in created["items"]] == [CANONICAL_MODEL, CANONICAL_MODEL]
    assert created["items"][0]["request"]["model"] == ALIAS_MODEL
    assert created["model_ids"] == [CANONICAL_MODEL]


async def test_a_batch_may_mix_models(client, store):
    body = {"requests": [_request("a", _chat(MODEL)), _request("b", _chat(ALIAS_MODEL))]}

    await _create(client, body=body)

    assert store.created[0]["model_ids"] == sorted([MODEL, CANONICAL_MODEL])


async def test_the_batch_object_is_openai_shaped(client, settings):
    settings["BATCH_COMPLETION_WINDOW_HOURS"] = "48"

    batch = await _create(client, body=_body(3, metadata={"project": "eval"}))

    assert set(batch) >= OPENAI_BATCH_FIELDS
    assert batch["object"] == "batch"
    assert batch["endpoint"] == "/v1/chat/completions"
    assert batch["completion_window"] == "48h"
    assert batch["expires_at"] - batch["created_at"] == 48 * 3600
    assert batch["request_counts"] == {"total": 3, "completed": 0, "failed": 0}
    assert batch["metadata"] == {"project": "eval"}
    for field in ("errors", "input_file_id", "output_file_id", "error_file_id", "finalizing_at"):
        assert batch[field] is None
    assert batch["in_progress_at"] is None
    assert batch["cost_usd"] == 0.0
    assert batch["error"] is None


async def test_the_key_hash_is_stored_but_never_returned(client, store):
    batch = await _create(client)
    path = f"/v1/batches/{batch['id']}"

    responses = [
        await client.get(path, headers=_auth()),
        await client.get("/v1/batches", headers=_auth()),
        await client.post(f"{path}/cancel", headers=_auth()),
    ]

    assert store.created[0]["key_hash"] == "hash-alice"
    assert "key_hash" not in batch
    assert "user_id" not in batch
    for response in responses:
        assert response.status_code == 200
        assert "hash-alice" not in response.text
        assert "key_hash" not in response.text
        assert "user_id" not in response.text


async def test_an_anonymous_caller_on_an_auth_disabled_gateway_stores_no_key(client, users, store):
    users["anon"] = {"user_id": "anonymous", "role": "admin", "authenticated": False}

    await _create(client, "anon")

    assert store.created[0]["user_id"] == "anonymous"
    assert store.created[0]["key_hash"] is None


async def test_a_caller_at_the_active_batch_limit_gets_429(client, settings, store):
    settings["BATCH_MAX_ACTIVE_PER_USER"] = "2"
    first = await _create(client)
    await _create(client)

    refused = await client.post("/v1/batches", json=_body(), headers=_auth())
    others = await client.post("/v1/batches", json=_body(), headers=_auth("key-bob"))
    await client.post(f"/v1/batches/{first['id']}/cancel", headers=_auth())
    after_cancel = await client.post("/v1/batches", json=_body(), headers=_auth())

    assert refused.status_code == 429
    assert "at most 2" in _message(refused)
    assert others.status_code == 200
    assert after_cancel.status_code == 200


async def test_the_limit_is_checked_again_before_the_insert(client, settings, store, monkeypatch):
    settings["BATCH_MAX_ACTIVE_PER_USER"] = "1"
    counts = iter([0, 1])

    async def racing_count(user_id: str) -> int:
        return next(counts)

    monkeypatch.setattr(store, "count_active_batches", racing_count)

    response = await client.post("/v1/batches", json=_body(), headers=_auth())

    assert response.status_code == 429
    assert store.created == []


async def test_create_works_without_a_running_worker(client, services, store):
    services.batch_scheduler = None

    await _create(client)

    assert len(store.created) == 1


async def test_a_worker_that_cannot_be_woken_does_not_fail_the_create(client, scheduler, store):
    def broken_notify() -> None:
        raise RuntimeError("worker gone")

    scheduler.notify = broken_notify

    await _create(client)

    assert len(store.created) == 1


# --- every target is a standard batch target -------------------------------------

_TARGET_BODIES = {
    "/v1/chat/completions": _chat(),
    "/v1/embeddings": {"model": MODEL, "input": "hello"},
    "/v1/responses": {"model": MODEL, "input": "hello"},
}


@pytest.mark.parametrize("retired_flag", [None, "0", "false", "1"])
@pytest.mark.parametrize("url", sorted(_TARGET_BODIES))
async def test_every_target_is_accepted_without_any_opt_in(
    client, services, settings, store, url, retired_flag
):
    """BATCH_EXPANSION_ENABLED is retired: a value left behind, off or on, changes nothing."""
    services.embedding_adapters = {MODEL: _adapter(MODEL)}
    # An embedding model is admitted only with the access gate the registry records.
    services.router.register_embedding_route(MODEL, [(services.embedding_adapters[MODEL], 1.0)])
    if retired_flag is not None:
        settings["BATCH_EXPANSION_ENABLED"] = retired_flag
    body = {"requests": [_request("only", _TARGET_BODIES[url], url=url)]}

    batch = await _create(client, body=body)

    assert batch["endpoint"] == url
    assert store.created[-1]["endpoint"] == url


@pytest.mark.parametrize("url", sorted(_TARGET_BODIES))
async def test_turning_batches_off_still_refuses_every_target(client, services, settings, url):
    services.embedding_adapters = {MODEL: _adapter(MODEL)}
    settings["BATCH_ENABLED"] = "0"
    body = {"requests": [_request("only", _TARGET_BODIES[url], url=url)]}

    response = await client.post("/v1/batches", json=body, headers=_auth())

    assert response.status_code == 404


async def test_the_min_role_still_applies_to_every_target(client, services, store):
    services.embedding_adapters = {MODEL: _adapter(MODEL)}
    for url, request_body in _TARGET_BODIES.items():
        body = {"requests": [_request("only", request_body, url=url)]}
        response = await client.post("/v1/batches", json=body, headers=_auth("key-carol"))
        assert response.status_code == 403, url
    assert store.created == []


# --- the Messages target -----------------------------------------------------------


def _messages(model: str = MODEL, **extra: Any) -> dict[str, Any]:
    return {
        "model": model,
        "max_tokens": 16,
        "messages": [{"role": "user", "content": "hello"}],
        **extra,
    }


def _messages_batch(*bodies: dict[str, Any], **envelope: Any) -> dict[str, Any]:
    requests = [
        _request(f"m-{i}", body, url="/v1/messages")
        for i, body in enumerate(bodies or [_messages()])
    ]
    return {"requests": requests, **envelope}


def _versioned(version: str, key: str = "key-alice") -> dict[str, str]:
    """The headers of a Messages create: the version travels in ``anthropic-version``."""
    return {**_auth(key), "anthropic-version": version}


@pytest.mark.parametrize("version", ["2023-06-01", "2026-10-21"])
async def test_a_messages_batch_is_created_on_v1_batches_with_its_version(client, store, version):
    response = await client.post("/v1/batches", json=_messages_batch(), headers=_versioned(version))
    assert response.status_code == 200, response.text
    batch = response.json()

    assert batch["object"] == "batch" and batch["endpoint"] == "/v1/messages"
    assert batch["anthropic_version"] == version
    assert batch["id"].startswith("batch_")
    assert store.options["target"] == "messages"
    assert store.options["api_surface"] == "openai_inline"
    assert store.options["protocol_profile"] == version
    # The body is stored as sent: never converted to a chat request.
    assert store.created[0]["items"][0]["request"] == _messages()


async def test_the_version_header_is_read_without_surrounding_whitespace(client, store):
    response = await client.post(
        "/v1/batches", json=_messages_batch(), headers=_versioned("  2026-10-21 ")
    )

    assert response.status_code == 200, response.text
    assert store.options["protocol_profile"] == "2026-10-21"


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({}, "needs this header"),
        ({"anthropic-version": "   "}, "needs this header"),
        ({"anthropic-version": "2099-01-01"}, "'2099-01-01' is not supported"),
    ],
)
async def test_a_messages_batch_needs_a_known_version_header(client, store, headers, expected):
    response = await client.post(
        "/v1/batches", json=_messages_batch(), headers={**_auth(), **headers}
    )

    assert response.status_code == 400
    assert expected in _message(response)
    assert store.created == []


@pytest.mark.parametrize("value", ["2023-06-01", None, ""])
async def test_the_obsolete_body_version_field_is_refused_never_ignored(client, store, value):
    """Even beside a valid header, and even null: the envelope would otherwise drop it."""
    response = await client.post(
        "/v1/batches",
        json=_messages_batch(anthropic_version=value),
        headers=_versioned("2023-06-01"),
    )

    assert response.status_code == 400
    assert "not accepted in the body" in _message(response)
    assert "anthropic-version header" in _message(response)
    assert store.created == []


async def test_a_known_version_the_setting_does_not_allow_cannot_create(client, settings, store):
    settings["BATCH_ANTHROPIC_VERSIONS"] = "2023-06-01"

    response = await client.post(
        "/v1/batches", json=_messages_batch(), headers=_versioned("2026-10-21")
    )

    assert response.status_code == 400
    assert "use one of 2023-06-01" in _message(response)
    assert store.created == []


@pytest.mark.parametrize("url", sorted(_TARGET_BODIES))
async def test_only_a_messages_batch_takes_a_version(client, services, store, url):
    services.embedding_adapters = {MODEL: _adapter(MODEL)}
    services.router.register_embedding_route(MODEL, [(services.embedding_adapters[MODEL], 1.0)])
    body = {"requests": [_request("only", _TARGET_BODIES[url], url=url)]}

    in_header = await client.post("/v1/batches", json=body, headers=_versioned("2023-06-01"))
    in_body = await client.post(
        "/v1/batches", json={**body, "anthropic_version": "2023-06-01"}, headers=_auth()
    )

    assert in_header.status_code == 400
    assert "only a /v1/messages batch takes one" in _message(in_header)
    assert in_body.status_code == 400 and "not accepted in the body" in _message(in_body)
    assert store.created == []
    # A blank header is no header.
    blank = await client.post(
        "/v1/batches", json=body, headers={**_auth(), "anthropic-version": " "}
    )
    assert blank.status_code == 200, blank.text


async def test_a_file_batch_follows_the_same_version_rules_before_the_file_is_read(client, store):
    file_batch = {"input_file_id": "file-x", "completion_window": "24h"}
    missing = await client.post(
        "/v1/batches", json={**file_batch, "endpoint": "/v1/messages"}, headers=_auth()
    )
    on_chat = await client.post(
        "/v1/batches",
        json={**file_batch, "endpoint": "/v1/chat/completions"},
        headers=_versioned("2023-06-01"),
    )
    in_body = await client.post(
        "/v1/batches",
        json={**file_batch, "endpoint": "/v1/messages", "anthropic_version": "2023-06-01"},
        headers=_versioned("2023-06-01"),
    )

    assert missing.status_code == 400 and "needs this header" in _message(missing)
    assert on_chat.status_code == 400 and "only a /v1/messages" in _message(on_chat)
    assert in_body.status_code == 400 and "not accepted in the body" in _message(in_body)
    assert store.created == []


async def test_betas_are_refused_whatever_the_version(client, store):
    headers = {**_versioned("2023-06-01"), "anthropic-beta": "files-api-2025-04-14"}

    beta = await client.post("/v1/batches", json=_messages_batch(), headers=headers)

    assert beta.status_code == 400 and "anthropic-beta" in _message(beta)
    assert store.created == []


async def test_management_needs_no_version_header(client, store):
    created = await client.post(
        "/v1/batches", json=_messages_batch(), headers=_versioned("2023-06-01")
    )
    batch_id = created.json()["id"]

    for method, path in (
        ("GET", "/v1/batches"),
        ("GET", f"/v1/batches/{batch_id}"),
        ("GET", f"/v1/batches/{batch_id}/results"),
        ("POST", f"/v1/batches/{batch_id}/cancel"),
        ("DELETE", f"/v1/batches/{batch_id}"),
    ):
        response = await client.request(method, path, headers=_auth())
        assert response.status_code == 200, (method, path, response.text)


async def test_each_version_applies_its_own_rules_to_every_request(client, store):
    sampled = _messages(temperature=0.5)

    old = await client.post(
        "/v1/batches", json=_messages_batch(sampled), headers=_versioned("2023-06-01")
    )
    new = await client.post(
        "/v1/batches",
        json=_messages_batch(_messages(), sampled),
        headers=_versioned("2026-10-21"),
    )

    assert old.status_code == 200, old.text
    assert new.status_code == 400
    assert "custom_id 'm-1'" in _message(new) and "temperature" in _message(new)
    assert len(store.created) == 1


@pytest.mark.parametrize(
    "extra",
    [
        {"tools": [{"type": "web_search_20250305", "name": "web_search"}]},
        {"mcp_servers": [{"url": "https://mcp.example"}]},
        {"stream": True},
        {"betas": ["x"]},
        {"anthropic_version": "2023-06-01"},
        # A native-only feature on a route only an OpenAI-compatible adapter serves.
        {"thinking": {"type": "enabled", "budget_tokens": 1024}},
    ],
)
async def test_an_unsupported_messages_capability_refuses_the_whole_batch(client, store, extra):
    body = _messages_batch(_messages(), _messages(**extra))

    response = await client.post("/v1/batches", json=body, headers=_versioned("2023-06-01"))

    assert response.status_code == 400
    assert "custom_id 'm-1'" in _message(response)
    assert store.created == []


async def test_a_messages_model_the_caller_may_not_use_reads_as_unknown(client, store):
    body = _messages_batch(_messages(ADMIN_MODEL))

    response = await client.post("/v1/batches", json=body, headers=_versioned("2023-06-01"))

    assert response.status_code == 400
    assert ADMIN_MODEL in _message(response) and "not found" in _message(response)
    assert store.created == []


async def test_messages_and_another_target_cannot_share_a_batch(client, store):
    body = {
        "requests": [
            _request("m", _messages(), url="/v1/messages"),
            _request("c", _chat(), url="/v1/chat/completions"),
        ]
    }

    response = await client.post("/v1/batches", json=body, headers=_versioned("2023-06-01"))

    assert response.status_code == 400
    assert "same url" in _message(response)


def _digest_of(store: FakeBatchStore) -> str:
    return store.options["idempotency"]["digest"]


async def test_the_version_is_part_of_the_idempotency_digest(client, store):
    headers = {**_auth(), "Idempotency-Key": "k"}
    for version in ("2023-06-01", "2026-10-21"):
        response = await client.post(
            "/v1/batches",
            json=_messages_batch(),
            headers={**headers, "anthropic-version": version},
        )
        assert response.status_code == 200, response.text
    first, second = (entry for entry in store.created)
    assert first["items"] == second["items"]
    # Same requests, different version: different digests, so a replay of one
    # is never answered with the other.
    digests = [
        admission.request_digest(
            "openai_inline",
            requests=[
                {"custom_id": "m-0", "method": "POST", "url": "/v1/messages", "body": _messages()}
            ],
            metadata=None,
            anthropic_version=version,
        )
        for version in ("2023-06-01", "2026-10-21")
    ]
    assert digests[0] != digests[1]
    assert _digest_of(store) == digests[1]


async def test_a_non_messages_digest_is_unchanged_by_versions(client, store):
    """Records written before versions existed must still replay after the upgrade."""
    await client.post("/v1/batches", json=_body(), headers={**_auth(), "Idempotency-Key": "k"})

    request = {
        "custom_id": "req-0",
        "method": "POST",
        "url": "/v1/chat/completions",
        "body": _chat(),
    }
    before = json.dumps(
        {"surface": "openai_inline", "requests": [request], "metadata": None},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    assert _digest_of(store) == hashlib.sha256(before.encode()).hexdigest()


def _legacy_message_batch(store: FakeBatchStore, owner: str = "alice") -> str:
    """A batch the retired Message Batches API stored, as it sits in the database."""
    batch_id = "msgbatch_legacy0001"
    created = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
    store.batches[batch_id] = {
        "id": batch_id,
        "user_id": owner,
        "key_hash": f"hash-{owner}",
        "endpoint": "/v1/messages",
        "status": "completed",
        "model_ids": [MODEL],
        "request_count": 3,
        "completed_count": 2,
        "failed_count": 1,
        "metadata": None,
        "error": None,
        "cost_usd": 0.25,
        "created_at": created,
        "in_progress_at": created,
        "completed_at": created + timedelta(minutes=3),
        "failed_at": None,
        "expired_at": None,
        "cancelling_at": None,
        "cancelled_at": None,
        "expires_at": created + timedelta(hours=24),
        "paused_until": None,
        "api_surface": "anthropic",
        "target": "messages",
        "schema_version": 2,
        "phase": "sealed",
        "protocol_profile": "2023-06-01",
    }
    store.items[batch_id] = []
    return batch_id


async def test_a_legacy_message_batch_is_read_listed_and_deleted_through_v1_batches(client, store):
    batch_id = _legacy_message_batch(store)

    got = (await client.get(f"/v1/batches/{batch_id}", headers=_auth())).json()
    listed = (await client.get("/v1/batches", headers=_auth())).json()["data"]
    other = await client.get(f"/v1/batches/{batch_id}", headers=_auth("key-bob"))

    assert got["object"] == "batch" and got["status"] == "completed"
    assert got["endpoint"] == "/v1/messages" and got["anthropic_version"] == "2023-06-01"
    assert got["request_counts"] == {"total": 3, "completed": 2, "failed": 1}
    assert [row["id"] for row in listed] == [batch_id]
    assert other.status_code == 404
    deleted = await client.delete(f"/v1/batches/{batch_id}", headers=_auth())
    assert deleted.json() == {"id": batch_id, "object": "batch.deleted", "deleted": True}


async def test_a_non_messages_batch_shows_no_version(client):
    batch = await _create(client)
    assert batch["anthropic_version"] is None


# --- ownership --------------------------------------------------------------------


async def test_another_users_batch_reads_as_unknown(client, store):
    batch = await _create(client, "key-alice")
    path = f"/v1/batches/{batch['id']}"

    responses = [
        await client.get(path, headers=_auth("key-bob")),
        await client.get(f"{path}/results", headers=_auth("key-bob")),
        await client.post(f"{path}/cancel", headers=_auth("key-bob")),
        await client.delete(path, headers=_auth("key-bob")),
    ]

    assert [r.status_code for r in responses] == [404, 404, 404, 404]
    assert store.batches[batch["id"]]["status"] == "validating"
    listed = await client.get("/v1/batches", headers=_auth("key-bob"))
    assert listed.json()["data"] == []


async def test_an_admin_may_read_cancel_and_delete_any_batch(client, store):
    batch = await _create(client, "key-alice")
    path = f"/v1/batches/{batch['id']}"

    read = await client.get(path, headers=_auth("key-root"))
    results = await client.get(f"{path}/results", headers=_auth("key-root"))
    cancelled = await client.post(f"{path}/cancel", headers=_auth("key-root"))
    listed = await client.get("/v1/batches", headers=_auth("key-root"))
    deleted = await client.delete(path, headers=_auth("key-root"))

    assert read.json()["id"] == batch["id"]
    assert len(results.json()["data"]) == 1
    assert cancelled.json()["status"] == "cancelled"
    # Listing shows the caller's own batches, an administrator's included.
    assert listed.json()["data"] == []
    assert deleted.status_code == 200
    assert batch["id"] not in store.batches


async def test_an_unknown_batch_is_404(client):
    response = await client.get("/v1/batches/batch_missing", headers=_auth())

    assert response.status_code == 404
    assert _message(response) == "Batch 'batch_missing' not found"


# --- paging -----------------------------------------------------------------------


async def test_results_page_in_submission_order(client, store):
    batch = await _create(client, body=_body(5))

    first = await client.get(f"/v1/batches/{batch['id']}/results?limit=2", headers=_auth())
    page = first.json()
    second = await client.get(
        f"/v1/batches/{batch['id']}/results?limit=2&after={page['last_id']}", headers=_auth()
    )
    third = await client.get(
        f"/v1/batches/{batch['id']}/results?limit=2&after={second.json()['last_id']}",
        headers=_auth(),
    )

    assert page["object"] == "list"
    assert [r["id"] for r in page["data"]] == ["batch_req_1", "batch_req_2"]
    assert [r["custom_id"] for r in page["data"]] == ["req-0", "req-1"]
    assert (page["first_id"], page["last_id"], page["has_more"]) == (
        "batch_req_1",
        "batch_req_2",
        True,
    )
    assert [r["custom_id"] for r in second.json()["data"]] == ["req-2", "req-3"]
    assert second.json()["has_more"] is True
    assert [r["custom_id"] for r in third.json()["data"]] == ["req-4"]
    assert third.json()["has_more"] is False


async def test_results_carry_each_requests_outcome(client, store):
    batch = await _create(client, body=_body(3))
    done, failed, _queued = store.items[batch["id"]]
    done.update(
        status="completed",
        status_code=200,
        request_id="req_abc",
        response={"id": "chatcmpl-1", "object": "chat.completion", "choices": []},
    )
    failed.update(
        status="failed",
        status_code=400,
        error={"code": "invalid_request", "message": "bad", "status": 400},
    )

    response = await client.get(f"/v1/batches/{batch['id']}/results", headers=_auth())

    first, second, third = response.json()["data"]
    assert first == {
        "id": "batch_req_1",
        "custom_id": "req-0",
        "status": "completed",
        "response": {
            "status_code": 200,
            "request_id": "req_abc",
            "body": {"id": "chatcmpl-1", "object": "chat.completion", "choices": []},
        },
        "error": None,
    }
    assert second["status"] == "failed"
    assert second["response"] is None
    assert second["error"] == {"code": "invalid_request", "message": "bad", "status": 400}
    assert (third["status"], third["response"], third["error"]) == ("queued", None, None)


@pytest.mark.parametrize("after", ["12", "req_12", "batch_req_x", "batch_req_" + "9" * 19])
async def test_a_malformed_results_cursor_is_400(client, after):
    batch = await _create(client)

    response = await client.get(
        f"/v1/batches/{batch['id']}/results", params={"after": after}, headers=_auth()
    )

    assert response.status_code == 400
    assert "batch_req_" in _message(response)


@pytest.mark.parametrize("limit", [0, 1001])
async def test_results_limit_is_bounded(client, limit):
    batch = await _create(client)

    response = await client.get(
        f"/v1/batches/{batch['id']}/results", params={"limit": limit}, headers=_auth()
    )

    assert response.status_code == 422


async def test_batches_list_newest_first_a_page_at_a_time(client):
    ids = [(await _create(client))["id"] for _ in range(3)]

    first = await client.get("/v1/batches?limit=2", headers=_auth())
    second = await client.get(
        f"/v1/batches?limit=2&after={first.json()['last_id']}", headers=_auth()
    )

    assert first.json()["object"] == "list"
    assert [b["id"] for b in first.json()["data"]] == [ids[2], ids[1]]
    assert first.json()["has_more"] is True
    assert (first.json()["first_id"], first.json()["last_id"]) == (ids[2], ids[1])
    assert [b["id"] for b in second.json()["data"]] == [ids[0]]
    assert second.json()["has_more"] is False


@pytest.mark.parametrize("limit", [0, 101])
async def test_list_limit_is_bounded(client, limit):
    response = await client.get("/v1/batches", params={"limit": limit}, headers=_auth())

    assert response.status_code == 422


async def test_an_empty_list(client):
    response = await client.get("/v1/batches", headers=_auth())

    assert response.json() == {
        "object": "list",
        "data": [],
        "has_more": False,
        "first_id": None,
        "last_id": None,
    }


# --- cancel and delete ----------------------------------------------------------------


async def test_cancel_stops_queued_requests(client, store):
    batch = await _create(client, body=_body(2))

    response = await client.post(f"/v1/batches/{batch['id']}/cancel", headers=_auth())

    cancelled = response.json()
    assert cancelled["status"] == "cancelled"
    assert cancelled["cancelling_at"] is not None
    assert cancelled["cancelled_at"] is not None
    assert [i["status"] for i in store.items[batch["id"]]] == ["cancelled", "cancelled"]


async def test_cancel_waits_for_running_requests(client, store):
    batch = await _create(client, body=_body(2))
    store.items[batch["id"]][0]["status"] = "running"

    response = await client.post(f"/v1/batches/{batch['id']}/cancel", headers=_auth())

    assert response.json()["status"] == "cancelling"
    assert response.json()["cancelled_at"] is None


async def test_cancelling_a_finished_batch_returns_it_unchanged(client, store):
    batch = await _create(client)
    store.batches[batch["id"]]["status"] = "completed"

    response = await client.post(f"/v1/batches/{batch['id']}/cancel", headers=_auth())

    assert response.status_code == 200
    assert response.json()["status"] == "completed"


async def test_delete_removes_the_batch(client, store):
    batch = await _create(client)
    path = f"/v1/batches/{batch['id']}"

    deleted = await client.delete(path, headers=_auth())
    again = await client.delete(path, headers=_auth())

    assert deleted.json() == {"id": batch["id"], "object": "batch.deleted", "deleted": True}
    assert batch["id"] not in store.batches
    assert again.status_code == 404
    assert (await client.get(path, headers=_auth())).status_code == 404


async def test_delete_is_409_while_an_upstream_request_lease_is_live(client, store, monkeypatch):
    batch = await _create(client)
    path = f"/v1/batches/{batch['id']}"

    async def delete_in_progress(batch_id, *, user_id=None, **scope):
        raise BatchDeleteInProgress(batch_id)

    monkeypatch.setattr(store, "delete_batch", delete_in_progress)

    response = await client.delete(path, headers=_auth())

    assert response.status_code == 409
    assert _message(response) == (
        f"Batch '{batch['id']}' has a running request; cancel the batch and wait for its "
        "running requests to finish before deleting it"
    )
    assert batch["id"] in store.batches
