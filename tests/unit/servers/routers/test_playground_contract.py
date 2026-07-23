"""Stable-control contract tests for playground compatibility aliases."""

from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from serving.servers.deps import get_router
from serving.servers.routers import playground


class _PlaygroundRouter:
    def __init__(self) -> None:
        self.routes: dict[str, Any] = {}

    async def stream_chat_completion(self, model, messages, **kwargs):
        yield (
            'data: {"choices":[{"index":0,"delta":{"content":"hello"},'
            '"finish_reason":null}],"_routing":{"provider":"secret"}}\n\n'
        )
        yield "data: [DONE]\n\n"


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(playground.router)
    app.dependency_overrides[playground._require_internal] = lambda: {
        "user_id": "internal-user",
        "role": "internal",
    }
    app.dependency_overrides[get_router] = lambda: _PlaygroundRouter()
    return TestClient(app)


def _normalized_sse_frames(body: str) -> list[Any]:
    frames: list[Any] = []
    for line in body.splitlines():
        if not line.startswith("data: "):
            continue
        payload = line[6:]
        if payload == "[DONE]":
            frames.append(payload)
            continue
        frame = json.loads(payload)
        frame.pop("id", None)
        frame.pop("created", None)
        frames.append(frame)
    return frames


def test_models_stable_path_matches_legacy_alias() -> None:
    client = _client()

    stable = client.get("/control/v1/playground/models")
    legacy = client.get("/internal/playground/models")

    assert stable.status_code == legacy.status_code == 200
    assert stable.json() == legacy.json() == {"models": []}


def test_chat_stable_path_preserves_legacy_auth_and_sse_contract() -> None:
    client = _client()
    payload = {
        "model": "model-a",
        "messages": [{"role": "user", "content": "hello"}],
    }

    stable = client.post("/control/v1/playground/chat", json=payload)
    legacy = client.post("/internal/playground/chat", json=payload)

    assert stable.status_code == legacy.status_code == 200
    for response in (stable, legacy):
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-cache, no-transform"
        assert response.headers["x-accel-buffering"] == "no"
        assert "_routing" not in response.text
        assert "data: [DONE]" in response.text
    assert _normalized_sse_frames(stable.text) == _normalized_sse_frames(legacy.text)


def test_stable_and_legacy_routes_share_handlers_and_dependencies() -> None:
    routes = {(route.path, next(iter(route.methods))): route for route in playground.router.routes}

    stable_models = routes[("/control/v1/playground/models", "GET")]
    legacy_models = routes[("/internal/playground/models", "GET")]
    stable_chat = routes[("/control/v1/playground/chat", "POST")]
    legacy_chat = routes[("/internal/playground/chat", "POST")]

    assert stable_models.endpoint is legacy_models.endpoint
    assert stable_chat.endpoint is legacy_chat.endpoint
    assert [dependency.call for dependency in stable_models.dependant.dependencies] == [
        dependency.call for dependency in legacy_models.dependant.dependencies
    ]
    assert [dependency.call for dependency in stable_chat.dependant.dependencies] == [
        dependency.call for dependency in legacy_chat.dependant.dependencies
    ]


def test_stable_openapi_models_the_json_and_sse_responses() -> None:
    schema = _client().app.openapi()

    models = schema["paths"]["/control/v1/playground/models"]["get"]
    assert models["operationId"] == "listPlaygroundModels"
    assert models["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/PlaygroundModelsResponse"
    }

    chat = schema["paths"]["/control/v1/playground/chat"]["post"]
    assert chat["operationId"] == "streamPlaygroundChat"
    assert chat["responses"]["200"]["content"]["text/event-stream"]["schema"] == {"type": "string"}
