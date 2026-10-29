"""The public entry point for routing backends stays one import path.

The implementations were split into focused modules, but ``routing.backends`` is
what the composition root, both routers and the tests import. These assertions
pin that entry point: every exported name is the same object its defining module
owns, so reorganizing internal modules does not break callers.
"""

from __future__ import annotations

import pytest

from routing import backends
from routing.backends_contract import RoutingBackend, RoutingBackendBase
from routing.backends_domains import CloudBackend, FixedCloudBackend, LocalBackend
from routing.backends_leaf import LeafBackend
from routing.backends_routewise import RouteWiseCloudBackend
from routing.backends_tree import TreeBackend

_DEFINING_MODULE = {
    "RoutingBackend": RoutingBackend,
    "RoutingBackendBase": RoutingBackendBase,
    "LeafBackend": LeafBackend,
    "TreeBackend": TreeBackend,
    "LocalBackend": LocalBackend,
    "CloudBackend": CloudBackend,
    "FixedCloudBackend": FixedCloudBackend,
    "RouteWiseCloudBackend": RouteWiseCloudBackend,
}


@pytest.mark.unit
def test_the_public_module_exports_exactly_the_backend_contract() -> None:
    assert sorted(backends.__all__) == sorted(_DEFINING_MODULE)


@pytest.mark.unit
@pytest.mark.parametrize("name", sorted(_DEFINING_MODULE))
def test_every_export_is_the_object_its_module_defines(name: str) -> None:
    exported = getattr(backends, name)

    assert exported is _DEFINING_MODULE[name]


@pytest.mark.unit
def test_the_execution_domain_contract_stays_runtime_checkable() -> None:
    """Backends are matched structurally on the dispatch path."""

    class _MinimalBackend:
        name = "minimal"

        async def chat_completion(self, model_id, messages, **params):
            return {}

        def stream_chat_completion(self, model_id, messages, **params):
            return None

        def record_observation(self, obs):
            return None

        def get_provider_status(self):
            return {}

        def owns_observation(self, obs):
            return False

        @property
        def is_local(self):
            return False

        @property
        def is_cloud(self):
            return False

    assert isinstance(_MinimalBackend(), RoutingBackend)
