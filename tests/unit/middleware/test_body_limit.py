"""Tests for the request body limit middleware (REQUEST_MAX_BODY_MB)."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel

from serving.config.app_config_registry import static_entry
from serving.servers.middleware.body_limit import (
    ADMIN_MAX_BODY_BYTES,
    AUTHENTICATED_BODY_PATHS,
    CONTROL_MAX_BODY_BYTES,
    DEFAULT_MAX_BODY_MB,
    EXEMPT_PATHS,
    MAX_BODY_SETTING,
    RequestBodyLimitMiddleware,
    configured_max_body_bytes,
    control_limit,
)

LIMIT = 1024


class _Item(BaseModel):
    value: str


def _build_app(limit: int | None = LIMIT) -> tuple[FastAPI, dict[str, Any]]:
    seen: dict[str, Any] = {"calls": 0}
    app = FastAPI()
    if limit is None:
        app.add_middleware(RequestBodyLimitMiddleware)
    else:
        app.add_middleware(RequestBodyLimitMiddleware, max_body_bytes=limit)

    @app.post("/v1/chat/completions")
    async def echo(request: Request) -> dict[str, int]:
        seen["calls"] += 1
        body = await request.body()
        seen["body"] = body
        return {"received": len(body)}

    @app.post("/v1/messages")
    async def messages(request: Request) -> dict[str, int]:
        seen["calls"] += 1
        return {"received": len(await request.body())}

    @app.post("/forgiving")
    async def forgiving(request: Request) -> JSONResponse:
        # Mirrors the handlers that turn any read failure into a 400.
        seen["calls"] += 1
        try:
            await request.json()
        except Exception:
            seen["swallowed"] = True
            return JSONResponse({"error": "bad body"}, status_code=400)
        return JSONResponse({"ok": True})

    @app.post("/typed")
    async def typed(item: _Item) -> dict[str, str]:
        seen["calls"] += 1
        return {"value": item.value}

    for path in EXEMPT_PATHS:

        @app.post(path)
        async def upload(request: Request) -> dict[str, int]:
            return {"received": len(await request.body())}

    @app.get("/stream")
    async def stream() -> StreamingResponse:
        async def generate():
            for i in range(5):
                yield f"data: chunk-{i}\n\n"
                await asyncio.sleep(0.02)

        return StreamingResponse(generate(), media_type="text/event-stream")

    return app, seen


async def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_a_body_within_the_limit_reaches_the_handler_intact() -> None:
    app, seen = _build_app()
    payload = b"x" * LIMIT
    async with await _client(app) as client:
        response = await client.post("/v1/chat/completions", content=payload)
    assert response.status_code == 200
    assert response.json() == {"received": LIMIT}
    assert seen["body"] == payload


@pytest.mark.asyncio
async def test_a_declared_length_over_the_limit_is_refused_before_reading() -> None:
    app, seen = _build_app()
    reads: list[dict[str, Any]] = []
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        reads.append({})
        return {"type": "http.request", "body": b"x" * (LIMIT + 1), "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/chat/completions",
        "raw_path": b"/v1/chat/completions",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"content-length", str(LIMIT + 1).encode())],
        "client": ("127.0.0.1", 1),
        "server": ("test", 80),
    }
    await app(scope, receive, send)

    assert reads == []  # not a byte of the body was read
    assert seen["calls"] == 0  # the application never ran
    start = sent[0]
    assert start["status"] == 413
    headers = dict(start["headers"])
    assert headers[b"connection"] == b"close"
    body = json.loads(sent[1]["body"])
    assert body["error"]["code"] == 413
    assert body["error"]["type"] == "request_too_large"
    assert "1024 bytes" in body["error"]["message"]


@pytest.mark.asyncio
async def test_a_chunked_body_is_cut_off_once_it_passes_the_limit() -> None:
    app, seen = _build_app()
    produced: list[int] = []

    async def chunks():
        for i in range(10):
            produced.append(i)
            yield b"y" * 512

    async with await _client(app) as client:
        response = await client.post("/v1/chat/completions", content=chunks())

    assert response.status_code == 413
    assert response.headers["connection"] == "close"
    # 512 + 512 is the limit; the third chunk passes it and nothing after it is read.
    assert produced == [0, 1, 2]
    assert "body" not in seen


@pytest.mark.asyncio
async def test_a_handler_that_swallows_the_disconnect_cannot_replace_the_413() -> None:
    app, seen = _build_app()

    async def chunks():
        for _ in range(4):
            yield b"z" * 512

    async with await _client(app) as client:
        response = await client.post("/forgiving", content=chunks())

    assert seen.get("swallowed") is True
    assert response.status_code == 413


@pytest.mark.asyncio
async def test_a_typed_body_route_answers_413_not_400() -> None:
    app, _ = _build_app()

    async def chunks():
        yield b'{"value": "'
        for _ in range(4):
            yield b"v" * 512
        yield b'"}'

    async with await _client(app) as client:
        response = await client.post(
            "/typed", content=chunks(), headers={"content-type": "application/json"}
        )
    assert response.status_code == 413


@pytest.mark.asyncio
async def test_anthropic_surfaces_get_an_anthropic_error() -> None:
    app, _ = _build_app()
    async with await _client(app) as client:
        response = await client.post("/v1/messages", content=b"m" * (LIMIT + 1))
    assert response.status_code == 413
    assert response.json() == {
        "type": "error",
        "error": {
            "type": "request_too_large",
            "message": "Request body exceeds the size limit of 1024 bytes",
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("path", sorted(EXEMPT_PATHS))
async def test_batch_uploads_keep_their_own_limit(path: str) -> None:
    app, _ = _build_app()
    async with await _client(app) as client:
        response = await client.post(path, content=b"u" * (LIMIT * 4))
    assert response.status_code == 200
    assert response.json() == {"received": LIMIT * 4}


@pytest.mark.asyncio
async def test_streaming_responses_are_not_buffered() -> None:
    # Raw ASGI: httpx's transport collects a response before returning it.
    app, _ = _build_app()
    events: list[tuple[float, dict[str, Any]]] = []
    request_sent = False

    async def receive() -> dict[str, Any]:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.Event().wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        events.append((time.perf_counter(), message))

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/stream",
        "raw_path": b"/stream",
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "client": ("127.0.0.1", 1),
        "server": ("test", 80),
    }
    await app(scope, receive, send)

    bodies = [
        (at, message["body"])
        for at, message in events
        if message["type"] == "http.response.body" and message.get("body")
    ]
    # Each chunk is forwarded as it is produced, 20 ms apart, not collected.
    assert [body for _, body in bodies] == [f"data: chunk-{i}\n\n".encode() for i in range(5)]
    assert bodies[-1][0] - bodies[0][0] >= 0.06


@pytest.mark.asyncio
async def test_the_limit_follows_the_setting_on_every_request(monkeypatch) -> None:
    app, _ = _build_app(limit=None)
    payload = b"s" * (2 * 1024 * 1024)
    async with await _client(app) as client:
        monkeypatch.setenv(MAX_BODY_SETTING, "1")
        refused = await client.post("/v1/chat/completions", content=payload)
        monkeypatch.setenv(MAX_BODY_SETTING, "3")
        accepted = await client.post("/v1/chat/completions", content=payload)
    assert refused.status_code == 413
    assert "1 MiB" in refused.json()["error"]["message"]
    assert accepted.status_code == 200


@pytest.mark.parametrize("raw", [None, "", "  ", "abc", "0", "-5", "1.5"])
def test_a_missing_or_invalid_setting_keeps_the_default(monkeypatch, raw: str | None) -> None:
    if raw is None:
        monkeypatch.delenv(MAX_BODY_SETTING, raising=False)
    else:
        monkeypatch.setenv(MAX_BODY_SETTING, raw)
    assert configured_max_body_bytes() == DEFAULT_MAX_BODY_MB * 1024 * 1024


def test_the_registered_default_is_the_middleware_default() -> None:
    entry = static_entry(MAX_BODY_SETTING)
    assert entry is not None
    assert entry.default == str(DEFAULT_MAX_BODY_MB)
    assert entry.minimum == 1


def test_the_application_installs_the_limit_innermost() -> None:
    from serving.servers.app import create_app
    from serving.servers.middleware.cors import SettingsCORSMiddleware

    middleware = [entry.cls for entry in create_app().user_middleware]
    # user_middleware lists the outermost first: the limit sits inside CORS
    # (so its 413 carries CORS headers) and inside every other middleware.
    assert middleware[-1] is RequestBodyLimitMiddleware
    assert middleware.index(SettingsCORSMiddleware) < middleware.index(RequestBodyLimitMiddleware)


# --- control routes -----------------------------------------------------------
#
# FastAPI decodes a declared body model before any dependency runs, so on a
# control route an anonymous caller's body is decoded before the 401. They get
# a small limit of their own.


def _control_app(paths: list[str]) -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestBodyLimitMiddleware)

    for path in paths:

        @app.post(path)
        async def echo(request: Request) -> dict[str, int]:
            return {"received": len(await request.body())}

    return app


CONTROL_ROUTES = [
    "/auth/login",
    "/user/api-keys/hyi-abcdefgh/reveal",
    "/user/profile",
    "/internal/agent-grants",
    "/v1/identity/code",
    "/v1/batch_webhook",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", CONTROL_ROUTES)
async def test_a_control_route_refuses_a_body_over_its_small_limit(path: str) -> None:
    app = _control_app([path])
    async with await _client(app) as client:
        refused = await client.post(path, content=b"x" * (CONTROL_MAX_BODY_BYTES + 1))
        accepted = await client.post(path, content=b"x" * CONTROL_MAX_BODY_BYTES)
    assert refused.status_code == 413
    assert refused.json()["error"]["type"] == "request_too_large"
    assert str(CONTROL_MAX_BODY_BYTES) in refused.json()["error"]["message"]
    assert accepted.json() == {"received": CONTROL_MAX_BODY_BYTES}


@pytest.mark.asyncio
async def test_a_control_route_reads_no_more_than_its_limit_of_a_chunked_body() -> None:
    app = _control_app(["/auth/login"])
    produced: list[int] = []

    async def chunks():
        for i in range(64):
            produced.append(i)
            yield b"c" * (64 * 1024)

    async with await _client(app) as client:
        response = await client.post("/auth/login", content=chunks())
    assert response.status_code == 413
    # 4 chunks are the limit; the fifth passes it and nothing after it is read.
    assert produced == [0, 1, 2, 3, 4]


@pytest.mark.asyncio
async def test_administration_gets_a_larger_limit_than_other_control_routes() -> None:
    app = _control_app(["/admin/site-updates"])
    async with await _client(app) as client:
        accepted = await client.post("/admin/site-updates", content=b"a" * ADMIN_MAX_BODY_BYTES)
        refused = await client.post(
            "/admin/site-updates", content=b"a" * (ADMIN_MAX_BODY_BYTES + 1)
        )
    assert accepted.status_code == 200
    assert refused.status_code == 413


@pytest.mark.asyncio
@pytest.mark.parametrize("path", sorted(AUTHENTICATED_BODY_PATHS))
async def test_routes_that_read_after_authentication_keep_the_general_limit(path: str) -> None:
    app = _control_app([path])
    size = 2 * 1024 * 1024
    async with await _client(app) as client:
        response = await client.post(path, content=b"b" * size)
    assert response.status_code == 200
    assert response.json() == {"received": size}


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/v1/chat/completions", None),
        ("/v1/embeddings", None),
        ("/v1/messages", None),
        ("/v1/qdrant/collections/c/points", None),
        ("/v1/identity", None),
        ("/v1/identity/token", CONTROL_MAX_BODY_BYTES),
        ("/userinfo", None),
        ("/user/batches", CONTROL_MAX_BODY_BYTES),
        ("/admin/config", ADMIN_MAX_BODY_BYTES),
        ("/admin/config/restore", None),
        ("/internal/playground/chat", None),
        ("/internal/playground/models", CONTROL_MAX_BODY_BYTES),
    ],
)
def test_which_paths_are_control_routes(path: str, expected: int | None) -> None:
    assert control_limit(path) == expected


@pytest.mark.asyncio
async def test_a_control_limit_never_raises_the_general_one() -> None:
    app = FastAPI()
    app.add_middleware(RequestBodyLimitMiddleware, max_body_bytes=LIMIT)

    @app.post("/auth/login")
    async def login(request: Request) -> dict[str, int]:
        return {"received": len(await request.body())}

    async with await _client(app) as client:
        response = await client.post("/auth/login", content=b"l" * (LIMIT + 1))
    assert response.status_code == 413


@pytest.mark.asyncio
async def test_every_body_model_route_outside_the_limits_reads_after_authentication() -> None:
    """A route that declares a body model must be a control route.

    FastAPI decodes a declared body before the route's authentication runs; only
    the control limit keeps that small. A route that takes a large body reads it
    after authentication instead (serving.servers.request_body).
    """
    from fastapi.routing import APIRoute, _IncludedRouter

    from serving.servers.app import create_app

    def routes(entries):
        for entry in entries:
            if isinstance(entry, _IncludedRouter):
                yield from entry.effective_route_contexts()
            elif isinstance(entry, APIRoute):
                yield entry

    uncapped = sorted(
        route.path
        for route in routes(create_app().routes)
        if route.body_field is not None
        and control_limit(route.path.rstrip("/")) is None
        and route.path not in EXEMPT_PATHS
    )
    assert uncapped == []
