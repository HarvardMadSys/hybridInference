"""Routing refresh scopes changes and never acknowledges a failed application."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from serving.adapters import dynamic_keys
from serving.config import live, revisions
from serving.config.routing_sync import RoutingSynchronizer
from serving.servers.routers.admin import provider_routes


class Context:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *args):
        return False


async def test_refresh_only_reconciles_changed_models_and_retries_failure(monkeypatch):
    conn = SimpleNamespace(
        fetch=AsyncMock(
            return_value=[{"model_id": "one", "revision": 2}, {"model_id": "two", "revision": 1}]
        ),
        fetchrow=AsyncMock(return_value={"epoch": "same", "revision": 2, "contracts": {}}),
    )
    conn.transaction = lambda **kw: Context(conn)
    pool = SimpleNamespace(acquire=lambda: Context(conn))
    services = SimpleNamespace(
        operational_store=SimpleNamespace(on_connection=lambda _: "snapshot"),
        model_router_transition_locks={},
    )
    reporter = Mock()
    monkeypatch.setattr(live.COORDINATOR, "reporter", reporter)
    keys = AsyncMock()
    monkeypatch.setattr(dynamic_keys, "reconcile_db_keys", keys)
    reconcile = AsyncMock(side_effect=RuntimeError("transient"))
    steps = Mock()
    steps.attach_mock(reconcile, "model")
    steps.attach_mock(keys, "credentials")
    monkeypatch.setattr(provider_routes, "reconcile_live_routes_from_store", reconcile)
    sync = RoutingSynchronizer(services, pool, applied={"one": 1, "two": 1})
    await sync.refresh()
    assert sync.state == "failed" and sync.applied == {"one": 1, "two": 1}
    keys.assert_not_awaited()
    reconcile.assert_awaited_once_with(
        services, "snapshot", model_ids={"one"}, strict=True, lock_held=True
    )
    reconcile.side_effect = None
    await sync.refresh()
    assert sync.state == "applied" and sync.applied["one"] == 2
    assert sync.version.revision == 2
    assert reconcile.await_count == 2
    keys.assert_awaited_once_with("snapshot")
    assert [call[0] for call in steps.mock_calls[-2:]] == ["model", "credentials"]
    await sync.refresh()
    assert reconcile.await_count == 2


async def test_new_epoch_reconciles_equal_counters_and_models_removed_by_restore(monkeypatch):
    conn = SimpleNamespace(
        fetch=AsyncMock(return_value=[{"model_id": "one", "revision": 1}]),
        fetchrow=AsyncMock(return_value={"epoch": "restored", "revision": 1, "contracts": {}}),
    )
    conn.transaction = lambda **kw: Context(conn)
    services = SimpleNamespace(
        router=SimpleNamespace(routes={"one": object(), "removed": object()}),
        operational_store=SimpleNamespace(on_connection=lambda _: "snapshot"),
        model_router_transition_locks={},
    )
    monkeypatch.setattr(live.COORDINATOR, "reporter", Mock())
    reconcile = AsyncMock()
    monkeypatch.setattr(provider_routes, "reconcile_live_routes_from_store", reconcile)
    from serving.adapters import dynamic_keys, provider_registry
    from serving.servers.routers.admin import provider_definitions

    providers = AsyncMock()
    monkeypatch.setattr(provider_registry, "apply_provider_definitions_at_boot", providers)
    monkeypatch.setattr(dynamic_keys, "reconcile_db_keys", AsyncMock())
    monkeypatch.setattr(provider_definitions, "_configured_provider_specs", lambda: {})
    sync = RoutingSynchronizer(
        services,
        SimpleNamespace(acquire=lambda: Context(conn)),
        applied={"one": 1},
        version=revisions.SourceVersion("original", 1),
    )
    await sync.refresh()
    assert sync.state == "applied" and sync.version.epoch == "restored"
    providers.assert_awaited_once()
    assert [call.kwargs["model_ids"] for call in reconcile.await_args_list] == [
        {"one"},
        {"removed"},
    ]


async def test_missing_source_version_is_not_acknowledged(monkeypatch):
    conn = SimpleNamespace(fetchrow=AsyncMock(return_value=None))
    conn.transaction = lambda **kw: Context(conn)
    monkeypatch.setattr(live.COORDINATOR, "reporter", Mock())
    sync = RoutingSynchronizer(SimpleNamespace(), SimpleNamespace(acquire=lambda: Context(conn)))
    await sync.refresh()
    assert sync.state == "failed" and sync.version is None
