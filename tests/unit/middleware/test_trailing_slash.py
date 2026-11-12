"""A trailing slash on the entry-split prefixes, answered as the console answers it.

Behind the console, ``next.config.js`` ``redirects()`` sends ``/v1/models/`` to
the bare path ``/v1/models`` with a 308. Sent straight to the gateway by an
entry, the same request used to get Starlette's 307 to an absolute URL built
from the ``Host`` and scheme the gateway saw (``http://<whatever Host>``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from serving.servers.middleware.body_limit import RequestBodyLimitMiddleware
from serving.servers.middleware.trailing_slash import (
    ENTRY_SPLIT_PREFIXES,
    TrailingSlashRedirectMiddleware,
    slashless_location,
)

MANIFEST = Path(__file__).resolve().parents[3] / "deploy" / "public-paths.json"


def _scope(path: str, raw_path: bytes | None = None, query: bytes = b"") -> dict[str, Any]:
    return {
        "type": "http",
        "path": path,
        "raw_path": raw_path if raw_path is not None else path.encode(),
        "query_string": query,
    }


def _app() -> FastAPI:
    """Routes under and outside the split prefixes, behind the gateway's two inner layers."""
    app = FastAPI()
    app.add_middleware(RequestBodyLimitMiddleware)
    app.add_middleware(TrailingSlashRedirectMiddleware)

    @app.post("/v1/chat/completions")
    async def echo(request: Request):
        return {"received": json.loads(await request.body())}

    @app.get("/v1/models")
    async def models():
        return {"object": "list", "data": []}

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    return app


def _client(app: Any, **kwargs: Any) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://backend:8080", **kwargs)


def test_the_prefixes_are_the_manifests_entry_split_paths() -> None:
    manifest = json.loads(MANIFEST.read_text())
    split = [entry for entry in manifest["paths"] if entry["entry_split"]]
    assert all(entry["match"] == "prefix" for entry in split)
    assert tuple(entry["path"] for entry in split) == ENTRY_SPLIT_PREFIXES


@pytest.mark.parametrize(
    ("path", "query", "location"),
    [
        ("/v1/models/", b"", b"/v1/models"),
        ("/v1/models/", b"a=1&b=2", b"/v1/models?a=1&b=2"),
        ("/v1/chat/completions/", b"", b"/v1/chat/completions"),
        # One slash per redirect, as the console does.
        ("/v1/models//", b"", b"/v1/models/"),
        ("/v1//", b"", b"/v1/"),
        ("/v1/", b"", b"/v1"),
        ("/anthropic/", b"", b"/anthropic"),
        ("/anthropic/v1/messages/", b"", b"/anthropic/v1/messages"),
        ("/v1/no-such-route/", b"", b"/v1/no-such-route"),
    ],
)
def test_a_split_path_with_a_trailing_slash_goes_to_the_bare_path(
    path: str, query: bytes, location: bytes
) -> None:
    assert slashless_location(_scope(path, query=query)) == location


def test_the_location_keeps_the_path_as_it_was_sent() -> None:
    scope = _scope("/v1/files/a b/", raw_path=b"/v1/files/a%20b/")
    assert slashless_location(scope) == b"/v1/files/a%20b"


@pytest.mark.parametrize(
    ("path", "raw_path"),
    [
        ("/v1/models", None),  # no slash to remove
        ("/", None),
        ("/health/", None),  # not a split prefix: Starlette's redirect as before
        ("/admin/config/", None),
        ("/v10/", None),  # a different first segment
        ("/anthropicx/", None),
        ("//v1/models/", None),  # a client would read `//v1` as a host
        ("/\\v1/", None),
        ("/v1/../openapi.json/", b"/v1/%2e%2e/openapi.json/"),
        ("/v1/./models/", None),
        ("/v1/models/", b"/v1/models%2F"),  # the slash was sent encoded
    ],
)
def test_anything_else_reaches_the_application(path: str, raw_path: bytes | None) -> None:
    assert slashless_location(_scope(path, raw_path=raw_path)) is None


@pytest.mark.asyncio
async def test_the_redirect_is_a_bare_path_308_whatever_host_the_entry_sent() -> None:
    async with _client(_app()) as client:
        response = await client.get(
            "/v1/models/?x=1",
            headers={"Host": "backend:8080", "X-Forwarded-Proto": "https"},
        )
    assert response.status_code == 308
    assert response.headers["location"] == "/v1/models?x=1"
    assert response.content == b""


@pytest.mark.asyncio
async def test_a_post_body_survives_the_redirect() -> None:
    """308 keeps the method and body: a client that follows it sends both again."""
    async with _client(_app(), follow_redirects=True) as client:
        response = await client.post("/v1/chat/completions/", json={"model": "m", "n": 1})
    assert response.status_code == 200
    assert response.json() == {"received": {"model": "m", "n": 1}}
    assert [r.status_code for r in response.history] == [308]


@pytest.mark.asyncio
async def test_it_redirects_before_the_body_limit_reads_anything() -> None:
    async with _client(_app()) as client:
        response = await client.post(
            "/v1/chat/completions/",
            content=b"{}",
            headers={"Content-Length": str(10**12), "Content-Type": "application/json"},
        )
    assert response.status_code == 308
    assert response.headers["location"] == "/v1/chat/completions"


@pytest.mark.asyncio
async def test_other_prefixes_keep_starlettes_redirect() -> None:
    async with _client(_app()) as client:
        response = await client.get("/health/")
    assert response.status_code == 307
    assert response.headers["location"] == "http://backend:8080/health"


def test_the_gateway_registers_it_inside_cors_and_outside_the_body_limit() -> None:
    from serving.servers.app import create_app
    from serving.servers.middleware.cors import SettingsCORSMiddleware

    middleware = [entry.cls for entry in create_app().user_middleware]
    # user_middleware lists the outermost first.
    cors = middleware.index(SettingsCORSMiddleware)
    redirect = middleware.index(TrailingSlashRedirectMiddleware)
    assert cors < redirect < middleware.index(RequestBodyLimitMiddleware)


@pytest.mark.asyncio
async def test_the_gateway_answers_the_redirect_itself() -> None:
    """The real app, unstarted: the redirect needs no route and no service."""
    from serving.servers.app import create_app

    async with _client(create_app()) as client:
        for path, location in [
            ("/v1/models/", "/v1/models"),
            ("/v1/responses/", "/v1/responses"),
            ("/anthropic/v1/messages/", "/anthropic/v1/messages"),
        ]:
            response = await client.post(path, json={})
            assert response.status_code == 308, path
            assert response.headers["location"] == location, path
            assert response.headers["x-content-type-options"] == "nosniff", path
            assert "x-request-id" in response.headers, path
