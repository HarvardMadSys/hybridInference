"""Observing workers reconcile revocations without leaking credentials between routes."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from serving.adapters import ModelConfig, OpenAICompatAdapter, dynamic_keys, provider_registry


class Store:
    def __init__(self):
        self.keys = {}
        self.bound_ids = set()
        self.disabled = set()
        self.env_roles = {}
        self.definitions = []
        self.fail_read = False

    async def list_all_provider_route_configs(self):
        return [{"api_key_id": key} for key in self.bound_ids]

    async def list_all_provider_route_candidates(self):
        return []

    async def list_provider_keys_full(self, provider, *, exclude_ids=()):
        if self.fail_read:
            raise RuntimeError("database unavailable")
        return [
            key for row, (key, active) in self.keys.items() if active and row not in exclude_ids
        ]

    async def list_disabled_provider_env_key_hashes(self, provider):
        return self.disabled

    async def list_provider_env_key_min_roles(self, provider):
        return self.env_roles

    async def list_provider_key_min_roles(self, provider):
        return {}

    async def list_provider_definitions(self):
        return self.definitions


@pytest.fixture(autouse=True)
def reset_registries():
    dynamic_keys.reset()
    for row in provider_registry.list_provider_definitions():
        provider_registry.unregister_provider_definition(row.provider)
    yield
    dynamic_keys.reset()
    for row in provider_registry.list_provider_definitions():
        provider_registry.unregister_provider_definition(row.provider)


def adapter(*keys, binding=None):
    instance = OpenAICompatAdapter(
        ModelConfig(
            id="sync",
            name="Sync",
            provider="sync",
            base_url="http://localhost:9/v1",
            api_keys=list(keys),
        )
    )
    dynamic_keys.register_adapter_for_provider(
        "sync", instance, allow_db_key_injection=binding is None, api_key_id=binding
    )
    return instance


async def test_observer_removes_disabled_and_deleted_keys_and_keeps_duplicate_sources():
    store = Store()
    first = adapter("own-env", "env-duplicate")
    second = adapter("other-env")
    store.keys = {
        "delete": ("to-delete", True),
        "disable": ("to-disable", True),
        "duplicate-a": ("duplicate", True),
        "duplicate-b": ("duplicate", True),
        "env-duplicate": ("env-duplicate", True),
    }
    await dynamic_keys.reconcile_db_keys(store)
    assert "to-delete" in first._key_pool.snapshot_keys()
    assert "own-env" not in second._key_pool.snapshot_keys()
    del store.keys["delete"]
    del store.keys["duplicate-a"]
    del store.keys["env-duplicate"]
    store.keys["disable"] = ("to-disable", False)
    await dynamic_keys.reconcile_db_keys(store)
    assert set(first._key_pool.snapshot_keys()) == {"own-env", "env-duplicate", "duplicate"}
    assert set(second._key_pool.snapshot_keys()) == {"other-env", "duplicate"}
    store.keys["disable"] = ("to-disable", True)
    await dynamic_keys.reconcile_db_keys(store)
    assert "to-disable" in first._key_pool.snapshot_keys()
    assert "to-disable" in second._key_pool.snapshot_keys()


async def test_observer_keeps_pinned_db_credentials_scoped_through_revocation_and_reenable():
    store = Store()
    store.keys = {"pinned-row": ("pinned-secret", True), "shared-row": ("shared-secret", True)}
    store.bound_ids = {"pinned-row"}
    unbound = adapter("own-env")
    pinned = adapter("pinned-secret", binding="pinned-row")
    await dynamic_keys.reconcile_db_keys(store)
    assert set(unbound._key_pool.snapshot_keys()) == {"own-env", "shared-secret"}
    assert pinned._key_pool.snapshot_keys() == ["pinned-secret"]
    store.keys["pinned-row"] = ("pinned-secret", False)
    await dynamic_keys.reconcile_db_keys(store)
    assert pinned._key_pool.snapshot_keys() == []
    store.keys["pinned-row"] = ("pinned-secret", True)
    await dynamic_keys.reconcile_db_keys(store)
    assert pinned._key_pool.snapshot_keys() == ["pinned-secret"]
    del store.keys["pinned-row"]
    await dynamic_keys.reconcile_db_keys(store)
    assert pinned._key_pool.snapshot_keys() == []
    assert "pinned-secret" not in unbound._key_pool.snapshot_keys()


async def test_observer_reenables_empty_env_tombstone_set_only_on_the_owning_adapter():
    store = Store()
    owner = adapter("env-secret", binding="env:owner")
    unrelated = adapter("unrelated-secret", binding="env:other")
    key_hash = dynamic_keys.env_key_hash("env-secret")
    store.disabled = {key_hash}
    await dynamic_keys.reconcile_db_keys(store)
    assert owner._key_pool.snapshot_keys() == []
    store.disabled.clear()
    store.env_roles[key_hash] = "pro"
    await dynamic_keys.reconcile_db_keys(store)
    assert owner._key_pool.snapshot_keys() == ["env-secret"]
    assert owner._key_pool.snapshot_min_roles() == {"env-secret": "pro"}
    assert unrelated._key_pool.snapshot_keys() == ["unrelated-secret"]
    store.env_roles.clear()
    await dynamic_keys.reconcile_db_keys(store)
    assert owner._key_pool.snapshot_min_roles() == {"env-secret": "free"}


async def test_snapshot_read_failure_never_partially_changes_existing_pools():
    store = Store()
    instance = adapter("env-secret")
    store.keys["one"] = ("db-secret", True)
    await dynamic_keys.reconcile_db_keys(store)
    before = instance._key_pool.snapshot_keys()
    store.keys.clear()
    store.fail_read = True
    with pytest.raises(RuntimeError, match="database unavailable"):
        await dynamic_keys.reconcile_db_keys(store)
    assert instance._key_pool.snapshot_keys() == before


async def test_provider_refresh_removes_hard_deleted_definitions():
    store = Store()
    store.definitions = [
        SimpleNamespace(
            provider="temporary",
            display_name="Temporary",
            adapter_kind="openai_compat",
            default_base_url="http://localhost:9/v1",
            status="active",
        )
    ]
    await provider_registry.apply_provider_definitions_at_boot(store, remove_missing=True)
    assert provider_registry.get_provider_definition("temporary") is not None
    store.definitions.clear()
    await provider_registry.apply_provider_definitions_at_boot(store, remove_missing=True)
    assert provider_registry.get_provider_definition("temporary") is None
    assert "temporary" not in dynamic_keys.get_known_providers()
