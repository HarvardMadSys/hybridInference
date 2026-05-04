"""Unit tests for RuntimeSettings (TTL cache, fallback, type coercion)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from serving.config.runtime_settings import (
    RUNTIME_SETTINGS_REGISTRY,
    RuntimeSettings,
    get_runtime_settings_instance,
    init_runtime_settings,
)


@pytest.fixture
def mock_store():
    store = MagicMock()
    store.get_setting = AsyncMock(return_value=None)
    store.set_setting = AsyncMock()
    store.list_settings = AsyncMock(return_value=[])
    return store


@pytest.fixture
def rt(mock_store):
    return RuntimeSettings(mock_store, ttl=30.0)


class TestCoerce:
    def test_bool_true_values(self, rt):
        assert rt._coerce("true", "bool") is True
        assert rt._coerce("True", "bool") is True
        assert rt._coerce("1", "bool") is True
        assert rt._coerce("yes", "bool") is True

    def test_bool_false_values(self, rt):
        assert rt._coerce("false", "bool") is False
        assert rt._coerce("0", "bool") is False
        assert rt._coerce("no", "bool") is False

    def test_int(self, rt):
        assert rt._coerce("42", "int") == 42

    def test_float(self, rt):
        assert rt._coerce("3.14", "float") == 3.14

    def test_str(self, rt):
        assert rt._coerce("hello", "str") == "hello"

    def test_none(self, rt):
        assert rt._coerce(None, "bool") is None


class TestFallback:
    async def test_get_bool_returns_default_when_no_db_row(self, rt, mock_store):
        mock_store.get_setting.return_value = None
        val = await rt.get_bool("user_auth_enabled")
        assert val is True

    async def test_get_bool_returns_db_value(self, rt, mock_store):
        mock_store.get_setting.return_value = {
            "key": "signup_enabled",
            "value": "false",
            "value_type": "bool",
        }
        val = await rt.get_bool("signup_enabled")
        assert val is False

    async def test_get_bool_returns_settings_attr_as_fallback(self, rt, mock_store):
        mock_store.get_setting.return_value = None
        val = await rt.get_bool("user_auth_enabled")
        assert val is True

    async def test_unknown_key_raises(self, rt):
        with pytest.raises(KeyError):
            await rt.get_bool("nonexistent_key")


class TestTTLCache:
    async def test_cache_hit_avoids_db(self, rt, mock_store):
        mock_store.get_setting.return_value = {
            "key": "signup_enabled",
            "value": "false",
            "value_type": "bool",
        }
        await rt.get_bool("signup_enabled")
        await rt.get_bool("signup_enabled")
        assert mock_store.get_setting.call_count == 1

    async def test_cache_expiry_reads_db_again(self, mock_store):
        rt = RuntimeSettings(mock_store, ttl=0.0)
        mock_store.get_setting.return_value = {
            "key": "signup_enabled",
            "value": "false",
            "value_type": "bool",
        }
        await rt.get_bool("signup_enabled")
        mock_store.get_setting.return_value = {
            "key": "signup_enabled",
            "value": "true",
            "value_type": "bool",
        }
        val = await rt.get_bool("signup_enabled")
        assert val is True
        assert mock_store.get_setting.call_count == 2


class TestInvalidate:
    async def test_invalidate_key_clears_single_entry(self, rt, mock_store):
        mock_store.get_setting.return_value = {
            "key": "signup_enabled",
            "value": "false",
            "value_type": "bool",
        }
        await rt.get_bool("signup_enabled")
        rt.invalidate_key("signup_enabled")
        mock_store.get_setting.return_value = {
            "key": "signup_enabled",
            "value": "true",
            "value_type": "bool",
        }
        val = await rt.get_bool("signup_enabled")
        assert val is True
        assert mock_store.get_setting.call_count == 2

    async def test_invalidate_cache_clears_all(self, rt, mock_store):
        mock_store.get_setting.return_value = {
            "key": "signup_enabled",
            "value": "false",
            "value_type": "bool",
        }
        await rt.get_bool("signup_enabled")
        rt.invalidate_cache()
        mock_store.get_setting.return_value = {
            "key": "signup_enabled",
            "value": "true",
            "value_type": "bool",
        }
        val = await rt.get_bool("signup_enabled")
        assert val is True


class TestListAll:
    async def test_list_all_returns_registry_entries(self, rt, mock_store):
        mock_store.get_setting.return_value = None
        results = await rt.list_all()
        assert len(results) == len(RUNTIME_SETTINGS_REGISTRY)
        assert all("key" in r for r in results)
        assert all("value" in r for r in results)
        assert all("description" in r for r in results)

    async def test_list_all_includes_db_overrides(self, rt, mock_store):
        def get_setting_side_effect(key):
            if key == "signup_enabled":
                return {"key": key, "value": "false", "value_type": "bool"}
            return None

        mock_store.get_setting.side_effect = get_setting_side_effect
        results = await rt.list_all()
        signup = next(r for r in results if r["key"] == "signup_enabled")
        assert signup["value"] is False


class TestSingleton:
    def test_init_and_get(self, mock_store):
        import serving.config.runtime_settings as mod

        old = mod._runtime_settings
        try:
            rs = init_runtime_settings(mock_store)
            assert get_runtime_settings_instance() is rs
        finally:
            mod._runtime_settings = old

    def test_get_raises_when_not_initialized(self):
        import serving.config.runtime_settings as mod

        old = mod._runtime_settings
        try:
            mod._runtime_settings = None
            with pytest.raises(RuntimeError):
                get_runtime_settings_instance()
        finally:
            mod._runtime_settings = old


class TestCacheWarmup:
    async def test_warmup_populates_all_registry_keys(self, mock_store):
        """After warming up all keys, get_cached returns (True, ...) for every key."""
        rt = RuntimeSettings(mock_store, ttl=30.0)
        for key in RUNTIME_SETTINGS_REGISTRY:
            await rt.get_bool(key)
        for key in RUNTIME_SETTINGS_REGISTRY:
            found, _ = rt.get_cached(key)
            assert found, f"Cache miss for {key!r} after warmup"

    async def test_warmup_bad_key_does_not_prevent_others(self, mock_store):
        """A per-key exception during warmup leaves the other keys in cache."""
        import contextlib

        async def get_setting_side_effect(key):
            if key == "signup_enabled":
                raise RuntimeError("simulated DB error")
            return None

        mock_store.get_setting.side_effect = get_setting_side_effect

        rt = RuntimeSettings(mock_store, ttl=30.0)
        keys = list(RUNTIME_SETTINGS_REGISTRY)
        for key in keys:
            with contextlib.suppress(Exception):
                await rt.get_bool(key)

        for key in keys:
            if key == "signup_enabled":
                continue
            found, _ = rt.get_cached(key)
            assert found, f"Cache miss for {key!r} after partial warmup"
