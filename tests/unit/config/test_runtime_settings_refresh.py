"""Runtime settings: the refresh loop, a cache that does not expire, environment import."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from serving.config import runtime_settings as runtime_module
from serving.config.runtime_settings import (
    RUNTIME_SETTINGS_REGISTRY,
    RuntimeSettings,
    import_environment_values,
    init_runtime_settings,
)
from serving.config.settings import get_settings


def _store(rows: list[dict] | None = None) -> MagicMock:
    store = MagicMock()
    store.list_settings = AsyncMock(return_value=rows or [])
    store.get_setting = AsyncMock(return_value=None)
    return store


def _row(key: str, value: str, value_type: str) -> dict:
    return {"key": key, "value": value, "value_type": value_type}


@pytest.fixture
def clock(monkeypatch):
    """Control ``time.monotonic`` as RuntimeSettings sees it."""
    now = [1_000.0]
    monkeypatch.setattr(runtime_module.time, "monotonic", lambda: now[0])
    return now


@pytest.mark.asyncio
async def test_refresh_loads_every_setting_with_one_query() -> None:
    store = _store([_row("user_concurrency_free", "7", "int"), _row("other:key", "x", "str")])
    runtime = RuntimeSettings(store)

    await runtime.refresh()

    store.list_settings.assert_awaited_once()
    store.get_setting.assert_not_awaited()
    assert runtime.get_cached("user_concurrency_free") == (True, 7)
    # A setting without a row caches what it resolves to: Settings, then default.
    for key, entry in RUNTIME_SETTINGS_REGISTRY.items():
        found, value = runtime.get_cached(key)
        assert found, key
        if key != "user_concurrency_free":
            assert value == getattr(get_settings(), key, entry["default"]), key


@pytest.mark.asyncio
async def test_cached_values_do_not_expire(clock) -> None:
    runtime = RuntimeSettings(_store([_row("signup_enabled", "False", "bool")]), ttl=30.0)
    await runtime.refresh()

    clock[0] += 3_600

    assert runtime.get_cached("signup_enabled") == (True, False)


@pytest.mark.asyncio
async def test_is_user_auth_enabled_tracks_the_stored_value(monkeypatch, clock) -> None:
    """Regression: the cache used to lapse after 30 s and fail open to the environment."""
    from serving.servers.auth import is_user_auth_enabled

    monkeypatch.setenv("USER_AUTH_ENABLED", "false")
    get_settings.cache_clear()
    store = _store([_row("user_auth_enabled", "True", "bool")])
    runtime = init_runtime_settings(store)
    await runtime.refresh()

    clock[0] += 31  # past the TTL the cache used to expire at
    assert is_user_auth_enabled() is True

    store.list_settings.return_value = [_row("user_auth_enabled", "False", "bool")]
    await runtime.refresh()
    assert is_user_auth_enabled() is False


@pytest.mark.asyncio
async def test_an_unreadable_row_keeps_the_previous_value() -> None:
    store = _store([_row("user_concurrency_free", "5", "int")])
    runtime = RuntimeSettings(store)
    await runtime.refresh()

    store.list_settings.return_value = [_row("user_concurrency_free", "five", "int")]
    await runtime.refresh()

    assert runtime.get_cached("user_concurrency_free") == (True, 5)


@pytest.mark.asyncio
async def test_a_refresh_in_flight_does_not_undo_a_local_write() -> None:
    gate = asyncio.Event()
    store = _store([_row("user_auth_enabled", "False", "bool")])

    async def slow_list_settings():
        await gate.wait()
        return [_row("user_auth_enabled", "False", "bool")]

    store.list_settings = slow_list_settings
    runtime = RuntimeSettings(store)
    refresh = asyncio.create_task(runtime.refresh())
    await asyncio.sleep(0)

    runtime.set_cached("user_auth_enabled", True)  # an administrator's write
    gate.set()
    await refresh

    assert runtime.get_cached("user_auth_enabled") == (True, True)


@pytest.mark.asyncio
async def test_the_refresh_loop_runs_until_stopped() -> None:
    store = _store([_row("signup_enabled", "False", "bool")])
    runtime = RuntimeSettings(store)

    runtime.start_refresh(interval_seconds=0.01)
    for _ in range(100):
        await asyncio.sleep(0.01)
        if store.list_settings.await_count >= 2:
            break
    await runtime.stop_refresh()

    assert store.list_settings.await_count >= 2
    assert runtime.get_cached("signup_enabled") == (True, False)


# --- environment import ------------------------------------------------------


class _Connection:
    """Applies the import query's ON CONFLICT DO NOTHING to a dict."""

    def __init__(self, table: dict[str, tuple[str, str]]) -> None:
        self.table = table
        self.queries: list[str] = []

    async def fetch(self, sql, keys, values, types):
        self.queries.append(sql)
        added = []
        for key, value, value_type in zip(keys, values, types, strict=True):
            if key not in self.table:
                self.table[key] = (value, value_type)
                added.append({"key": key})
        return added


class _Pool:
    def __init__(self, table: dict[str, tuple[str, str]] | None = None) -> None:
        self.connection = _Connection({} if table is None else table)

    def acquire(self):
        connection = self.connection

        class _Acquired:
            async def __aenter__(self):
                return connection

            async def __aexit__(self, *exc_info):
                return False

        return _Acquired()


@pytest.fixture
def clean_runtime_env(monkeypatch):
    for key in RUNTIME_SETTINGS_REGISTRY:
        monkeypatch.delenv(key.upper(), raising=False)


@pytest.mark.asyncio
async def test_import_normalizes_environment_values(monkeypatch, clean_runtime_env) -> None:
    monkeypatch.setenv("USER_AUTH_ENABLED", "on")
    monkeypatch.setenv("SIGNUP_ENABLED", "0")
    monkeypatch.setenv("USER_CONCURRENCY_FREE", "4")
    monkeypatch.setenv("USER_DAILY_QUOTA_FREE", "12.50")
    pool = _Pool()

    imported = await import_environment_values(pool)

    assert sorted(imported) == [
        "signup_enabled",
        "user_auth_enabled",
        "user_concurrency_free",
        "user_daily_quota_free",
    ]
    table = pool.connection.table
    # Written the way the admin API writes, so the cache reads it back right.
    assert table["user_auth_enabled"] == ("True", "bool")
    assert table["signup_enabled"] == ("False", "bool")
    assert table["user_concurrency_free"] == ("4", "int")
    assert table["user_daily_quota_free"] == ("12.5", "float")
    assert "ON CONFLICT (key) DO NOTHING" in pool.connection.queries[0]


@pytest.mark.asyncio
async def test_import_skips_blank_invalid_and_out_of_range_values(
    monkeypatch, clean_runtime_env
) -> None:
    monkeypatch.setenv("LOG_FULL_PAYLOAD", "   ")
    monkeypatch.setenv("SIGNUP_ENABLED", "maybe")
    monkeypatch.setenv("USER_CONCURRENCY_FREE", "0")  # min is 1
    monkeypatch.setenv("ROUTEWISE_BUDGET_ALPHA", "1.5")  # max is 1.0
    pool = _Pool()

    assert await import_environment_values(pool) == []
    assert pool.connection.queries == []


@pytest.mark.asyncio
async def test_import_never_replaces_a_stored_value(monkeypatch, clean_runtime_env) -> None:
    monkeypatch.setenv("USER_AUTH_ENABLED", "true")
    monkeypatch.setenv("SIGNUP_ENABLED", "true")
    pool = _Pool({"user_auth_enabled": ("False", "bool")})

    imported = await import_environment_values(pool)

    assert imported == ["signup_enabled"]
    assert pool.connection.table["user_auth_enabled"] == ("False", "bool")
    # Twice is the same as once.
    assert await import_environment_values(pool) == []
