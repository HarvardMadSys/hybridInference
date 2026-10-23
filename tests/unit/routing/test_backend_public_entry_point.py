"""The public entry point for routing backends stays one import path.

The implementations were split into focused modules, but ``routing.backends`` is
what the composition root, both routers and the tests import. These assertions
pin that entry point: every exported name is the same object its defining module
owns, and the inheritance relationships a dispatch relies on are unchanged.
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
    "RoutingBackend": (RoutingBackend, "routing.backends_contract"),
    "RoutingBackendBase": (RoutingBackendBase, "routing.backends_contract"),
    "LeafBackend": (LeafBackend, "routing.backends_leaf"),
    "TreeBackend": (TreeBackend, "routing.backends_tree"),
    "LocalBackend": (LocalBackend, "routing.backends_domains"),
    "CloudBackend": (CloudBackend, "routing.backends_domains"),
    "FixedCloudBackend": (FixedCloudBackend, "routing.backends_domains"),
    "RouteWiseCloudBackend": (RouteWiseCloudBackend, "routing.backends_routewise"),
}


@pytest.mark.unit
def test_the_public_module_exports_exactly_the_backend_contract() -> None:
    assert sorted(backends.__all__) == sorted(_DEFINING_MODULE)


@pytest.mark.unit
@pytest.mark.parametrize("name", sorted(_DEFINING_MODULE))
def test_every_export_is_the_object_its_module_defines(name: str) -> None:
    expected, defining_module = _DEFINING_MODULE[name]

    exported = getattr(backends, name)

    assert exported is expected
    assert exported.__module__ == defining_module


@pytest.mark.unit
def test_the_dispatch_boundary_shapes_are_unchanged() -> None:
    """A leaf executes one endpoint; every pool-shaped backend is a tree."""
    assert not issubclass(LeafBackend, TreeBackend)
    assert not issubclass(LeafBackend, RoutingBackendBase)
    assert issubclass(TreeBackend, RoutingBackendBase)
    assert issubclass(LocalBackend, TreeBackend)
    assert issubclass(CloudBackend, TreeBackend)
    assert issubclass(FixedCloudBackend, CloudBackend)
    assert issubclass(RouteWiseCloudBackend, CloudBackend)


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
