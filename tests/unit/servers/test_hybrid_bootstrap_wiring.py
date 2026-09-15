"""Bootstrap must attach the hybrid factory before anything looks a router up.

The function under test is the one ``initialize()`` uses to build the registry,
so the attach is exercised through the production entry point rather than by
constructing the factory by hand. An ordering slip is otherwise silent: the
registry keeps serving the plain shared router and nothing fails.
"""

from __future__ import annotations

from typing import Any

import pytest

from routing.dependencies import RouterBuildDependencies
from routing.endpoint_health import EndpointHealthRegistry
from routing.hybrid import HybridRouter
from routing.routers import FixedRouter
from serving.adapters.base import BaseAdapter, ModelConfig
from serving.servers.bootstrap import _build_model_router_registry

_MODEL_ID = "bootstrap-model"
_LOCAL_ENDPOINT = f"{_MODEL_ID}:local-11434"
_CLOUD_ENDPOINT = f"{_MODEL_ID}:zai-api"


class _Adapter(BaseAdapter):
    """Minimal adapter double: this test never dispatches a request."""

    async def chat_completion(
        self,
        messages: list[dict[str, Any]],
        **params: Any,
    ) -> dict[str, Any]:
        return self.format_response(content="ok", model=self.config.id)

    async def stream_chat_completion(self, messages: list[dict[str, Any]], **params: Any) -> Any:
        raise NotImplementedError


def _adapter(endpoint_id: str, *, provider: str, base_url: str) -> _Adapter:
    return _Adapter(
        ModelConfig(
            id=_MODEL_ID,
            name=_MODEL_ID,
            provider=provider,
            base_url=base_url,
            endpoint_id=endpoint_id,
            pricing={"prompt": "1", "completion": "1"},
            input_modalities=["text"],
        )
    )


def _bootstrap_shaped_pieces() -> tuple[RouterBuildDependencies, FixedRouter]:
    """Build the dependencies and shared router ``initialize()`` builds."""
    dependencies = RouterBuildDependencies(health_registry=EndpointHealthRegistry())
    shared = FixedRouter(health_registry=dependencies.health_registry)
    return (dependencies, shared)


def _registry_for(
    dependencies: RouterBuildDependencies, shared: FixedRouter, *, opt_in: bool | None = True
) -> Any:
    return _build_model_router_registry(
        models_config={
            _MODEL_ID: {
                "router": "fixed",
                "router_params": {} if opt_in is None else {"hybrid_composition": opt_in},
            }
        },
        default_router_name="fixed",
        alias_to_model={},
        router_dependencies=dependencies,
        shared_router=shared,
    )


@pytest.mark.unit
@pytest.mark.parametrize("opt_in", [None, False, True])
def test_bootstrap_registry_builds_a_hybrid_router_for_a_mixed_model(opt_in: bool | None) -> None:
    """The production entry point is what makes the seam live."""
    dependencies, shared = _bootstrap_shaped_pieces()
    shared.register_route(
        _MODEL_ID,
        [
            (
                _adapter(_LOCAL_ENDPOINT, provider="local", base_url="http://localhost:11434/v1"),
                1.0,
            ),
            (
                _adapter(_CLOUD_ENDPOINT, provider="zai", base_url="https://api.zai.example/v1"),
                1.0,
            ),
        ],
    )

    registry = _registry_for(dependencies, shared, opt_in=opt_in)

    if opt_in:
        assert isinstance(registry.get_router(_MODEL_ID), HybridRouter)
    else:
        assert registry.get_router(_MODEL_ID) is shared


@pytest.mark.unit
def test_bootstrap_registry_leaves_a_remote_only_deployment_on_the_shared_router() -> None:
    """No local range means no split to express; the model keeps working."""
    dependencies, shared = _bootstrap_shaped_pieces()
    shared.register_route(
        _MODEL_ID,
        [
            (
                _adapter(_CLOUD_ENDPOINT, provider="zai", base_url="https://api.zai.example/v1"),
                1.0,
            )
        ],
    )

    registry = _registry_for(dependencies, shared)

    assert isinstance(registry.get_router(_MODEL_ID), FixedRouter)


@pytest.mark.unit
def test_bootstrap_registry_attaches_before_the_first_lookup() -> None:
    """A late attach is refused, which is why the two steps share one call.

    This documents the failure the single-call structure prevents: once a model
    has been looked up, the registry will not accept the hook, and every model
    already cached would keep the plain shared router.
    """
    dependencies, shared = _bootstrap_shaped_pieces()
    shared.register_route(
        _MODEL_ID,
        [
            (
                _adapter(_LOCAL_ENDPOINT, provider="local", base_url="http://localhost:11434/v1"),
                1.0,
            ),
            (
                _adapter(_CLOUD_ENDPOINT, provider="zai", base_url="https://api.zai.example/v1"),
                1.0,
            ),
        ],
    )
    registry = _registry_for(dependencies, shared)
    # The eager lookup ``initialize()`` performs right after building the registry
    # must observe the hybrid router, not the shared one it replaced.
    assert isinstance(registry.get_router(_MODEL_ID), HybridRouter)

    from serving.servers.hybrid_composition import HybridFixedRouterFactory

    factory = HybridFixedRouterFactory(
        registry=registry,
        health_registry=dependencies.health_registry,
    )
    with pytest.raises(RuntimeError, match="before router lookup"):
        registry.set_hybrid_router_factory(factory)
