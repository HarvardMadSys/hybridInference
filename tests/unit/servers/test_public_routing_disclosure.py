"""``/routing`` and ``/health/deep`` answer anyone, and show only an admin the upstreams.

Both endpoints are public: monitors poll ``/health/deep`` and ``/routing`` is
the documented way to read the route weights. Anonymously they used to return
every route's upstream ``base_url`` -- private LAN addresses, internal hosts and
ports -- and the routing file's probe results keyed by endpoint URL. Those are
now shown only to an administrator's credential; everyone else gets the same
status codes and everything else.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from routing.routers import FixedRouter
from serving.adapters.base import BaseAdapter, ModelConfig
from serving.config.settings import get_settings
from serving.servers.deps import AppServices
from serving.servers.routers import health

pytestmark = pytest.mark.asyncio

ADMIN_TOKEN = "unit-test-admin-token-for-routing"
LAN_URL = "http://10.20.30.40:8004/v1"
CLOUD_URL = "https://api.provider.example/v1"
PROBED_URL = "http://10.20.30.40:8004"


class _EchoAdapter(BaseAdapter):
    async def chat_completion(self, messages: list[dict[str, Any]], **params) -> dict[str, Any]:
        return self.format_response(content="ok", model=self.config.id)

    async def stream_chat_completion(self, messages: list[dict[str, Any]], **params):
        yield self.format_stream_chunk(model=self.config.id, content="ok")


class _StaticDisabledResolver:
    def __init__(self, disabled: set[str]) -> None:
        self.disabled = disabled

    def is_disabled(self, provider: str) -> bool:
        return provider in self.disabled


class _RoutingManager:
    def get_status(self) -> dict[str, object]:
        return {
            "loaded": True,
            "strategy": "fixed",
            "deployments": {"local": 1, "remote": 1},
            "endpoint_health": {PROBED_URL: False},
            "endpoint_health_enforced": False,
        }


def _adapter(provider: str, base_url: str) -> _EchoAdapter:
    return _EchoAdapter(
        ModelConfig(
            id="chat-model",
            name="chat-model",
            provider=provider,
            base_url=base_url,
            context_length=8192,
            max_output_length=1024,
            endpoint_id=f"chat-model:{provider}-api",
        )
    )


@pytest.fixture
def app(monkeypatch) -> FastAPI:
    monkeypatch.setenv("ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("JWT_SECRET_KEY", "unit-test-jwt-secret-for-routing-0123456789")
    get_settings.cache_clear()
    router = FixedRouter(disabled_provider_resolver=_StaticDisabledResolver({"cloud"}))
    router.register_route(
        "chat-model", [(_adapter("lan", LAN_URL), 1.0), (_adapter("cloud", CLOUD_URL), 1.0)]
    )
    app = FastAPI()
    app.state.services = AppServices(
        router=router,
        operational_store=None,
        log_store=None,
        routing_manager=_RoutingManager(),
    )
    app.include_router(health.router)
    yield app
    get_settings.cache_clear()


async def _get(app: FastAPI, path: str, token: str | None = None):
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path, headers=headers)


@pytest.mark.parametrize("token", [None, "not-the-admin-token", ""])
async def test_routing_shows_anyone_the_weights_but_not_the_upstreams(app, token) -> None:
    response = await _get(app, "/routing", token)

    assert response.status_code == 200
    body = response.json()
    assert body["routes"] == {
        "chat-model": [
            {"provider": "lan", "weight": "50%"},
            {"provider": "cloud", "weight": "50%"},
        ]
    }
    manager = body["manager_status"]
    assert manager["strategy"] == "fixed"
    assert manager["endpoint_health_enforced"] is False
    assert "endpoint_health" not in manager
    assert "10.20.30.40" not in response.text
    assert "provider.example" not in response.text


async def test_routing_shows_an_admin_the_upstreams_and_probe_results(app) -> None:
    response = await _get(app, "/routing", ADMIN_TOKEN)

    assert response.status_code == 200
    body = response.json()
    assert body["routes"]["chat-model"] == [
        {"provider": "lan", "base_url": LAN_URL, "weight": "50%"},
        {"provider": "cloud", "base_url": CLOUD_URL, "weight": "50%"},
    ]
    assert body["manager_status"]["endpoint_health"] == {PROBED_URL: False}


@pytest.mark.parametrize("token", [None, "not-the-admin-token"])
async def test_deep_health_names_excluded_routes_without_their_upstreams(app, token) -> None:
    response = await _get(app, "/health/deep", token)

    assert response.status_code == 200
    body = response.json()
    (exclusion,) = body["route_exclusions"]
    assert exclusion["model_id"] == "chat-model"
    assert exclusion["endpoint_id"] == "chat-model:cloud-api"
    assert exclusion["provider"] == "cloud"
    assert exclusion["reasons"] == ["provider_disabled"]
    assert "base_url" not in exclusion
    assert body["providers"]["chat-model:cloud-api"]["excluded_from_models"] == ["chat-model"]
    assert "provider.example" not in response.text


async def test_deep_health_shows_an_admin_the_excluded_routes_upstream(app) -> None:
    response = await _get(app, "/health/deep", ADMIN_TOKEN)

    assert response.status_code == 200
    (exclusion,) = response.json()["route_exclusions"]
    assert exclusion["base_url"] == CLOUD_URL


async def test_a_credential_that_cannot_be_checked_reads_as_anonymous(app, monkeypatch) -> None:
    """No JWT secret (a gateway without authentication) must not turn into a 500."""

    async def unverifiable(*_args: Any, **_kwargs: Any) -> str:
        raise ValueError("JWT_SECRET_KEY must be set in environment")

    monkeypatch.setattr(health, "verify_admin_access", unverifiable)

    response = await _get(app, "/routing", ADMIN_TOKEN)

    assert response.status_code == 200
    assert "base_url" not in response.text


async def test_a_direct_call_without_the_dependency_gets_the_public_answer() -> None:
    """The ``Depends`` default object is truthy; only a real ``True`` shows the upstreams."""
    router = FixedRouter(disabled_provider_resolver=_StaticDisabledResolver({"cloud"}))
    router.register_route("chat-model", [(_adapter("cloud", CLOUD_URL), 1.0)])

    body = await health.get_routing(
        router_exec=router, services=SimpleNamespace(routing_manager=None)
    )

    assert body["routes"] == {"chat-model": [{"provider": "cloud", "weight": "100%"}]}
