"""Provider quota framework: key discovery, result shaping, the fetcher registry."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.adapters import dynamic_keys
from serving.adapters.key_pool import KeyPool
from serving.admin.provider_quotas import (
    _discover_env_keys,
    _discover_provider_keys,
    _mask_key,
    _next_reset,
    _parse_iso,
    _process_multi_key_results,
    gather_all,
    key_ref,
    register_quota_fetcher,
    registered_quota_fetchers,
    reset_quota_fetchers,
)
from serving.schemas_admin import ProviderQuotaResult, ProviderQuotaUsage
from serving.servers.deps import AppServices, verify_admin_access
from serving.servers.routers import admin as admin_router


@pytest.fixture(autouse=True)
def _isolated_fetcher_registry():
    reset_quota_fetchers()
    yield
    reset_quota_fetchers()


def _result(
    name: str,
    display_name: str,
    *,
    key: str | None = None,
    ok: bool = True,
    error: str | None = None,
    usages: list[ProviderQuotaUsage] | None = None,
) -> ProviderQuotaResult:
    return ProviderQuotaResult(
        name=name,
        display_name=display_name,
        key_configured=key is not None,
        key_masked=_mask_key(key) if key else None,
        fetched_at=datetime.now(timezone.utc),
        ok=ok,
        error=error,
        usages=usages or [],
        key_ref=key_ref(key) if key else None,
    )


def _register_fake(name: str, display_name: str, results: list[ProviderQuotaResult]):
    """Register a fetcher that records how it was called and returns *results*."""
    calls: list[tuple] = []

    async def fetch(operational_store=None, services=None):
        calls.append((operational_store, services))
        return list(results)

    register_quota_fetcher(name, display_name, fetch)
    return calls


class TestMaskKey:
    def test_normal_length_key_shows_prefix_and_suffix(self):
        # >= 16 chars: first 8 + "..." + last 4
        assert _mask_key("cpk_ab123456cccccccxyz9") == "cpk_ab12...xyz9"

    def test_exactly_16_char_key_uses_full_form(self):
        assert _mask_key("0123456789abcdef") == "01234567...cdef"

    def test_15_char_key_uses_placeholder(self):
        assert _mask_key("0123456789abcde") == "***configured***"

    def test_short_key_returns_placeholder(self):
        assert _mask_key("short") == "***configured***"

    def test_long_cookie_string_gets_masked(self):
        cookie = "session=abc123def456ghi789jkl012mno345"
        result = _mask_key(cookie)
        assert result.startswith("session=")
        assert "..." in result
        assert len(result) == 8 + 3 + 4


class TestParseIso:
    def test_z_suffix_parsed_as_utc(self):
        result = _parse_iso("2026-05-02T04:00:00Z")
        assert result is not None
        assert result.tzinfo is not None
        assert result.utcoffset().total_seconds() == 0
        assert result.year == 2026
        assert result.month == 5
        assert result.day == 2
        assert result.hour == 4

    def test_explicit_utc_offset_preserved(self):
        result = _parse_iso("2026-05-02T04:00:00+00:00")
        assert result is not None
        assert result.tzinfo is not None
        assert result.utcoffset().total_seconds() == 0

    def test_naive_string_assumed_utc(self):
        result = _parse_iso("2026-04-11T17:07:09")
        assert result is not None
        assert result.tzinfo is not None
        assert result.utcoffset().total_seconds() == 0
        assert result.hour == 17

    def test_non_string_returns_none(self):
        assert _parse_iso(None) is None
        assert _parse_iso(123) is None
        assert _parse_iso(["2026-05-02"]) is None

    def test_malformed_string_returns_none(self):
        assert _parse_iso("not a date") is None
        assert _parse_iso("") is None
        assert _parse_iso("2026-13-99T99:99:99") is None

    def test_non_utc_offset_normalized_to_utc(self):
        # 09:00+05:00 == 04:00 UTC
        result = _parse_iso("2026-05-02T09:00:00+05:00")
        assert result is not None
        assert result.utcoffset().total_seconds() == 0
        assert result.hour == 4

    def test_only_trailing_z_replaced(self):
        # An embedded 'Z' (e.g., timezone-name part) should not be substituted.
        # Plain trailing 'Z' still parses.
        assert _parse_iso("2026-05-02T04:00:00Z") is not None
        # Embedded Z that is not a TZ marker -> ValueError -> None
        assert _parse_iso("2026Z05-02T04:00:00") is None


class TestDiscoverEnvKeys:
    def test_single_key_returns_index_1(self, monkeypatch):
        monkeypatch.setenv("ZAI_API_KEY", "key1_long_enough_1234")
        monkeypatch.delenv("ZAI_API_KEY2", raising=False)
        keys = _discover_env_keys("ZAI_API_KEY", "ZAI_API_KEY")
        assert keys == [(1, "key1_long_enough_1234")]

    def test_multiple_keys_returns_all(self, monkeypatch):
        monkeypatch.setenv("ZAI_API_KEY", "key1_long_enough_1234")
        monkeypatch.setenv("ZAI_API_KEY2", "key2_long_enough_5678")
        monkeypatch.setenv("ZAI_API_KEY3", "key3_long_enough_9012")
        keys = _discover_env_keys("ZAI_API_KEY", "ZAI_API_KEY")
        assert keys == [
            (1, "key1_long_enough_1234"),
            (2, "key2_long_enough_5678"),
            (3, "key3_long_enough_9012"),
        ]

    def test_no_keys_returns_empty(self, monkeypatch):
        monkeypatch.delenv("ZAI_API_KEY", raising=False)
        keys = _discover_env_keys("ZAI_API_KEY", "ZAI_API_KEY")
        assert keys == []

    def test_gap_stops_discovery(self, monkeypatch):
        monkeypatch.setenv("ZAI_API_KEY", "key1_long_enough_1234")
        monkeypatch.delenv("ZAI_API_KEY2", raising=False)
        monkeypatch.setenv("ZAI_API_KEY3", "key3_long_enough_9012")
        keys = _discover_env_keys("ZAI_API_KEY", "ZAI_API_KEY")
        assert keys == [(1, "key1_long_enough_1234")]

    def test_numbered_only_without_base_returns_empty(self, monkeypatch):
        monkeypatch.delenv("ZAI_API_KEY", raising=False)
        monkeypatch.setenv("ZAI_API_KEY2", "key2_long_enough_5678")
        keys = _discover_env_keys("ZAI_API_KEY", "ZAI_API_KEY")
        assert keys == []


class TestDiscoverProviderKeys:
    @staticmethod
    def _store(
        *,
        db_keys: list[str] | None = None,
        disabled_hashes: set[str] | None = None,
        route_configs: list[dict[str, Any]] | None = None,
        route_candidates: list[dict[str, Any]] | None = None,
    ):
        return SimpleNamespace(
            list_provider_keys_full=AsyncMock(return_value=db_keys or []),
            list_disabled_provider_env_key_hashes=AsyncMock(return_value=disabled_hashes or set()),
            list_all_provider_route_configs=AsyncMock(return_value=route_configs or []),
            list_all_provider_route_candidates=AsyncMock(return_value=route_candidates or []),
        )

    @pytest.mark.asyncio
    async def test_appends_db_and_live_pool_keys_and_deduplicates(self, monkeypatch):
        env_key = "key1_long_enough_1234"
        db_key = "db_key_long_enough_0000"
        db_key_1 = "db_key_long_enough_5678"
        db_key_2 = "db_key_long_enough_9012"
        db_key_3 = "db_key_long_enough_3456"
        monkeypatch.setenv("FEATHERLESS_API_KEY", env_key)
        store = self._store(db_keys=[env_key, db_key])
        dynamic_keys.register_adapter_for_provider(
            "featherless",
            SimpleNamespace(_key_pool=KeyPool([env_key, db_key_1, db_key_2], "featherless")),
        )
        dynamic_keys.register_adapter_for_provider(
            "featherless",
            SimpleNamespace(_key_pool=KeyPool([db_key_1, db_key_3], "featherless")),
        )

        keys = await _discover_provider_keys(
            "featherless",
            "FEATHERLESS_API_KEY",
            "FEATHERLESS_API_KEY",
            store,
        )

        assert keys == [
            (1, env_key),
            (2, db_key),
            (3, db_key_1),
            (4, db_key_2),
            (5, db_key_3),
        ]
        store.list_provider_keys_full.assert_awaited_once_with(
            "featherless",
            exclude_ids=set(),
        )

    @pytest.mark.asyncio
    async def test_skips_route_bound_db_keys(self, monkeypatch):
        monkeypatch.delenv("FEATHERLESS_API_KEY", raising=False)
        global_db_key = "db_key_global_123456"
        route_bound_db_key = "db_key_route_bound_123456"
        store = self._store(
            db_keys=[global_db_key],
            route_configs=[{"api_key_id": "route-key-id"}],
        )
        dynamic_keys.register_adapter_for_provider(
            "featherless",
            SimpleNamespace(_key_pool=KeyPool([route_bound_db_key], "featherless")),
            allow_db_key_injection=False,
        )

        keys = await _discover_provider_keys(
            "featherless",
            "FEATHERLESS_API_KEY",
            "FEATHERLESS_API_KEY",
            store,
        )

        assert keys == [(1, global_db_key)]
        store.list_provider_keys_full.assert_awaited_once_with(
            "featherless",
            exclude_ids={"route-key-id"},
        )
        assert route_bound_db_key not in [key for _idx, key in keys]

    @pytest.mark.asyncio
    async def test_skips_disabled_env_keys(self, monkeypatch):
        active_key = "key1_long_enough_1234"
        disabled_key = "key2_long_enough_5678"
        pool_key = "key3_long_enough_9012"
        monkeypatch.setenv("FEATHERLESS_API_KEY", active_key)
        monkeypatch.setenv("FEATHERLESS_API_KEY2", disabled_key)
        store = self._store(disabled_hashes={dynamic_keys.env_key_hash(disabled_key)})
        dynamic_keys.register_adapter_for_provider(
            "featherless",
            SimpleNamespace(_key_pool=KeyPool([disabled_key, pool_key], "featherless")),
        )

        keys = await _discover_provider_keys(
            "featherless",
            "FEATHERLESS_API_KEY",
            "FEATHERLESS_API_KEY",
            store,
        )

        assert keys == [(1, active_key), (2, pool_key)]

    @pytest.mark.asyncio
    async def test_disabled_first_env_key_does_not_collide_with_appended_keys(self, monkeypatch):
        """Env keys keep their suffix number; appended keys must allocate past it.

        Disabling FEATHERLESS_API_KEY leaves [(2, key2)] -- length 1, index 2 --
        so `len(keys) + 1` handed the pool key index 2 as well. Two keys then
        shared an index and a display name, and the admin dashboard could not
        tell them apart.
        """
        # Low-entropy placeholders: the repo's gitleaks allowlist uses the
        # [[allowlists]] array form, which the pinned pre-commit gitleaks does
        # not parse, so its ^tests/.*\.py$ exemption does not actually apply to
        # new lines.
        disabled_key = "env-key-one-env-key-one"
        active_key = "env-key-two-env-key-two"
        pool_key = "pool-key-aaa-pool-key-aaa"
        monkeypatch.setenv("FEATHERLESS_API_KEY", disabled_key)
        monkeypatch.setenv("FEATHERLESS_API_KEY2", active_key)
        store = self._store(disabled_hashes={dynamic_keys.env_key_hash(disabled_key)})
        dynamic_keys.register_adapter_for_provider(
            "featherless",
            SimpleNamespace(_key_pool=KeyPool([pool_key], "featherless")),
        )

        keys = await _discover_provider_keys(
            "featherless",
            "FEATHERLESS_API_KEY",
            "FEATHERLESS_API_KEY",
            store,
        )

        assert keys == [(2, active_key), (3, pool_key)]
        assert len({index for index, _ in keys}) == len(keys)


class TestNextReset:
    def test_daily_midnight(self):
        now = datetime(2026, 5, 2, 14, 30, 0, tzinfo=timezone.utc)
        reset = _next_reset("daily", now=now)
        assert reset == datetime(2026, 5, 3, 0, 0, 0, tzinfo=timezone.utc)

    def test_session_same_as_daily(self):
        now = datetime(2026, 5, 2, 14, 30, 0, tzinfo=timezone.utc)
        reset = _next_reset("session", now=now)
        assert reset == datetime(2026, 5, 3, 0, 0, 0, tzinfo=timezone.utc)

    def test_weekly_next_monday(self):
        now = datetime(2026, 5, 6, 10, 0, 0, tzinfo=timezone.utc)  # Wednesday
        reset = _next_reset("weekly", now=now)
        assert reset == datetime(2026, 5, 11, 0, 0, 0, tzinfo=timezone.utc)  # next Monday

    def test_weekly_on_monday_goes_next_week(self):
        now = datetime(2026, 5, 4, 0, 0, 0, tzinfo=timezone.utc)  # Monday
        reset = _next_reset("weekly", now=now)
        assert reset == datetime(2026, 5, 11, 0, 0, 0, tzinfo=timezone.utc)  # next Monday

    def test_monthly_first_of_next_month(self):
        now = datetime(2026, 5, 15, 12, 0, 0, tzinfo=timezone.utc)
        reset = _next_reset("monthly", now=now)
        assert reset == datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)

    def test_monthly_december_wraps_year(self):
        now = datetime(2026, 12, 31, 23, 59, 0, tzinfo=timezone.utc)
        reset = _next_reset("monthly", now=now)
        assert reset == datetime(2027, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


class _QuotaKeyStore:
    """Minimal operational-store stand-in for key discovery in gather_all."""

    def __init__(self, disabled_env=None, db_rows=None, db_raw=None):
        # provider -> list[(key_hash, key_prefix)]
        self.disabled_env = disabled_env or {}
        # provider -> list[SimpleNamespace(id, key_prefix, status)]
        self.db_rows = db_rows or {}
        # key_id -> (provider, raw_key)
        self.db_raw = db_raw or {}

    async def list_disabled_provider_env_key_hashes(self, provider: str) -> set[str]:
        return {h for h, _ in self.disabled_env.get(provider, [])}

    async def list_disabled_provider_env_keys(self, provider: str) -> list[tuple[str, str]]:
        return list(self.disabled_env.get(provider, []))

    async def list_provider_keys(self, provider: str | None = None):
        return list(self.db_rows.get(provider, []))

    async def list_provider_keys_full(self, provider: str, *, exclude_ids=None) -> list[str]:
        return []

    async def get_provider_key_full(self, key_id: str):
        return self.db_raw.get(key_id)

    async def list_all_provider_route_configs(self) -> list[dict]:
        return []

    async def list_all_provider_route_candidates(self) -> list[dict]:
        return []


class TestRegistry:
    def test_register_keeps_registration_order_and_trims(self):
        async def fetch(operational_store=None, services=None):
            return []

        register_quota_fetcher(" beta ", "  Beta  ", fetch)
        register_quota_fetcher("alpha", "", fetch)
        assert [(s.provider, s.display_name) for s in registered_quota_fetchers()] == [
            ("beta", "Beta"),
            ("alpha", "alpha"),
        ]

    def test_register_rejects_a_duplicate_unless_overridden(self):
        async def fetch(operational_store=None, services=None):
            return []

        async def other(operational_store=None, services=None):
            return []

        register_quota_fetcher("alpha", "Alpha", fetch)
        with pytest.raises(ValueError, match="already registered"):
            register_quota_fetcher("alpha", "Alpha", other)
        register_quota_fetcher("alpha", "Alpha", other, override=True)
        assert registered_quota_fetchers()[0].fetch is other

    def test_register_rejects_a_blank_provider(self):
        async def fetch(operational_store=None, services=None):
            return []

        with pytest.raises(ValueError, match="must not be blank"):
            register_quota_fetcher("  ", "Blank", fetch)


class TestGatherAll:
    @pytest.mark.asyncio
    async def test_gather_all_is_empty_until_an_extension_registers_a_fetcher(self):
        assert await gather_all() == []

    @pytest.mark.asyncio
    async def test_gather_all_flattens_results_in_registration_order(self):
        _register_fake("beta", "Beta", [_result("beta", "Beta #1"), _result("beta", "Beta #2")])
        _register_fake("alpha", "Alpha", [_result("alpha", "Alpha")])

        results = await gather_all()

        assert [r.display_name for r in results] == ["Beta #1", "Beta #2", "Alpha"]

    @pytest.mark.asyncio
    async def test_gather_all_passes_the_store_and_services_to_each_fetcher(self):
        calls = _register_fake("alpha", "Alpha", [])
        store = MagicMock(name="operational_store")
        store.list_disabled_provider_env_keys = AsyncMock(return_value=[])
        store.list_provider_keys = AsyncMock(return_value=[])
        services = SimpleNamespace()

        await gather_all(store, services)

        assert calls == [(store, services)]

    @pytest.mark.asyncio
    async def test_gather_all_handles_unexpected_exception(self):
        async def boom(operational_store=None, services=None):
            raise RuntimeError("simulated failure")

        register_quota_fetcher("alpha", "Alpha", boom)
        _register_fake("beta", "Beta", [_result("beta", "Beta")])

        results = await gather_all()

        assert [r.name for r in results] == ["alpha", "beta"]
        alpha = results[0]
        assert alpha.ok is False
        assert alpha.error == "unexpected"
        assert alpha.display_name == "Alpha"
        assert results[1].ok is True


class TestProviderQuotasRoute:
    @pytest.fixture
    def admin_app(self):
        """Build a minimal FastAPI app with the admin router mounted."""
        app = FastAPI(title="Admin Provider Quotas Test")
        op_store = MagicMock(name="operational_store")
        op_store.list_provider_keys_full = AsyncMock(return_value=[])
        op_store.list_disabled_provider_env_keys = AsyncMock(return_value=[])
        op_store.list_provider_keys = AsyncMock(return_value=[])
        op_store.get_setting = AsyncMock(return_value=None)
        services = AppServices(
            router=MagicMock(),
            db_logger=None,
            operational_store=op_store,
            routing_manager=None,
        )
        app.state.services = services  # type: ignore[attr-defined]
        app.include_router(admin_router.router)
        return app

    @pytest.mark.asyncio
    async def test_route_requires_admin_auth(self, admin_app):
        transport = ASGITransport(app=admin_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/admin/provider-quotas")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_route_returns_aggregated_response(self, admin_app):
        async def _fake_admin() -> str:
            return "admin@test"

        admin_app.dependency_overrides[verify_admin_access] = _fake_admin
        _register_fake("alpha", "Alpha", [_result("alpha", "Alpha", key="alpha-key-0123456789")])
        _register_fake("beta", "Beta", [_result("beta", "Beta", ok=False, error="not_configured")])

        transport = ASGITransport(app=admin_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/admin/provider-quotas")
        admin_app.dependency_overrides.clear()

        assert resp.status_code == 200
        body = resp.json()
        assert "generated_at" in body
        assert [p["name"] for p in body["providers"]] == ["alpha", "beta"]
        assert body["providers"][0]["key_masked"] == _mask_key("alpha-key-0123456789")
        assert body["providers"][1]["error"] == "not_configured"


class TestPerKeyRef:
    def test_key_ref_matches_env_key_id_hash(self):
        raw = "sk-example-key-0123456789"
        assert key_ref(raw) == dynamic_keys.env_key_hash(raw)[:32]

    def test_api_key_results_carry_key_ref(self):
        keys = [(1, "rc_1111111111111111aaaa"), (2, "rc_2222222222222222bbbb")]
        results = _process_multi_key_results(
            "alpha",
            "Alpha",
            keys,
            [_result("alpha", "Alpha"), _result("alpha", "Alpha")],
        )
        assert [r.key_ref for r in results] == [key_ref(k) for _, k in keys]
        assert all(r.key_disabled is False for r in results)
        assert [r.key_index for r in results] == [1, 2]

    def test_cookie_results_have_no_key_ref(self):
        """Session cookies are not managed by the provider-key endpoints."""
        results = _process_multi_key_results(
            "alpha",
            "Alpha",
            [(1, "session=abc123def456ghi789jkl")],
            [_result("alpha", "Alpha")],
            manageable=False,
        )
        assert results
        assert all(r.key_ref is None for r in results)


class TestDisabledKeyCards:
    @pytest.mark.asyncio
    async def test_gather_all_surfaces_disabled_env_and_db_keys(self):
        env_raw = "alpha-env-disabled-key-000000"
        db_raw = "alpha-db-disabled-key-111111"
        store = _QuotaKeyStore(
            disabled_env={
                "alpha": [(dynamic_keys.env_key_hash(env_raw), "alpha-env-...0000")],
            },
            db_rows={
                "alpha": [
                    SimpleNamespace(id="key-1", key_prefix="alpha-db-1...1111", status="disabled"),
                    SimpleNamespace(id="key-2", key_prefix="alpha-db-2...2222", status="active"),
                ],
            },
            db_raw={"key-1": ("alpha", db_raw)},
        )
        _register_fake("alpha", "Alpha", [])

        results = await gather_all(store)

        disabled = [r for r in results if r.key_disabled]
        assert {r.key_ref for r in disabled} == {key_ref(env_raw), key_ref(db_raw)}
        assert all(r.name == "alpha" for r in disabled)
        assert all(r.ok is False and r.error == "key_disabled" for r in disabled)
        # The active DB row is not duplicated as a disabled card.
        assert len(disabled) == 2

    @pytest.mark.asyncio
    async def test_disabled_cards_are_only_built_for_registered_providers(self):
        store = _QuotaKeyStore(
            db_rows={
                "orphan": [
                    SimpleNamespace(id="key-9", key_prefix="orphan...9999", status="disabled")
                ],
            },
            db_raw={"key-9": ("orphan", "orphan-disabled-key-999999")},
        )
        _register_fake("alpha", "Alpha", [])

        assert await gather_all(store) == []

    @pytest.mark.asyncio
    async def test_disabled_card_is_dropped_when_the_key_is_still_live(self):
        """One credential yields one card, even when two sources record it.

        A raw value present in a live pool and in a disabled DB row used to
        produce both an active and a "Key disabled" card for the same key.
        """
        shared = "alpha-shared-key-000000"
        store = _QuotaKeyStore(
            db_rows={
                "alpha": [
                    SimpleNamespace(id="key-1", key_prefix="alpha...0000", status="disabled"),
                ],
            },
            db_raw={"key-1": ("alpha", shared)},
        )
        _register_fake("alpha", "Alpha", [_result("alpha", "Alpha", key=shared)])

        results = await gather_all(store)

        cards = [r for r in results if r.key_ref == key_ref(shared)]
        assert len(cards) == 1
        assert cards[0].key_disabled is False

    @pytest.mark.asyncio
    async def test_gather_all_tolerates_store_failures(self):
        class _BrokenStore(_QuotaKeyStore):
            async def list_disabled_provider_env_keys(self, provider: str):
                raise RuntimeError("boom")

            async def list_provider_keys(self, provider: str | None = None):
                raise RuntimeError("boom")

        _register_fake("alpha", "Alpha", [_result("alpha", "Alpha")])
        _register_fake("beta", "Beta", [_result("beta", "Beta")])

        results = await gather_all(_BrokenStore())

        assert [r.name for r in results] == ["alpha", "beta"]
        assert not any(r.key_disabled for r in results)
