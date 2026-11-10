"""Routes that take a large body read it only once the caller is authenticated.

FastAPI reads and decodes a declared body model before any dependency runs,
authentication included, so an anonymous caller could make the server decode
up to the body limit (32 MiB: seconds of event-loop time and hundreds of MiB)
before the 401. These routes read their body through
``serving.servers.request_body`` instead, and answer an invalid body exactly as
a declared one did.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.exceptions import RequestValidationError
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel

from serving.servers.request_body import json_body_after, validated_json_body

MiB = 1024 * 1024


def _anonymous() -> None:
    raise HTTPException(status_code=401, detail="Missing API key")


class _Probe:
    """A request body that records how much of it the application read."""

    def __init__(self, size: int) -> None:
        self.size = size
        self.read = 0

    async def __aiter__(self):
        chunk = b"[" + b"0," * (64 * 1024 // 2 - 1)
        while self.read < self.size:
            self.read += len(chunk)
            yield chunk


async def _post(app: FastAPI, path: str, **kwargs: Any) -> Any:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        return await client.post(path, **kwargs)


# --- the helper, against a declared body model --------------------------------


class _Body(BaseModel):
    name: str
    count: int = 1


def _pair() -> FastAPI:
    """One route declaring the model, one reading it through the helper."""
    from serving.servers.routers.anthropic_messages import (
        anthropic_aware_validation_exception_handler,
    )

    app = FastAPI()
    app.add_exception_handler(RequestValidationError, anthropic_aware_validation_exception_handler)

    async def caller(authorization: str | None = Header(None)) -> str:
        if authorization != "Bearer ok":
            raise HTTPException(status_code=401, detail="no")
        return "caller"

    @app.post("/declared")
    async def declared(body: _Body, _caller: str = Depends(caller)) -> dict:
        return body.model_dump()

    @app.post("/deferred")
    async def deferred(body: _Body = Depends(json_body_after(caller, _Body))) -> dict:
        return body.model_dump()

    return app


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "headers"),
    [
        (b'{"name": "x", "count": 2}', {"content-type": "application/json"}),
        (b'{"name": "x"', {"content-type": "application/json"}),
        (b'{"count": "many"}', {"content-type": "application/json"}),
        (b"", {"content-type": "application/json"}),
        (b"null", {"content-type": "application/json"}),
        (b"[1, 2]", {"content-type": "application/json"}),
        (b'{"name": "x"}', {"content-type": "text/plain"}),
        (b'{"name": "x"}', {}),
        (b'{"name": "x"}', {"content-type": "application/vnd.api+json"}),
    ],
)
async def test_the_helper_answers_as_a_declared_body_does(content: bytes, headers: dict) -> None:
    app = _pair()
    auth = {"authorization": "Bearer ok", **headers}
    declared = await _post(app, "/declared", content=content, headers=auth)
    deferred = await _post(app, "/deferred", content=content, headers=auth)
    assert deferred.status_code == declared.status_code
    assert deferred.json() == declared.json()


@pytest.mark.asyncio
async def test_the_helper_reads_nothing_before_authentication() -> None:
    app = _pair()
    probe = _Probe(4 * MiB)
    response = await _post(
        app, "/deferred", content=probe, headers={"content-type": "application/json"}
    )
    assert response.status_code == 401
    assert probe.read == 0


@pytest.mark.asyncio
async def test_a_declared_body_is_read_before_authentication() -> None:
    """Why the helper exists: the declared route reads every byte first."""
    app = _pair()
    probe = _Probe(4 * MiB)
    response = await _post(
        app, "/declared", content=probe, headers={"content-type": "application/json"}
    )
    assert response.status_code in (401, 422)
    assert probe.read >= 4 * MiB


@pytest.mark.asyncio
async def test_a_decoded_body_is_not_decoded_again() -> None:
    from fastapi import Request

    decodes = 0

    class _Counting(Request):
        async def json(self) -> Any:
            nonlocal decodes
            if not hasattr(self, "_json"):
                decodes += 1
            return await super().json()

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": [(b"content-type", b"application/json")],
        "query_string": b"",
    }
    sent = False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": b'{"name": "n"}', "more_body": False}

    request = _Counting(scope, receive)
    await request.json()  # what the concurrency gate does first
    body = await validated_json_body(request, _Body)
    assert body.name == "n"
    assert decodes == 1


# --- the routes ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_embeddings_reads_no_body_before_authentication() -> None:
    from serving.servers.auth import verify_api_key
    from serving.servers.routers import embeddings

    app = FastAPI()
    app.include_router(embeddings.router)
    app.dependency_overrides[verify_api_key] = _anonymous
    probe = _Probe(8 * MiB)
    response = await _post(
        app, "/v1/embeddings", content=probe, headers={"content-type": "application/json"}
    )
    assert response.status_code == 401
    assert probe.read == 0


def _embeddings_app() -> tuple[FastAPI, list[dict[str, Any]]]:
    from types import SimpleNamespace

    from serving.config.runtime_settings import get_runtime_settings
    from serving.servers.auth import verify_api_key
    from serving.servers.concurrency import enforce_user_concurrency
    from serving.servers.deps import (
        get_completions_logger,
        get_embedding_adapters,
        get_log_store,
        get_model_visibility_resolver,
        get_operational_store,
        get_router,
    )
    from serving.servers.routers import embeddings
    from serving.servers.routers.anthropic_messages import (
        anthropic_aware_validation_exception_handler,
    )

    logged: list[dict[str, Any]] = []

    class _Adapter:
        config = SimpleNamespace(provider="p", pricing={"prompt": "0", "completion": "0"})

        async def embeddings(self, input_data: Any, **params: Any) -> dict[str, Any]:
            return {
                "object": "list",
                "model": "emb",
                "data": [{"object": "embedding", "embedding": [0.5], "index": 0}],
                "usage": {"prompt_tokens": 1, "total_tokens": 1},
            }

    class _Logger:
        def schedule_log(self, request_id: str, row: dict[str, Any]) -> None:
            logged.append(row)

    route = SimpleNamespace(
        adapters=[(SimpleNamespace(config=SimpleNamespace(id="emb")), 1.0)],
        published=True,
        required_role="free",
        admin_only=False,
    )
    app = FastAPI()
    app.add_exception_handler(RequestValidationError, anthropic_aware_validation_exception_handler)
    app.include_router(embeddings.router)
    overrides = app.dependency_overrides
    overrides[verify_api_key] = lambda: {"user_id": "u", "role": "free", "authenticated": True}
    overrides[enforce_user_concurrency] = lambda: None
    overrides[get_embedding_adapters] = lambda: {"emb": _Adapter()}
    overrides[get_log_store] = lambda: object()
    overrides[get_completions_logger] = lambda: _Logger()
    overrides[get_operational_store] = lambda: None
    overrides[get_runtime_settings] = lambda: None
    overrides[get_router] = lambda: SimpleNamespace(embedding_routes={"emb": route})
    overrides[get_model_visibility_resolver] = lambda: None
    return app, logged


@pytest.mark.asyncio
async def test_embeddings_still_serves_a_valid_body_and_its_session() -> None:
    app, logged = _embeddings_app()
    response = await _post(
        app,
        "/v1/embeddings",
        json={"model": "emb", "input": "hello", "metadata": {"session_id": "s-1"}},
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"][0]["embedding"] == [0.5]
    assert logged[0]["prompt"] == "hello"
    # The undeclared field is still read from the body as sent.
    assert logged[0]["metadata"]["session_id"] == "s-1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "loc"),
    [
        (b'{"model": "emb", "input": ', "body"),
        (b'{"model": "emb"}', "input"),
        (b"", "body"),
    ],
)
async def test_embeddings_keeps_its_422_for_an_invalid_body(content: bytes, loc: str) -> None:
    app, logged = _embeddings_app()
    response = await _post(
        app, "/v1/embeddings", content=content, headers={"content-type": "application/json"}
    )
    assert response.status_code == 422
    assert loc in response.text
    assert logged == []


@pytest.mark.asyncio
async def test_the_playground_reads_no_body_before_the_role_check() -> None:
    from serving.servers.deps import get_current_user
    from serving.servers.routers import playground

    app = FastAPI()
    app.include_router(playground.router)

    def free_user() -> dict[str, Any]:
        return {"user_id": "u", "role": "free"}

    app.dependency_overrides[get_current_user] = free_user
    probe = _Probe(8 * MiB)
    response = await _post(
        app,
        "/internal/playground/chat",
        content=probe,
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 403
    assert probe.read == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/admin/config/restore", "/admin/config/restore/preview"])
async def test_a_restore_reads_no_body_before_the_administrator_check(path: str) -> None:
    from serving.servers.deps import verify_admin_access
    from serving.servers.routers.admin import config_backup

    app = FastAPI()
    app.include_router(config_backup.router)
    app.dependency_overrides[verify_admin_access] = _anonymous
    probe = _Probe(8 * MiB)
    response = await _post(app, path, content=probe, headers={"content-type": "application/json"})
    assert response.status_code == 401
    assert probe.read == 0
