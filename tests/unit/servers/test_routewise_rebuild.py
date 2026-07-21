"""Tests for the serving-to-routing route-table refresh boundary."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from serving.servers.routewise_rebuild import rebuild_cached_routewise_routers


@pytest.mark.unit
def test_rebuild_cached_routers_delegates_to_registry_capability() -> None:
    refresh_route_tables = MagicMock()
    registry = SimpleNamespace(refresh_route_tables=refresh_route_tables)

    rebuild_cached_routewise_routers(registry)

    refresh_route_tables.assert_called_once_with()


@pytest.mark.unit
def test_rebuild_cached_routers_accepts_missing_registry() -> None:
    rebuild_cached_routewise_routers(None)
