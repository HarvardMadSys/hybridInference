"""Routing refresh: scoped to what changed, keeps what matches, isolates failures per model.

The first tests drive :class:`RoutingSynchronizer` against stand-ins for its
collaborators. The rest drive it against a real route table, model router
registry and key registry, with the routing tables in memory, the way a
backend process meets the Admin edits another process made.
"""

from __future__ import annotations

import json
import socket
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from routing.model_router_registry import ModelRouterRegistry
from routing.routers import FixedRouter
from routing.routewise.router import RouteWiseRouter
from serving.adapters import ModelConfig, OpenAICompatAdapter, dynamic_keys, provider_registry
from serving.config import live, revisions
from serving.config.routing_sync import RoutingSynchronizer
from serving.servers.deps import AppServices
from serving.servers.routers.admin import provider_definitions, provider_routes
from serving.servers.routewise_compat import EnvelopeNotCalibratedError

PROVIDERS = "@providers"


class Context:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *args):
        return False


def _snapshot_connection(revisions_by_model, *, epoch="same", revision=2):
    conn = SimpleNamespace(
        fetch=AsyncMock(
            return_value=[
                {"model_id": model_id, "revision": value}
                for model_id, value in revisions_by_model.items()
            ]
        ),
        fetchrow=AsyncMock(return_value={"epoch": epoch, "revision": revision, "contracts": {}}),
        fetchval=AsyncMock(side_effect=lambda _sql, model_id: revisions_by_model.get(model_id)),
    )
    conn.transaction = lambda **kw: Context(conn)
    return conn


def _stub_steps(monkeypatch, reconcile):
    keys = AsyncMock()
    providers = AsyncMock()
    monkeypatch.setattr(dynamic_keys, "reconcile_db_keys", keys)
    monkeypatch.setattr(provider_registry, "apply_provider_definitions_at_boot", providers)
    monkeypatch.setattr(provider_definitions, "_configured_provider_specs", lambda: {})
    monkeypatch.setattr(provider_routes, "reconcile_model_from_store", reconcile)
    monkeypatch.setattr(
        provider_routes, "read_stored_model_routes", AsyncMock(return_value={"one": "rows"})
    )
    return keys, providers


async def test_refresh_only_reconciles_changed_models_and_retries_failure(monkeypatch):
    conn = _snapshot_connection({"one": 2, "two": 1})
    pool = SimpleNamespace(acquire=lambda: Context(conn))
    services = SimpleNamespace(
        operational_store=SimpleNamespace(on_connection=lambda _: "snapshot"),
        model_router_transition_locks={},
    )
    reporter = Mock()
    monkeypatch.setattr(live.COORDINATOR, "reporter", reporter)
    reconcile = AsyncMock(side_effect=RuntimeError("transient"))
    keys, providers = _stub_steps(monkeypatch, reconcile)
    steps = Mock()
    steps.attach_mock(reconcile, "model")
    steps.attach_mock(keys, "credentials")
    sync = RoutingSynchronizer(
        services,
        pool,
        applied={"one": 1, "two": 1},
        version=revisions.SourceVersion("same", 1),
    )
    await sync.refresh()
    assert sync.state == "failed" and sync.applied == {"one": 1, "two": 1}
    assert reporter.set_group.call_args.args[1]["error_keys"] == ["one"]
    reconcile.assert_awaited_once_with(services, "snapshot", "one", "rows")
    # A model that cannot be applied does not hold the credential state back.
    keys.assert_awaited_once_with("snapshot")
    providers.assert_not_awaited()
    reconcile.side_effect = None
    await sync.refresh()
    assert sync.state == "applied" and sync.applied["one"] == 2
    assert sync.version.revision == 2
    assert reconcile.await_count == 2
    assert [call[0] for call in steps.mock_calls[-2:]] == ["model", "credentials"]
    assert reporter.set_group.call_args.args[1]["error_keys"] == []
    await sync.refresh()
    assert reconcile.await_count == 2


async def test_new_epoch_reconciles_equal_counters_and_models_removed_by_restore(monkeypatch):
    conn = _snapshot_connection({"one": 1}, epoch="restored", revision=1)
    services = SimpleNamespace(
        router=SimpleNamespace(routes={"one": object(), "removed": object()}),
        operational_store=SimpleNamespace(on_connection=lambda _: "snapshot"),
        model_router_transition_locks={},
    )
    monkeypatch.setattr(live.COORDINATOR, "reporter", Mock())
    reconcile = AsyncMock()
    _keys, providers = _stub_steps(monkeypatch, reconcile)
    sync = RoutingSynchronizer(
        services,
        SimpleNamespace(acquire=lambda: Context(conn)),
        applied={"one": 1},
        version=revisions.SourceVersion("original", 1),
    )
    await sync.refresh()
    assert sync.state == "applied" and sync.version.epoch == "restored"
    providers.assert_awaited_once()
    assert [call.args[2] for call in reconcile.await_args_list] == ["one", "removed"]


async def test_a_provider_change_rechecks_every_stored_model(monkeypatch):
    conn = _snapshot_connection({"one": 1, "two": 1, PROVIDERS: 3})
    services = SimpleNamespace(
        operational_store=SimpleNamespace(on_connection=lambda _: "snapshot"),
        model_router_transition_locks={},
    )
    monkeypatch.setattr(live.COORDINATOR, "reporter", Mock())
    reconcile = AsyncMock()
    _keys, providers = _stub_steps(monkeypatch, reconcile)
    sync = RoutingSynchronizer(
        services,
        SimpleNamespace(acquire=lambda: Context(conn)),
        applied={"one": 1, "two": 1, PROVIDERS: 2},
        version=revisions.SourceVersion("same", 2),
    )
    await sync.refresh()
    # A re-enabled key can make a route that was left out buildable again.
    assert [call.args[2] for call in reconcile.await_args_list] == ["one", "two"]
    providers.assert_awaited_once()
    assert sync.applied[PROVIDERS] == 3


async def test_missing_source_version_is_not_acknowledged(monkeypatch):
    conn = SimpleNamespace(fetchrow=AsyncMock(return_value=None))
    conn.transaction = lambda **kw: Context(conn)
    monkeypatch.setattr(live.COORDINATOR, "reporter", Mock())
    sync = RoutingSynchronizer(SimpleNamespace(), SimpleNamespace(acquire=lambda: Context(conn)))
    await sync.refresh()
    assert sync.state == "failed" and sync.version is None


# --- A backend process against the routing tables -------------------------------------

PRICING = {"prompt": "0", "completion": "0"}
_UPDATED = datetime(2026, 11, 1, tzinfo=UTC)
CATALOG = (("cfg-model", 7001), ("rw-config", 7101), ("z-model", 7201))


class Database:
    """The routing tables and their change counters."""

    def __init__(self) -> None:
        self.candidates: dict[tuple[str, str], dict] = {}
        self.configs: dict[tuple[str, str], dict] = {}
        self.capabilities: dict[tuple[str, str], dict] = {}
        self.settings: dict[str, str] = {}
        # key id -> (provider, raw key, status)
        self.keys: dict[str, tuple[str, str, str]] = {}
        self.revisions: dict[str, int] = {}
        self.revision = 0
        # Counters committed after a refresh's snapshot was taken.
        self.ahead: dict[str, int] = {}
        # Called on every read, to look at the live table mid-reconcile.
        self.on_read = None

    def bump(self, *model_ids: str) -> None:
        for model_id in model_ids:
            self.revision += 1
            self.revisions[model_id] = self.revision

    def put_candidate(self, model_id: str, port: int, **fields) -> None:
        route_id = f"{model_id}:local-{port}"
        row = {
            "model_id": model_id,
            "route_id": route_id,
            "route_type": "on_demand",
            "provider": "chutes",
            "openrouter_sort": None,
            "base_url": f"http://127.0.0.1:{port}/v1",
            "api_key_id": None,
            "provider_model_id": "upstream/model",
            "quota_limit": None,
            "concurrency_limit": None,
            "weight": 1.0,
            "pricing": json.dumps(PRICING),
            "capabilities": None,
            "deployment_type": None,
            "endpoint_id": None,
            "updated_at": _UPDATED,
            "updated_by": "admin",
        }
        row.update(fields)
        self.candidates[(model_id, route_id)] = row
        self.bump(model_id)

    def put_runtime_model(self, model_id: str, strategy: str = "routewise") -> None:
        self.settings[f"model_router_strategy:{model_id}"] = strategy
        self.settings[f"model_required_role:{model_id}"] = "admin"
        self.bump(model_id)

    def put_config(self, model_id: str, route_id: str, port: int, **fields) -> None:
        row = {
            "model_id": model_id,
            "route_id": route_id,
            "provider": "chutes",
            "openrouter_sort": None,
            "base_url": f"http://127.0.0.1:{port}/v1",
            "api_key_id": None,
            "provider_model_id": "upstream/model",
            "quota_limit": None,
            "concurrency_limit": None,
            "deployment_type": None,
            "updated_at": _UPDATED,
            "updated_by": "admin",
        }
        row.update(fields)
        self.configs[(model_id, route_id)] = row
        self.bump(model_id)


class Store:
    """An operational store over :class:`Database`, as a refresh's snapshot connection."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def on_connection(self, _conn):
        return self

    def _read(self) -> None:
        if self.db.on_read is not None:
            self.db.on_read()

    async def list_all_provider_route_candidates(self):
        self._read()
        return [dict(row) for row in self.db.candidates.values()]

    async def list_all_provider_route_configs(self):
        self._read()
        return [dict(row) for row in self.db.configs.values()]

    async def list_all_route_capability_overrides(self):
        return [dict(row) for row in self.db.capabilities.values()]

    async def list_route_capability_overrides_for_model(self, model_id):
        return [dict(row) for (owner, _), row in self.db.capabilities.items() if owner == model_id]

    async def list_settings(self):
        self._read()
        return [{"key": key, "value": value} for key, value in self.db.settings.items()]

    async def delete_runtime_model_state(self, *_args, **_kwargs):
        return True

    async def delete_provider_route_config(self, *_args):
        return True

    async def get_provider_key_full(self, key_id):
        self._read()
        row = self.db.keys.get(key_id)
        return None if row is None else (row[0], row[1])

    async def list_provider_keys_full(self, provider, *, exclude_ids=None):
        self._read()
        excluded = exclude_ids or set()
        return [
            raw
            for key_id, (owner, raw, status) in self.db.keys.items()
            if owner == provider and status == "active" and key_id not in excluded
        ]

    async def list_disabled_provider_env_key_hashes(self, provider):
        return set()

    async def list_provider_env_key_min_roles(self, provider):
        self._read()
        return {}

    async def list_provider_key_min_roles(self, provider, *, exclude_ids=None):
        return {}

    async def list_provider_definitions(self):
        return []


class Connection:
    def __init__(self, db: Database) -> None:
        self.db = db

    def transaction(self, **_kwargs):
        return Context(self)

    async def fetchrow(self, _sql, _source):
        return {"epoch": "epoch", "revision": self.db.revision, "contracts": {}}

    async def fetch(self, _sql):
        return [{"model_id": key, "revision": value} for key, value in self.db.revisions.items()]

    async def fetchval(self, _sql, model_id):
        return self.db.ahead.get(model_id, self.db.revisions.get(model_id))


class Pool:
    def __init__(self, db: Database) -> None:
        self.db = db

    def acquire(self):
        return Context(Connection(self.db))


@pytest.fixture
async def world(monkeypatch):
    """One backend process: its route table, router registry and key registry."""
    dynamic_keys.reset()
    for row in provider_registry.list_provider_definitions():
        provider_registry.unregister_provider_definition(row.provider)
    monkeypatch.setattr(provider_definitions, "_configured_provider_specs", lambda: {})
    monkeypatch.setattr(live.COORDINATOR, "reporter", Mock())
    db = Database()
    router = FixedRouter()
    dynamic_keys.register_known_provider("chutes")
    for model_id, port in CATALOG:
        adapter = OpenAICompatAdapter(
            ModelConfig(
                id=model_id,
                name=model_id,
                provider="chutes",
                base_url=f"http://127.0.0.1:{port}/v1",
                api_keys=["chutes-env-key-1234567890"],
                provider_model_id="upstream/model",
                endpoint_id=f"{model_id}:local-{port}",
            )
        )
        router.register_route(model_id, [(adapter, 1.0)])
        dynamic_keys.register_adapter_for_provider("chutes", adapter)
    registry = ModelRouterRegistry(
        models_config={model_id: {"router": "fixed"} for model_id, _port in CATALOG},
        shared_fixed_router=router,
    )
    services = AppServices(router=router, model_router_registry=registry)
    services.operational_store = Store(db)
    yield SimpleNamespace(db=db, services=services, registry=registry, router=router)
    for managed in list(services.managed_routers):
        await managed.stop()
    dynamic_keys.reset()


async def _restore(world) -> None:
    """Restore the stored state as bootstrap does."""
    services, store = world.services, world.services.operational_store
    await provider_routes.apply_persisted_model_router_strategy_overrides(services, store)
    await provider_routes.apply_persisted_provider_route_candidates(services, store)
    await provider_routes.apply_persisted_provider_route_configs(services, store)
    await provider_routes.sync_route_capability_overrides(services, store)
    for managed in list(services.managed_routers):
        await managed.start()


async def _boot(world) -> RoutingSynchronizer:
    """Restore the stored state as bootstrap does, then start the synchronizer."""
    await _restore(world)
    sync = RoutingSynchronizer(world.services, Pool(world.db))
    await sync.start()
    await sync.stop()
    return sync


def _adapters(world, model_id: str) -> list:
    return [
        adapter for adapter, _weight, _endpoint_id in world.router.routes[model_id].raw_adapters
    ]


def _urls(world, model_id: str) -> list[str]:
    return [str(adapter.config.base_url) for adapter in _adapters(world, model_id)]


def _keys(adapter) -> list[str]:
    pool = adapter._key_pool
    return [] if pool is None else pool.snapshot_keys()


async def test_boot_and_edits_keep_unchanged_routers_and_routes(world):
    db = world.db
    db.keys["k1"] = ("chutes", "chutes-db-key-1234567890", "active")
    db.put_runtime_model("rt-model")
    db.put_candidate("rt-model", 8001, api_key_id="k1")
    db.put_candidate("rt-model", 8002, api_key_id="k1")
    db.settings["model_router_strategy:rw-config"] = "routewise"
    db.bump("rw-config")

    sync = await _boot(world)
    runtime_router = world.registry.get_router("rt-model")
    config_router = world.registry.get_router("rw-config")
    assert isinstance(runtime_router, RouteWiseRouter)
    assert isinstance(config_router, RouteWiseRouter)
    # Checking what boot restored against the store keeps the warm routers.
    assert sync.state == "applied"
    assert world.registry.get_router("rt-model") is runtime_router
    booted = _adapters(world, "rt-model")

    # Another process edits one route: only that route is rebuilt.
    db.put_candidate("rt-model", 8002, api_key_id="k1", provider_model_id="upstream/other")
    # And overrides a capability of the other model: its router stays.
    db.capabilities[("rw-config", "rw-config:local-7101")] = {
        "model_id": "rw-config",
        "route_id": "rw-config:local-7101",
        "capabilities": json.dumps(
            {
                "context_length": 12345,
                "max_output_length": 1024,
                "input_modalities": ["text"],
                "supports_tools": False,
            }
        ),
    }
    db.bump("rw-config")
    await sync.refresh()

    assert sync.state == "applied"
    assert world.registry.get_router("rt-model") is runtime_router
    assert world.registry.get_router("rw-config") is config_router
    edited = _adapters(world, "rt-model")
    assert edited[0] is booted[0]
    assert edited[1] is not booted[1]
    assert edited[1].config.provider_model_id == "upstream/other"
    assert _adapters(world, "rw-config")[0].config.context_length == 12345
    assert runtime_router._lifecycle_started and config_router._lifecycle_started

    # The process that made the edits reads its own counters back: nothing moves.
    db.bump("rt-model", "rw-config")
    await sync.refresh()
    assert world.registry.get_router("rt-model") is runtime_router
    assert world.registry.get_router("rw-config") is config_router
    assert _adapters(world, "rt-model") == edited


async def test_a_route_pinning_a_deleted_key_stalls_neither_models_nor_revocation(world):
    db = world.db
    db.keys["k1"] = ("chutes", "chutes-deleted-key-1234567890", "active")
    db.keys["k2"] = ("chutes", "chutes-second-key-1234567890", "active")
    db.put_runtime_model("rt-model")
    db.put_candidate("rt-model", 8001, api_key_id="k1")
    sync = await _boot(world)
    pinned = _adapters(world, "rt-model")[0]

    # The key the route pins is deleted, which the Admin API allows, and the
    # model is edited: a route is added to it. Another model changes as well.
    del db.keys["k1"]
    db.bump(PROVIDERS)
    db.put_candidate("rt-model", 8002, api_key_id="k2")
    db.put_candidate("z-model", 8201, api_key_id="k2")
    await sync.refresh()

    assert sync.state == "applied"
    assert _adapters(world, "rt-model")[0] is pinned
    assert _keys(pinned) == []
    assert _urls(world, "rt-model")[1] == "http://127.0.0.1:8002/v1"
    assert _urls(world, "z-model") == ["http://127.0.0.1:7201/v1", "http://127.0.0.1:8201/v1"]

    # A key disabled afterwards leaves every pool that held it.
    db.keys["k2"] = ("chutes", "chutes-second-key-1234567890", "disabled")
    db.bump(PROVIDERS)
    await sync.refresh()
    assert sync.state == "applied"
    for adapter in [*_adapters(world, "rt-model"), *_adapters(world, "z-model")]:
        assert "chutes-second-key-1234567890" not in _keys(adapter)


async def test_a_restarted_process_serves_what_builds_and_applies_revocations(world):
    db = world.db
    db.keys["k2"] = ("chutes", "chutes-second-key-1234567890", "active")
    db.put_runtime_model("rt-model")
    # The first route pins a key deleted before this process started.
    db.put_candidate("rt-model", 8001, api_key_id="k1")
    db.put_candidate("rt-model", 8002, api_key_id="k2")
    db.put_candidate("z-model", 8201, api_key_id="k2")

    sync = await _boot(world)

    assert sync.state == "applied"
    assert _urls(world, "rt-model") == ["http://127.0.0.1:8002/v1"]
    assert _urls(world, "z-model")[-1] == "http://127.0.0.1:8201/v1"
    db.keys["k2"] = ("chutes", "chutes-second-key-1234567890", "disabled")
    db.bump(PROVIDERS)
    await sync.refresh()
    assert sync.state == "applied"
    assert _keys(_adapters(world, "z-model")[-1]) == []
    assert _keys(_adapters(world, "rt-model")[0]) == []


async def test_routes_pinning_a_disabled_key_are_restored_without_serving_it(world):
    db = world.db
    db.keys["k2"] = ("chutes", "chutes-second-key-1234567890", "disabled")
    db.put_runtime_model("rt-model")
    db.put_candidate("rt-model", 8001)
    db.put_candidate("rt-model", 8002, api_key_id="k2")
    db.put_config("cfg-model", "cfg-model:local-7001", 7002, api_key_id="k2")

    # As restored at boot: every route is in place, none serves the disabled key,
    # and the override keeps its own target rather than the one it replaced.
    await _restore(world)
    assert _urls(world, "rt-model") == ["http://127.0.0.1:8001/v1", "http://127.0.0.1:8002/v1"]
    assert _urls(world, "cfg-model") == ["http://127.0.0.1:7002/v1"]
    assert _keys(_adapters(world, "rt-model")[1]) == []
    assert _keys(_adapters(world, "cfg-model")[0]) == []
    sync = RoutingSynchronizer(world.services, Pool(db))
    await sync.refresh()
    assert sync.state == "applied"
    assert _keys(_adapters(world, "rt-model")[1]) == []

    # Enabled again, the key returns to the routes bound to it.
    db.keys["k2"] = ("chutes", "chutes-second-key-1234567890", "active")
    db.bump(PROVIDERS)
    await sync.refresh()

    assert sync.state == "applied"
    assert _keys(_adapters(world, "rt-model")[1]) == ["chutes-second-key-1234567890"]
    assert _keys(_adapters(world, "cfg-model")[0]) == ["chutes-second-key-1234567890"]


async def test_a_failure_that_can_pass_keeps_the_model_serving_and_is_retried(world, monkeypatch):
    db = world.db
    db.put_runtime_model("rt-model")
    db.put_candidate("rt-model", 8001)
    sync = await _boot(world)
    runtime_router = world.registry.get_router("rt-model")
    serving = _adapters(world, "rt-model")

    # The route moves to a host that does not resolve yet; another model changes too.
    db.put_candidate("rt-model", 8001, base_url="http://upstream.example:8001/v1")
    db.put_candidate("z-model", 8201)

    def unresolved(*_args, **_kwargs):
        raise socket.gaierror("temporary failure in name resolution")

    monkeypatch.setattr(provider_routes.socket, "getaddrinfo", unresolved)
    await sync.refresh()

    assert "rt-model" in world.router.routes
    assert _adapters(world, "rt-model") == serving
    assert world.registry.get_router("rt-model") is runtime_router
    assert _urls(world, "z-model")[-1] == "http://127.0.0.1:8201/v1"
    assert sync.state == "failed" and sync.failed == {"rt-model"}

    monkeypatch.setattr(
        provider_routes.socket,
        "getaddrinfo",
        lambda _host, port, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))
        ],
    )
    await sync.refresh()
    assert sync.state == "applied" and sync.failed == set()
    assert _urls(world, "rt-model") == ["http://upstream.example:8001/v1"]
    assert world.registry.get_router("rt-model") is runtime_router


async def test_an_override_change_never_publishes_the_upstream_it_replaced(world):
    db = world.db
    route_id = "cfg-model:local-7001"
    db.put_config("cfg-model", route_id, 7002)
    sync = await _boot(world)
    assert _urls(world, "cfg-model") == ["http://127.0.0.1:7002/v1"]

    views: list[list[str]] = []
    db.on_read = lambda: views.append(_urls(world, "cfg-model"))
    db.put_config("cfg-model", route_id, 7003)
    await sync.refresh()

    assert sync.state == "applied"
    assert _urls(world, "cfg-model") == ["http://127.0.0.1:7003/v1"]
    # The models.yaml upstream the override replaced is never selectable meanwhile.
    assert views
    assert all("http://127.0.0.1:7001/v1" not in view for view in views)


async def test_a_router_that_does_not_start_is_not_reported_applied(world, monkeypatch):
    sync = await _boot(world)
    fixed = world.registry.get_router("cfg-model")

    async def uncalibrated(self):
        raise EnvelopeNotCalibratedError("envelope not calibrated")

    monkeypatch.setattr(RouteWiseRouter, "start", uncalibrated)
    world.db.settings["model_router_strategy:cfg-model"] = "routewise"
    world.db.bump("cfg-model")
    await sync.refresh()

    assert sync.state == "failed" and sync.failed == {"cfg-model"}
    assert world.registry.get_router_name("cfg-model") == "fixed"
    assert world.registry.get_router("cfg-model") is fixed


async def test_an_edit_committed_after_the_snapshot_is_left_to_the_next_refresh(world):
    db = world.db
    db.put_runtime_model("rt-model")
    db.put_candidate("rt-model", 8001)
    sync = await _boot(world)
    serving = _adapters(world, "rt-model")

    # The snapshot holds an older row than the edit this process has already
    # installed, which committed after the snapshot was taken.
    db.put_candidate("rt-model", 8001, provider_model_id="upstream/older")
    db.ahead["rt-model"] = db.revision + 1
    await sync.refresh()

    assert _adapters(world, "rt-model") == serving
    assert sync.state == "applying" and "rt-model" not in sync.failed


async def test_deletions_and_resets_made_elsewhere_are_applied(world):
    db = world.db
    db.put_runtime_model("rt-model")
    db.put_candidate("rt-model", 8001)
    db.put_config("cfg-model", "cfg-model:local-7001", 7002)
    db.settings["model_router_strategy:rw-config"] = "routewise"
    db.bump("rw-config")
    baseline = _adapters(world, "cfg-model")
    sync = await _boot(world)
    runtime_router = world.registry.get_router("rt-model")
    override_router = world.registry.get_router("rw-config")
    assert _urls(world, "cfg-model") == ["http://127.0.0.1:7002/v1"]

    # Another process deletes the runtime model, resets the route override and
    # returns the other model to its configured router.
    del db.candidates[("rt-model", "rt-model:local-8001")]
    del db.settings["model_required_role:rt-model"]
    del db.settings["model_router_strategy:rt-model"]
    db.bump("rt-model")
    del db.configs[("cfg-model", "cfg-model:local-7001")]
    db.bump("cfg-model")
    del db.settings["model_router_strategy:rw-config"]
    db.bump("rw-config")
    await sync.refresh()

    assert sync.state == "applied"
    assert "rt-model" not in world.router.routes
    assert not runtime_router._lifecycle_started
    assert _adapters(world, "cfg-model") == baseline
    assert world.registry.get_router_name("rw-config") == "fixed"
    assert world.registry.get_router("rw-config") is world.router
    assert not override_router._lifecycle_started
