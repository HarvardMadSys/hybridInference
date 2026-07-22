from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from routing.model_router_registry import ModelRouterRegistry
from routing.routers import FixedRouter
from routing.routewise.router import RouteWiseRouter
from serving.config.routewise_model_settings import (
    ROUTEWISE_SETTING_KEYS,
    RouteWiseSettingsResolver,
    apply_routewise_settings_to_router,
    model_routewise_setting_key,
    model_routewise_setting_keys,
)
from serving.config.runtime_settings import RuntimeSettings


def _registry(*, router_params=None, aliases=None) -> ModelRouterRegistry:
    return ModelRouterRegistry(
        models_config={
            "model": {
                "router": "routewise",
                "router_params": dict(router_params or {}),
            }
        },
        alias_to_model=dict(aliases or {}),
        shared_fixed_router=FixedRouter(),
    )


@pytest.mark.asyncio
async def test_resolver_precedence_and_alias_canonicalization():
    store = AsyncMock()
    global_rows = {
        "routewise_budget_alpha": {"value": "0.9", "value_type": "float"},
        "routewise_latency_slo_sec": {"value": "4.0", "value_type": "float"},
        "routewise_latency_min_samples": {"value": "12", "value_type": "int"},
        "routewise_probe_enabled": {"value": "true", "value_type": "bool"},
        "routewise_probe_interval_sec": {"value": "45", "value_type": "float"},
    }
    store.list_settings.return_value = [
        {
            "key": model_routewise_setting_key("routewise_budget_alpha", "model"),
            "value": "0.2",
            "value_type": "float",
        },
        *({"key": key, **row} for key, row in global_rows.items()),
    ]
    store.get_setting.side_effect = global_rows.get
    registry = _registry(
        router_params={"budget_alpha": 0.4, "latency_slo_sec": 0.8},
        aliases={"alias": "model"},
    )
    resolver = RouteWiseSettingsResolver(store, RuntimeSettings(store), registry)

    assert await resolver.load_all() is True
    resolved = await resolver.resolve_model("alias")

    alpha = resolved["routewise_budget_alpha"]
    assert (alpha.value, alpha.fallback_value, alpha.source) == (
        0.2,
        0.4,
        "runtime_override",
    )
    slo = resolved["routewise_latency_slo_sec"]
    assert (slo.value, slo.fallback_value, slo.source) == (0.8, 0.8, "model_config")
    samples = resolved["routewise_latency_min_samples"]
    assert (samples.value, samples.fallback_value, samples.source) == (
        12,
        12,
        "global_default",
    )
    assert resolved["routewise_probe_enabled"].value is True
    store.get_setting.assert_not_awaited()

    resolver.set_override("alias", "routewise_latency_min_samples", 20)
    assert resolver.get_override_snapshot("model")["routewise_latency_min_samples"] == 20


@pytest.mark.asyncio
async def test_resolver_preserves_values_accepted_by_yaml_strategy_schema():
    store = AsyncMock()
    store.list_settings.return_value = []
    store.get_setting.return_value = None
    registry = _registry(
        router_params={
            "budget_alpha": 2,
            "latency_slo_sec": 0.05,
            "latency_min_samples": 0,
            "routewise_probe_enabled": 1,
            "routewise_probe_interval_sec": 1,
        }
    )
    resolver = RouteWiseSettingsResolver(store, RuntimeSettings(store), registry)
    await resolver.load_all()

    resolved = await resolver.resolve_model("model")

    assert resolved["routewise_budget_alpha"].value == 2.0
    assert resolved["routewise_latency_slo_sec"].value == 0.05
    assert resolved["routewise_latency_min_samples"].value == 0
    assert resolved["routewise_probe_enabled"].value is True
    assert resolved["routewise_probe_interval_sec"].value == 1.0


@pytest.mark.asyncio
async def test_invalid_reload_keeps_last_known_good_override():
    store = AsyncMock()
    key = model_routewise_setting_key("routewise_budget_alpha", "model")
    store.list_settings.side_effect = [
        [{"key": key, "value": "0.3", "value_type": "float"}],
        [{"key": key, "value": "not-a-number", "value_type": "float"}],
    ]
    store.get_setting.return_value = None
    registry = _registry()
    resolver = RouteWiseSettingsResolver(store, RuntimeSettings(store), registry)

    assert await resolver.load_all() is True
    assert await resolver.load_all() is False

    assert resolver.get_override_snapshot("model") == {"routewise_budget_alpha": 0.3}


@pytest.mark.asyncio
async def test_local_write_fences_stale_full_snapshot_reload():
    started = asyncio.Event()
    release = asyncio.Event()

    async def list_settings():
        started.set()
        await release.wait()
        return [
            {
                "key": model_routewise_setting_key("routewise_budget_alpha", "model"),
                "value": "0.1",
            }
        ]

    store = AsyncMock()
    store.list_settings.side_effect = list_settings
    store.get_setting.return_value = None
    registry = _registry()
    resolver = RouteWiseSettingsResolver(store, RuntimeSettings(store), registry)

    load_task = asyncio.create_task(resolver.load_all())
    await started.wait()
    resolver.set_override("model", "routewise_budget_alpha", 0.6)
    release.set()

    # The first global-default warmup may still report a change, but the stale
    # model row must not replace the concurrent local write.
    assert await load_task is True
    assert resolver.get_override_snapshot("model") == {"routewise_budget_alpha": 0.6}


@pytest.mark.asyncio
async def test_newer_full_snapshot_fences_an_older_slow_reload():
    old_read_started = asyncio.Event()
    release_old_read = asyncio.Event()
    scoped_key = model_routewise_setting_key("routewise_budget_alpha", "model")
    reads = 0

    async def list_settings():
        nonlocal reads
        reads += 1
        if reads == 1:
            old_read_started.set()
            await release_old_read.wait()
            value = "0.1"
        else:
            value = "0.9"
        return [
            {"key": scoped_key, "value": value, "value_type": "float"},
            {
                "key": "routewise_latency_slo_sec",
                "value": value,
                "value_type": "float",
            },
        ]

    store = AsyncMock()
    store.list_settings.side_effect = list_settings
    store.get_setting.return_value = None
    resolver = RouteWiseSettingsResolver(store, RuntimeSettings(store), _registry())

    older = asyncio.create_task(resolver.load_all())
    await old_read_started.wait()
    await resolver.load_all()
    release_old_read.set()
    await older

    assert resolver.get_override_snapshot("model") == {"routewise_budget_alpha": 0.9}
    resolved_slo = await resolver.get_resolved("model", "routewise_latency_slo_sec")
    assert resolved_slo.value == 0.9


@pytest.mark.asyncio
async def test_newer_full_snapshot_wins_when_older_reload_finishes_first():
    first_started = asyncio.Event()
    second_started = asyncio.Event()
    release_first = asyncio.Event()
    release_second = asyncio.Event()
    scoped_key = model_routewise_setting_key("routewise_budget_alpha", "model")
    reads = 0

    async def list_settings():
        nonlocal reads
        reads += 1
        if reads == 1:
            first_started.set()
            await release_first.wait()
            value = "0.1"
        else:
            second_started.set()
            await release_second.wait()
            value = "0.9"
        return [{"key": scoped_key, "value": value, "value_type": "float"}]

    store = AsyncMock()
    store.list_settings.side_effect = list_settings
    store.get_setting.return_value = None
    resolver = RouteWiseSettingsResolver(store, RuntimeSettings(store), _registry())

    older = asyncio.create_task(resolver.load_all())
    await first_started.wait()
    newer = asyncio.create_task(resolver.load_all())
    await second_started.wait()

    release_first.set()
    await older
    assert resolver.get_override_snapshot("model") == {}

    release_second.set()
    await newer
    assert resolver.get_override_snapshot("model") == {"routewise_budget_alpha": 0.9}


@pytest.mark.asyncio
async def test_newer_global_refresh_fences_an_older_slow_read():
    old_read_started = asyncio.Event()
    release_old_read = asyncio.Event()
    budget_reads = 0

    async def get_setting(key):
        nonlocal budget_reads
        if key != "routewise_budget_alpha":
            return None
        budget_reads += 1
        if budget_reads == 1:
            old_read_started.set()
            await release_old_read.wait()
            return {"value": "0.1", "value_type": "float"}
        return {"value": "0.9", "value_type": "float"}

    store = AsyncMock()
    store.get_setting.side_effect = get_setting
    registry = _registry()
    resolver = RouteWiseSettingsResolver(store, RuntimeSettings(store), registry)

    older = asyncio.create_task(resolver._refresh_global_defaults(force=True))
    await old_read_started.wait()
    await resolver._refresh_global_defaults(force=True)
    release_old_read.set()
    await older

    resolved = await resolver.get_resolved("model", "routewise_budget_alpha")
    assert resolved.value == 0.9


@pytest.mark.asyncio
async def test_apply_replaces_all_five_fields_and_reconciles_probe_task():
    store = AsyncMock()
    store.list_settings.return_value = [
        {
            "key": model_routewise_setting_key(key, "model"),
            "value": value,
        }
        for key, value in {
            "routewise_budget_alpha": "0.25",
            "routewise_latency_slo_sec": "1.5",
            "routewise_latency_min_samples": "4",
            "routewise_probe_enabled": "true",
            "routewise_probe_interval_sec": "30",
        }.items()
    ]
    store.get_setting.return_value = None
    registry = _registry()
    resolver = RouteWiseSettingsResolver(store, RuntimeSettings(store), registry)
    await resolver.load_all()
    router = registry.get_router("model")
    assert isinstance(router, RouteWiseRouter)
    await router.start()

    await apply_routewise_settings_to_router(
        resolver,
        registry,
        "model",
        router,
        refresh_probe_task=True,
    )

    assert router.config.budget_alpha == 0.25
    assert router.config.latency_slo_sec == 1.5
    assert router.config.latency_min_samples == 4
    assert router.config.routewise_probe_enabled is True
    assert router.config.routewise_probe_interval_sec == 30.0
    assert router._probe_task is not None
    await router.stop()


@pytest.mark.asyncio
async def test_probe_interval_change_restarts_sleeping_probe_loop_immediately():
    router = RouteWiseRouter()
    original_config = router.config
    router.apply_runtime_overrides(
        routewise_probe_enabled=True,
        routewise_probe_interval_sec=30.0,
    )
    assert router.config is not original_config
    await router.start()
    first_task = router._probe_task
    assert first_task is not None

    router.apply_runtime_overrides(routewise_probe_interval_sec=60.0)
    await router.refresh_probe_task()

    assert first_task.done()
    assert router._probe_task is not first_task
    assert router._probe_task_config == (True, 60.0)
    await router.stop()


@pytest.mark.asyncio
async def test_probe_refresh_cannot_leak_a_task_after_concurrent_stop():
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()

    async def slow_probe_loop():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleanup_started.set()
            await release_cleanup.wait()
            # Match the production probe loop, which treats cancellation as a
            # normal return after its cleanup completes.
            return

    router = RouteWiseRouter()
    router._routewise_probe_loop = slow_probe_loop
    router.apply_runtime_overrides(
        routewise_probe_enabled=True,
        routewise_probe_interval_sec=30.0,
    )
    await router.start()

    router.apply_runtime_overrides(routewise_probe_interval_sec=60.0)
    refresh = asyncio.create_task(router.refresh_probe_task())
    await cleanup_started.wait()
    stop = asyncio.create_task(router.stop())
    await asyncio.sleep(0)
    release_cleanup.set()
    await asyncio.gather(refresh, stop)

    assert router._probe_task is None
    assert router._probe_task_config is None


@pytest.mark.asyncio
async def test_concurrent_restart_waits_for_old_maintenance_task_cleanup():
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    loop_starts = 0

    async def slow_sweep_loop():
        nonlocal loop_starts
        loop_starts += 1
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleanup_started.set()
            await release_cleanup.wait()

    router = RouteWiseRouter()
    router.prefix_cache.enabled = True
    router._sweep_pending_prefix_cache_loop = slow_sweep_loop
    await router.start()

    stop = asyncio.create_task(router.stop())
    await cleanup_started.wait()
    restart = asyncio.create_task(router.start())
    await asyncio.sleep(0)

    assert loop_starts == 1
    assert not restart.done()

    release_cleanup.set()
    await asyncio.gather(stop, restart)
    await asyncio.sleep(0)

    assert router._lifecycle_started is True
    assert loop_starts == 2
    assert router._prefix_cache_sweep_task is not None
    assert not router._prefix_cache_sweep_task.done()
    await router.stop()


@pytest.mark.asyncio
async def test_probe_refresh_propagates_caller_cancellation_without_replacement():
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()

    async def slow_probe_loop():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleanup_started.set()
            await release_cleanup.wait()
            # The child deliberately swallows its own cancellation, as the
            # production probe loop does.
            return

    router = RouteWiseRouter()
    router._routewise_probe_loop = slow_probe_loop
    router.apply_runtime_overrides(
        routewise_probe_enabled=True,
        routewise_probe_interval_sec=30.0,
    )
    await router.start()

    router.apply_runtime_overrides(routewise_probe_interval_sec=60.0)
    refresh = asyncio.create_task(router.refresh_probe_task())
    await cleanup_started.wait()
    refresh.cancel()
    release_cleanup.set()

    with pytest.raises(asyncio.CancelledError):
        await refresh
    assert router._probe_task is None
    assert router._probe_task_config is None


def test_scoped_setting_keys_are_one_row_per_key():
    keys = model_routewise_setting_keys("provider:model/with/slash")

    assert len(keys) == len(ROUTEWISE_SETTING_KEYS) == 5
    assert keys[0].startswith("model_routewise_setting:routewise_budget_alpha:")
    assert all(key.endswith("provider:model/with/slash") for key in keys)
