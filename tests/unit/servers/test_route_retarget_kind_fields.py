"""A route moved to another provider does not keep its old adapter kind's fields.

Route edits and new candidates start from a copy of an existing route's adapter
config. The OpenRouter pin and the provider profile in it are derived from that
route's adapter kind, and ``_make_adapter`` sets them only for kinds that have
them: copied onto another kind, a Chutes route kept ``openrouter[parasail]``'s
pin. It was then listed, keyed and probed as an OpenRouter route -- a new
OpenRouter key verified against it was sent to the Chutes host.
"""

from __future__ import annotations

import socket
from unittest.mock import AsyncMock, MagicMock

import pytest

from routing.routers import FixedRouter
from serving.adapters import dynamic_keys
from serving.servers.deps import AppServices
from serving.servers.registry import _make_adapter
from serving.servers.routers.admin import provider_routes

PINNED_ROUTE = "glm:openrouter[parasail]-api"


def _pinned_openrouter_route() -> FixedRouter:
    router = FixedRouter()
    adapter = _make_adapter(
        "openrouter[parasail]",
        {
            "id": "glm",
            "name": "glm",
            "provider": "openrouter",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": None,
            "api_keys": ["sk-or-env-key-1234567890"],
            "provider_model_id": "z-ai/glm-4.6",
            "endpoint_id": PINNED_ROUTE,
            "provider_type": "on_demand",
        },
    )
    router.register_route("glm", [(adapter, 1.0)])
    return router


def _op_store() -> MagicMock:
    op_store = MagicMock()
    op_store.list_disabled_provider_env_key_hashes = AsyncMock(return_value=set())
    op_store.list_provider_keys_full = AsyncMock(return_value=[])
    op_store.get_provider_key_full = AsyncMock(
        return_value=("chutes", "cpk_chutes-db-key-1234567890")
    )
    op_store.list_provider_key_min_roles = AsyncMock(return_value={})
    op_store.list_provider_env_key_min_roles = AsyncMock(return_value={})
    op_store.list_provider_keys = AsyncMock(return_value=[])
    return op_store


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    dynamic_keys.reset()
    monkeypatch.setattr(
        provider_routes.socket,
        "getaddrinfo",
        lambda _host, port, **_kw: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port or 443))
        ],
    )

    async def _no_pricing(**_kwargs):
        return None

    monkeypatch.setattr(provider_routes, "_openrouter_pricing_for_target", _no_pricing)
    monkeypatch.setattr(
        dynamic_keys, "load_min_role_declarations_for_provider", AsyncMock(return_value=None)
    )
    yield
    dynamic_keys.reset()


def _assert_plain_chutes(adapter) -> None:
    assert adapter.config.openrouter_pinned_provider is None
    assert adapter.config.provider_profile is None
    assert provider_routes._upstream_provider(adapter) == "chutes"


@pytest.mark.asyncio
async def test_an_edit_moving_a_pinned_route_to_chutes_drops_the_pin():
    services = AppServices(router=_pinned_openrouter_route(), operational_store=MagicMock())

    update = await provider_routes._prepare_route_update(
        services,
        _op_store(),
        model_id="glm",
        route_id=PINNED_ROUTE,
        upstream_provider="chutes",
        openrouter_sort=None,
        base_url="https://llm.chutes.ai/v1",
        api_key_id="db-chutes",
        provider_model_id_override="zai-org/GLM-4.6",
    )

    _assert_plain_chutes(update.adapter)


@pytest.mark.asyncio
async def test_a_chutes_candidate_copied_from_a_pinned_first_route_drops_the_pin():
    services = AppServices(router=_pinned_openrouter_route(), operational_store=MagicMock())

    candidate = await provider_routes._prepare_route_candidate(
        services,
        _op_store(),
        model_id="glm",
        route_type="on_demand",
        upstream_provider="chutes",
        openrouter_sort=None,
        base_url="https://llm.chutes.ai/v1",
        api_key_id="db-chutes",
        provider_model_id="zai-org/GLM-4.6",
        quota_limit=None,
        concurrency_limit=None,
        weight=1.0,
        capabilities=None,
    )

    _assert_plain_chutes(candidate.adapter)
