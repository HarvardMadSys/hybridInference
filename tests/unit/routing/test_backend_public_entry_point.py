"""The public entry point for routing backends stays one import path.

The implementations were split into focused modules, but ``routing.backends`` is
what the composition root, both routers and the tests import. These assertions
pin only that public entry point, so folding the internal modules back
together later does not break this test: nothing here imports a private
implementation module.
"""

from __future__ import annotations

import pytest

from routing import backends

_EXPECTED_EXPORTS = {
    "CloudBackend",
    "FixedCloudBackend",
    "LeafBackend",
    "LocalBackend",
    "RouteWiseCloudBackend",
    "RoutingBackend",
    "RoutingBackendBase",
    "TreeBackend",
}


@pytest.mark.unit
def test_the_public_module_exports_exactly_the_backend_contract() -> None:
    assert set(backends.__all__) == _EXPECTED_EXPORTS


@pytest.mark.unit
@pytest.mark.parametrize("name", sorted(_EXPECTED_EXPORTS))
def test_every_export_is_importable_from_the_public_entry_point(name: str) -> None:
    exported = getattr(backends, name)

    assert exported is not None
    assert name in backends.__all__


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

    assert isinstance(_MinimalBackend(), backends.RoutingBackend)
