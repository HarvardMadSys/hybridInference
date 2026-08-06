"""Tests for the admin provider-quotas module."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.adapters import dynamic_keys
from serving.adapters.key_pool import KeyPool
from serving.admin.provider_key_probe import ProviderKeyProbeError
from serving.admin.provider_quotas import (
    _discover_env_keys,
    _discover_provider_keys,
    _fetch_featherless_concurrency_usage,
    _mask_key,
    _next_reset,
    _parse_iso,
    fetch_chutes,
    fetch_featherless,
    fetch_kimi,
    fetch_minimax,
    fetch_ollama,
    fetch_zai,
    gather_all,
    key_ref,
)
from serving.servers.deps import AppServices, verify_admin_access
from serving.servers.routers import admin as admin_router


@pytest.fixture(autouse=True)
def _reset_featherless_fetch_state():
    import serving.admin.provider_quotas as provider_quotas

    provider_quotas._FEATHERLESS_CACHE = None
    provider_quotas._FEATHERLESS_FETCH_SIGNATURE = None
    provider_quotas._FEATHERLESS_FETCH_TASK = None
    provider_quotas._FEATHERLESS_FETCH_LOCK = None
    provider_quotas._FEATHERLESS_FETCH_LOCK_LOOP = None
    yield
    provider_quotas._FEATHERLESS_CACHE = None
    provider_quotas._FEATHERLESS_FETCH_SIGNATURE = None
    provider_quotas._FEATHERLESS_FETCH_TASK = None
    provider_quotas._FEATHERLESS_FETCH_LOCK = None
    provider_quotas._FEATHERLESS_FETCH_LOCK_LOOP = None


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


def _mock_aiohttp_get(
    *, status: int = 200, json_data: Any | None = None, raise_exc: Exception | None = None
):
    """Build a context-manager mock for `aiohttp.ClientSession().get(...)`."""
    response = MagicMock()
    response.status = status
    response.json = AsyncMock(return_value={} if json_data is None else json_data)
    response.text = AsyncMock(return_value="")

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=response)
    cm.__aexit__ = AsyncMock(return_value=None)

    session = MagicMock()
    if raise_exc is not None:
        session.get = MagicMock(side_effect=raise_exc)
    else:
        session.get = MagicMock(return_value=cm)

    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=None)
    return session_cm


def _mock_aiohttp_multi_get(responses: list[tuple[int, dict | None]]):
    """Build a session mock whose `get` returns each response context manager in order.

    Each entry is `(status, json_data)`. A fresh response context manager is
    created per entry so multiple sequential `session.get(...)` calls each
    produce their own response.
    """
    cms = []
    for status, json_data in responses:
        response = MagicMock()
        response.status = status
        response.json = AsyncMock(return_value=json_data or {})
        response.text = AsyncMock(return_value="")
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=response)
        cm.__aexit__ = AsyncMock(return_value=None)
        cms.append(cm)

    session = MagicMock()
    session.get = MagicMock(side_effect=cms)

    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=None)
    return session_cm


def _mock_html_session(html: str, *, status: int = 200):
    """Build a session mock whose `get` returns an HTML (`.text`) response."""
    response = MagicMock()
    response.status = status
    response.text = AsyncMock(return_value=html)
    response.json = AsyncMock(return_value={})
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=response)
    cm.__aexit__ = AsyncMock(return_value=None)
    session = MagicMock()
    session.get = MagicMock(return_value=cm)
    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=None)
    return session_cm


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


class TestFetchChutes:
    @pytest.mark.asyncio
    async def test_not_configured_when_key_missing(self, monkeypatch):
        monkeypatch.delenv("CHUTES_API_KEY", raising=False)
        results = await fetch_chutes()
        assert len(results) == 1
        result = results[0]
        assert result.ok is False
        assert result.error == "not_configured"
        assert result.key_configured is False
        assert result.name == "chutes"
        assert result.display_name == "Chutes"

    @pytest.mark.asyncio
    async def test_success_returns_usages(self, monkeypatch):
        monkeypatch.setenv("CHUTES_API_KEY", "cpk_abcdef1234567890xyz")
        sub_payload = {
            "anchor_date": "2026-04-11T17:07:09",
            "four_hour": {
                "usage": 0.0,
                "cap": 8.333,
                "remaining": 8.333,
                "reset_at": "2026-05-02T04:00:00+00:00",
            },
            "monthly": {
                "usage": 13.204,
                "cap": 100.0,
                "remaining": 86.796,
                "reset_at": "2026-05-11T17:07:09+00:00",
            },
        }
        quotas_payload = [
            {"chute_id": "*", "is_default": True, "quota": 5000},
        ]
        # Today's UTC midnight is the daily-window start. Pick buckets relative
        # to "now" so the test is stable regardless of date.
        now = datetime.now(timezone.utc)
        today_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        in_window_bucket = (today_midnight + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
        out_window_bucket = (today_midnight - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S")
        future_bucket = (today_midnight + timedelta(days=1, hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
        usage_payload = {
            "total": 5,
            "page": 0,
            "limit": 2000,
            "items": [
                {"bucket": in_window_bucket, "amount": 0.0, "count": 7},
                {"bucket": out_window_bucket, "amount": 0.0, "count": 99},
                # Beyond next reset boundary -> excluded by upper bound
                {"bucket": future_bucket, "amount": 0.0, "count": 1000},
                # Non-integer float -> excluded
                {"bucket": in_window_bucket, "amount": 0.0, "count": 1.5},
                # bool is a subclass of int but should be rejected
                {"bucket": in_window_bucket, "amount": 0.0, "count": True},
            ],
        }
        with patch(
            "serving.admin.provider_quotas.aiohttp.ClientSession",
            return_value=_mock_aiohttp_multi_get(
                [(200, sub_payload), (200, quotas_payload), (200, usage_payload)],
            ),
        ):
            results = await fetch_chutes()
        assert len(results) == 1
        result = results[0]
        assert result.ok is True
        assert result.key_configured is True
        assert result.key_masked == "cpk_abcd...3xyz" or result.key_masked.startswith("cpk_abcd")
        monthly = next(
            u for u in result.usages if u.label.lower().startswith("month") and u.unit == "USD"
        )
        assert monthly.used == 13.204
        assert monthly.limit == 100.0
        daily_req = next(
            u for u in result.usages if u.label == "Daily requests" and u.unit == "requests"
        )
        assert daily_req.used == 7.0
        assert daily_req.limit == 5000.0

    @pytest.mark.asyncio
    async def test_request_counts_failure_does_not_break_usd_usages(self, monkeypatch):
        monkeypatch.setenv("CHUTES_API_KEY", "cpk_abcdef1234567890xyz")
        sub_payload = {
            "anchor_date": "2026-04-11T17:07:09",
            "four_hour": {
                "usage": 0.0,
                "cap": 8.333,
                "remaining": 8.333,
                "reset_at": "2026-05-02T04:00:00+00:00",
            },
            "monthly": {
                "usage": 13.204,
                "cap": 100.0,
                "remaining": 86.796,
                "reset_at": "2026-05-11T17:07:09+00:00",
            },
        }
        # quotas endpoint 500 -> daily cap unknown -> request-count row dropped
        with patch(
            "serving.admin.provider_quotas.aiohttp.ClientSession",
            return_value=_mock_aiohttp_multi_get(
                [(200, sub_payload), (500, None)],
            ),
        ):
            results = await fetch_chutes()
        assert len(results) == 1
        result = results[0]
        assert result.ok is True
        usd_usages = [u for u in result.usages if u.unit == "USD"]
        assert len(usd_usages) == 2
        request_usages = [u for u in result.usages if u.unit == "requests"]
        assert request_usages == []


class TestFetchZai:
    @pytest.mark.asyncio
    async def test_not_configured_when_key_missing(self, monkeypatch):
        monkeypatch.delenv("ZAI_API_KEY", raising=False)
        results = await fetch_zai()
        assert len(results) == 1
        result = results[0]
        assert result.ok is False
        assert result.error == "not_configured"
        assert result.name == "zai"

    @pytest.mark.asyncio
    async def test_success_parses_token_and_time_limits(self, monkeypatch):
        monkeypatch.setenv("ZAI_API_KEY", "zai_abc1234567890xyz9")
        time_reset_ms = 1779844254994
        tokens_reset_ms = 1777754666484
        payload = {
            "code": 200,
            "data": {
                "limits": [
                    {
                        "type": "TIME_LIMIT",
                        "usage": 4000,
                        "currentValue": 0,
                        "remaining": 4000,
                        "percentage": 0,
                        "nextResetTime": time_reset_ms,
                    },
                    {
                        "type": "TOKENS_LIMIT",
                        "percentage": 6,
                        "nextResetTime": tokens_reset_ms,
                    },
                ]
            },
        }
        with patch(
            "serving.admin.provider_quotas.aiohttp.ClientSession",
            return_value=_mock_aiohttp_get(status=200, json_data=payload),
        ):
            results = await fetch_zai()
        result = results[0]
        assert result.ok is True
        assert len(result.usages) == 2
        labels = [u.label for u in result.usages]
        assert any("Token" in label for label in labels)
        assert any("Time" in label for label in labels)
        time_use = next(u for u in result.usages if "Time" in u.label)
        assert time_use.used == 0.0
        assert time_use.limit == 4000.0
        assert time_use.unit == "minutes"
        assert time_use.reset_at == datetime.fromtimestamp(time_reset_ms / 1000, tz=timezone.utc)
        token_use = next(u for u in result.usages if "Token" in u.label)
        assert token_use.used == 6.0
        assert token_use.limit == 100.0
        assert token_use.unit == "%"
        assert token_use.reset_at == datetime.fromtimestamp(tokens_reset_ms / 1000, tz=timezone.utc)
        assert time_use.reset_at != token_use.reset_at

    @pytest.mark.asyncio
    async def test_missing_next_reset_time_yields_none(self, monkeypatch):
        monkeypatch.setenv("ZAI_API_KEY", "zai_abc1234567890xyz9")
        time_reset_ms = 1779844254994
        payload = {
            "code": 200,
            "data": {
                "limits": [
                    {
                        "type": "TIME_LIMIT",
                        "usage": 4000,
                        "currentValue": 0,
                        "remaining": 4000,
                        "percentage": 0,
                        "nextResetTime": time_reset_ms,
                    },
                    {"type": "TOKENS_LIMIT", "percentage": 8},
                ]
            },
        }
        with patch(
            "serving.admin.provider_quotas.aiohttp.ClientSession",
            return_value=_mock_aiohttp_get(status=200, json_data=payload),
        ):
            results = await fetch_zai()
        result = results[0]
        assert result.ok is True
        time_use = next(u for u in result.usages if "Time" in u.label)
        token_use = next(u for u in result.usages if "Token" in u.label)
        assert time_use.reset_at == datetime.fromtimestamp(time_reset_ms / 1000, tz=timezone.utc)
        assert token_use.reset_at is None

    @pytest.mark.asyncio
    async def test_invalid_next_reset_time_yields_none(self, monkeypatch):
        monkeypatch.setenv("ZAI_API_KEY", "zai_abc1234567890xyz9")
        payload = {
            "code": 200,
            "data": {
                "limits": [
                    {
                        "type": "TIME_LIMIT",
                        "usage": 4000,
                        "currentValue": 0,
                        "remaining": 4000,
                        "percentage": 0,
                        "nextResetTime": "not-a-number",
                    },
                    {
                        "type": "TOKENS_LIMIT",
                        "percentage": 8,
                        "nextResetTime": True,
                    },
                ]
            },
        }
        with patch(
            "serving.admin.provider_quotas.aiohttp.ClientSession",
            return_value=_mock_aiohttp_get(status=200, json_data=payload),
        ):
            results = await fetch_zai()
        result = results[0]
        assert result.ok is True
        for u in result.usages:
            assert u.reset_at is None

    @pytest.mark.asyncio
    async def test_auth_failed_on_401(self, monkeypatch):
        monkeypatch.setenv("ZAI_API_KEY", "zai_abc1234567890xyz9")
        with patch(
            "serving.admin.provider_quotas.aiohttp.ClientSession",
            return_value=_mock_aiohttp_get(status=401),
        ):
            results = await fetch_zai()
        assert results[0].ok is False
        assert results[0].error == "auth_failed"

    @pytest.mark.asyncio
    async def test_parse_error_on_unexpected_shape(self, monkeypatch):
        monkeypatch.setenv("ZAI_API_KEY", "zai_abc1234567890xyz9")
        with patch(
            "serving.admin.provider_quotas.aiohttp.ClientSession",
            return_value=_mock_aiohttp_get(status=200, json_data={"unrelated": "junk"}),
        ):
            results = await fetch_zai()
        assert results[0].ok is False
        assert results[0].error == "parse_error"


class TestFetchMinimax:
    @pytest.fixture(autouse=True)
    def _no_api_key(self, monkeypatch):
        """Default to the cookie fallback path unless a test opts into the API key."""
        monkeypatch.delenv("MINIMAX_API_KEY", raising=False)

    @pytest.mark.asyncio
    async def test_api_key_path_preferred_over_cookie(self, monkeypatch):
        """With MINIMAX_API_KEY configured, the official token-plan endpoint wins."""
        monkeypatch.setenv("MINIMAX_API_KEY", "mm-api-key-abcdefghijklmnop")
        monkeypatch.setenv("MINIMAX_SESSION_COOKIE", "session=abcdefghijklmnop")

        payload = {
            "model_remains": [
                {
                    "model_name": "general",
                    "current_interval_remaining_percent": 98,
                    "end_time": 1781085600000,
                    "current_weekly_remaining_percent": 100,
                    "weekly_end_time": 1781481600000,
                }
            ],
            "base_resp": {"status_code": 0, "status_msg": "success"},
        }
        session_cm = _mock_aiohttp_get(status=200, json_data=payload)
        with patch(
            "serving.admin.provider_quotas.aiohttp.ClientSession",
            return_value=session_cm,
        ):
            results = await fetch_minimax()

        assert len(results) == 1
        result = results[0]
        assert result.ok is True

        session = session_cm.__aenter__.return_value
        url = session.get.call_args.args[0]
        headers = session.get.call_args.kwargs["headers"]
        assert url == "https://api.minimax.io/v1/token_plan/remains"
        assert headers["Authorization"].startswith("Bearer mm-api-key")

        interval = next(u for u in result.usages if u.label == "general (interval)")
        assert interval.unit == "%"
        assert interval.limit == 100.0
        assert interval.used == pytest.approx(2.0)

    @pytest.mark.asyncio
    async def test_not_configured_when_cookie_missing(self, monkeypatch):
        monkeypatch.delenv("MINIMAX_SESSION_COOKIE", raising=False)
        results = await fetch_minimax()
        assert len(results) == 1
        result = results[0]
        assert result.ok is False
        assert result.error == "not_configured"
        assert result.name == "minimax"

    @pytest.mark.asyncio
    async def test_auth_failed_on_cookie_rejected(self, monkeypatch):
        monkeypatch.setenv("MINIMAX_SESSION_COOKIE", "session=abcdefghijklmnop")
        # MiniMax returns HTTP 200 with status_code 1004 in body when cookie missing
        payload = {
            "base_resp": {"status_code": 1004, "status_msg": "cookie is missing, log in again"}
        }
        with patch(
            "serving.admin.provider_quotas.aiohttp.ClientSession",
            return_value=_mock_aiohttp_get(status=200, json_data=payload),
        ):
            result = (await fetch_minimax())[0]
        assert result.ok is False
        assert result.error == "auth_failed"

    @pytest.mark.asyncio
    async def test_not_configured_on_no_subscription(self, monkeypatch):
        monkeypatch.setenv("MINIMAX_SESSION_COOKIE", "session=abcdefghijklmnop")
        payload = {
            "model_remains": None,
            "base_resp": {"status_code": 2062, "status_msg": "no active token plan subscription"},
        }
        with patch(
            "serving.admin.provider_quotas.aiohttp.ClientSession",
            return_value=_mock_aiohttp_get(status=200, json_data=payload),
        ):
            result = (await fetch_minimax())[0]
        assert result.ok is False
        assert result.error == "not_configured"

    @pytest.mark.asyncio
    async def test_parse_error_when_json_root_is_not_object(self, monkeypatch):
        monkeypatch.setenv("MINIMAX_SESSION_COOKIE", "session=abcdefghijklmnop")
        with patch(
            "serving.admin.provider_quotas.aiohttp.ClientSession",
            return_value=_mock_aiohttp_get(status=200, json_data=["unexpected"]),
        ):
            result = (await fetch_minimax())[0]

        assert result.ok is False
        assert result.error == "parse_error"

    @pytest.mark.asyncio
    async def test_success_parses_remains(self, monkeypatch):
        monkeypatch.setenv("MINIMAX_SESSION_COOKIE", "session=abcdefghijklmnop")
        monkeypatch.setenv("MINIMAX_GROUP_ID", "test-group-42")
        payload = {
            "base_resp": {"status_code": 0, "status_msg": "success"},
            "model_remains": [
                {
                    "model_name": "MiniMax-M2.7",
                    "start_time": 1777734000000,
                    "end_time": 1777752000000,
                    "remains_time": 8376729,
                    "current_interval_total_count": 4500,
                    "current_interval_usage_count": 1200,
                    "current_weekly_total_count": 30000,
                    "current_weekly_usage_count": 5000,
                    "weekly_start_time": 1777248000000,
                    "weekly_end_time": 1777852800000,
                    "weekly_remains_time": 109176729,
                }
            ],
        }
        with (
            patch(
                "serving.admin.provider_quotas.settings",
                minimax_group_id="test-group-42",
            ),
            patch(
                "serving.admin.provider_quotas.aiohttp.ClientSession",
                return_value=_mock_aiohttp_get(status=200, json_data=payload),
            ) as mock_session_cls,
        ):
            result = (await fetch_minimax())[0]
        assert result.ok is True
        assert len(result.usages) == 2
        session_cm = mock_session_cls.return_value
        session = session_cm.__aenter__.return_value
        call_args = session.get.call_args
        assert (
            call_args.args[0]
            == "https://platform.minimax.io/v1/api/openplatform/coding_plan/remains"
        )
        assert call_args.kwargs["headers"].get("x-group-id") == "test-group-42"

        u_interval = result.usages[0]
        assert u_interval.label == "MiniMax-M2.7 (interval)"
        assert u_interval.used == 3300.0
        assert u_interval.limit == 4500.0
        assert u_interval.reset_at == datetime(2026, 5, 2, 20, 0, 0, tzinfo=timezone.utc)

        u_weekly = result.usages[1]
        assert u_weekly.label == "MiniMax-M2.7 (weekly)"
        assert u_weekly.used == 25000.0
        assert u_weekly.limit == 30000.0
        assert u_weekly.reset_at == datetime(2026, 5, 4, 0, 0, 0, tzinfo=timezone.utc)

    @pytest.mark.asyncio
    async def test_success_without_group_id(self, monkeypatch):
        monkeypatch.setenv("MINIMAX_SESSION_COOKIE", "session=abcdefghijklmnop")
        monkeypatch.delenv("MINIMAX_GROUP_ID", raising=False)
        payload = {
            "base_resp": {"status_code": 0, "status_msg": "success"},
            "model_remains": [
                {
                    "model_name": "MiniMax-M*",
                    "start_time": 1777734000000,
                    "end_time": 1777752000000,
                    "remains_time": 8376729,
                    "current_interval_total_count": 4500,
                    "current_interval_usage_count": 4500,
                    "current_weekly_total_count": 0,
                    "current_weekly_usage_count": 0,
                    "weekly_start_time": 1777248000000,
                    "weekly_end_time": 1777852800000,
                    "weekly_remains_time": 109176729,
                }
            ],
        }
        with patch(
            "serving.admin.provider_quotas.aiohttp.ClientSession",
            return_value=_mock_aiohttp_get(status=200, json_data=payload),
        ) as mock_session_cls:
            result = (await fetch_minimax())[0]
        assert result.ok is True
        assert len(result.usages) == 1
        session_cm = mock_session_cls.return_value
        session = session_cm.__aenter__.return_value
        sent_headers = session.get.call_args.kwargs["headers"]
        assert "x-group-id" not in sent_headers

        u = result.usages[0]
        assert u.label == "MiniMax-M* (interval)"
        assert u.used == 0.0
        assert u.limit == 4500.0

    @pytest.mark.asyncio
    async def test_uses_settings_cookie_when_env_missing(self, monkeypatch):
        monkeypatch.delenv("MINIMAX_SESSION_COOKIE", raising=False)
        payload = {
            "base_resp": {"status_code": 0, "status_msg": "success"},
            "model_remains": [
                {
                    "model_name": "MiniMax-M2.7",
                    "end_time": 1777752000000,
                    "current_interval_total_count": 4500,
                    "current_interval_usage_count": 1200,
                    "current_weekly_total_count": 0,
                }
            ],
        }
        with (
            patch(
                "serving.admin.provider_quotas.settings",
                minimax_session_cookie="session=fromsettings1234",
                minimax_group_id="",
            ),
            patch(
                "serving.admin.provider_quotas.aiohttp.ClientSession",
                return_value=_mock_aiohttp_get(status=200, json_data=payload),
            ) as mock_session_cls,
        ):
            result = (await fetch_minimax())[0]

        assert result.ok is True
        session_cm = mock_session_cls.return_value
        session = session_cm.__aenter__.return_value
        assert session.get.call_args.kwargs["headers"]["Cookie"] == "session=fromsettings1234"

    @pytest.mark.asyncio
    async def test_success_parses_nested_remain_count_shape(self, monkeypatch):
        monkeypatch.setenv("MINIMAX_SESSION_COOKIE", "session=abcdefghijklmnop")
        payload = {
            "base_resp": {"status_code": 0, "status_msg": "success"},
            "data": {
                "model_remains": [
                    {
                        "model_name": "MiniMax-M3",
                        "remain_count": 720,
                        "total_count": 1000,
                        "end_time": "2026-04-30T00:00:00Z",
                    }
                ]
            },
        }
        with patch(
            "serving.admin.provider_quotas.aiohttp.ClientSession",
            return_value=_mock_aiohttp_get(status=200, json_data=payload),
        ):
            result = (await fetch_minimax())[0]

        assert result.ok is True
        assert len(result.usages) == 1
        usage = result.usages[0]
        assert usage.label == "MiniMax-M3 (interval)"
        assert usage.used == 280.0
        assert usage.limit == 1000.0
        assert usage.reset_at == datetime(2026, 4, 30, 0, 0, 0, tzinfo=timezone.utc)

    @pytest.mark.asyncio
    async def test_extracts_group_id_from_cookie(self, monkeypatch):
        cookie = "_token=abc; minimax_group_id_v2=group-from-cookie; locale_preference=en"
        monkeypatch.setenv("MINIMAX_SESSION_COOKIE", cookie)
        payload = {
            "base_resp": {"status_code": 0, "status_msg": "success"},
            "model_remains": [
                {
                    "model_name": "MiniMax-M3",
                    "current_interval_total_count": 1000,
                    "current_interval_usage_count": 200,
                }
            ],
        }
        with (
            patch(
                "serving.admin.provider_quotas.settings",
                minimax_group_id="",
                minimax_session_cookie="",
            ),
            patch(
                "serving.admin.provider_quotas.aiohttp.ClientSession",
                return_value=_mock_aiohttp_get(status=200, json_data=payload),
            ) as mock_session_cls,
        ):
            result = (await fetch_minimax())[0]

        assert result.ok is True
        session_cm = mock_session_cls.return_value
        session = session_cm.__aenter__.return_value
        headers = session.get.call_args.kwargs["headers"]
        assert headers["x-group-id"] == "group-from-cookie"
        assert headers["Referer"] == "https://platform.minimax.io/console/usage"
        assert headers["Accept"] == "application/json, text/plain, */*"

    @pytest.mark.asyncio
    async def test_success_parses_credit_based_cookie_shape(self, monkeypatch):
        monkeypatch.setenv("MINIMAX_SESSION_COOKIE", "_token=abc; minimax_group_id_v2=test-group")
        payload = {
            "base_resp": {"status_code": 0, "status_msg": "success"},
            "data": {
                "model_remains": [
                    {
                        "model_name": "Token Plan",
                        "current_interval_total_credits": 5000,
                        "current_interval_remaining_credits": 3750,
                        "end_time": "2026-06-07T01:00:00Z",
                        "current_weekly_total_credits": 30000,
                        "current_weekly_remaining_credits": 22000,
                        "weekly_end_time": "2026-06-08T00:00:00Z",
                    }
                ]
            },
        }
        with patch(
            "serving.admin.provider_quotas.aiohttp.ClientSession",
            return_value=_mock_aiohttp_get(status=200, json_data=payload),
        ):
            result = (await fetch_minimax())[0]

        assert result.ok is True
        assert [(u.label, u.used, u.limit, u.unit, u.reset_at) for u in result.usages] == [
            (
                "Token Plan (interval)",
                1250.0,
                5000.0,
                "credits",
                datetime(2026, 6, 7, 1, 0, 0, tzinfo=timezone.utc),
            ),
            (
                "Token Plan (weekly)",
                8000.0,
                30000.0,
                "credits",
                datetime(2026, 6, 8, 0, 0, 0, tzinfo=timezone.utc),
            ),
        ]

    @pytest.mark.asyncio
    async def test_success_parses_percent_only_cookie_shape(self, monkeypatch):
        monkeypatch.setenv("MINIMAX_SESSION_COOKIE", "_token=abc; minimax_group_id_v2=test-group")
        payload = {
            "base_resp": {"status_code": 0, "status_msg": "success"},
            "data": {
                "model_remains": [
                    {
                        "model_name": "Token Plan",
                        "current_interval_remaining_percent": 25,
                        "end_time": "2026-06-07T01:00:00Z",
                        "current_weekly_remaining_percent": 40,
                        "weekly_end_time": "2026-06-08T00:00:00Z",
                    }
                ]
            },
        }
        with patch(
            "serving.admin.provider_quotas.aiohttp.ClientSession",
            return_value=_mock_aiohttp_get(status=200, json_data=payload),
        ):
            result = (await fetch_minimax())[0]

        assert result.ok is True
        assert [(u.label, u.used, u.limit, u.unit, u.reset_at) for u in result.usages] == [
            (
                "Token Plan (interval)",
                75.0,
                100.0,
                "%",
                datetime(2026, 6, 7, 1, 0, 0, tzinfo=timezone.utc),
            ),
            (
                "Token Plan (weekly)",
                60.0,
                100.0,
                "%",
                datetime(2026, 6, 8, 0, 0, 0, tzinfo=timezone.utc),
            ),
        ]

    @pytest.mark.asyncio
    async def test_weekly_credit_unit_not_overwritten_by_interval_percent(self, monkeypatch):
        monkeypatch.setenv("MINIMAX_SESSION_COOKIE", "_token=abc; minimax_group_id_v2=test-group")
        payload = {
            "base_resp": {"status_code": 0, "status_msg": "success"},
            "data": {
                "model_remains": [
                    {
                        "model_name": "Token Plan",
                        "current_interval_remaining_percent": 25,
                        "current_weekly_total_credits": 30000,
                        "current_weekly_remaining_credits": 22000,
                    }
                ]
            },
        }
        with patch(
            "serving.admin.provider_quotas.aiohttp.ClientSession",
            return_value=_mock_aiohttp_get(status=200, json_data=payload),
        ):
            result = (await fetch_minimax())[0]

        assert result.ok is True
        assert [(u.label, u.used, u.limit, u.unit) for u in result.usages] == [
            ("Token Plan (interval)", 75.0, 100.0, "%"),
            ("Token Plan (weekly)", 8000.0, 30000.0, "credits"),
        ]

    @pytest.mark.asyncio
    async def test_percent_fields_override_zero_absolute_counters(self, monkeypatch):
        monkeypatch.setenv("MINIMAX_SESSION_COOKIE", "_token=abc; minimax_group_id_v2=test-group")
        payload = {
            "base_resp": {"status_code": 0, "status_msg": "success"},
            "data": {
                "model_remains": [
                    {
                        "model_name": "Token Plan",
                        "current_interval_total_count": 0,
                        "current_interval_usage_count": 0,
                        "current_interval_used_count": 0,
                        "current_interval_remaining_percent": 25,
                        "current_weekly_total_count": 0,
                        "current_weekly_usage_count": 0,
                        "current_weekly_used_count": 0,
                        "current_weekly_remaining_percent": 40,
                    }
                ]
            },
        }
        with patch(
            "serving.admin.provider_quotas.aiohttp.ClientSession",
            return_value=_mock_aiohttp_get(status=200, json_data=payload),
        ):
            result = (await fetch_minimax())[0]

        assert result.ok is True
        assert [(u.label, u.used, u.limit, u.unit) for u in result.usages] == [
            ("Token Plan (interval)", 75.0, 100.0, "%"),
            ("Token Plan (weekly)", 60.0, 100.0, "%"),
        ]

    @pytest.mark.asyncio
    async def test_uses_api_key_endpoint_with_bearer_auth(self, monkeypatch):
        # API key configured -> hit /token_plan/remains with Bearer auth, no cookie.
        monkeypatch.setenv("MINIMAX_API_KEY", "minimax_key_1234567890abcd")
        monkeypatch.setenv("MINIMAX_BASE_URL", "https://api.minimax.io/v1")
        monkeypatch.delenv("MINIMAX_SESSION_COOKIE", raising=False)
        payload = {
            "base_resp": {"status_code": 0, "status_msg": "success"},
            "model_remains": [
                {
                    "model_name": "MiniMax-M2.7",
                    "end_time": 1777752000000,
                    "current_interval_total_count": 4500,
                    "current_interval_usage_count": 1200,
                }
            ],
        }
        with patch(
            "serving.admin.provider_quotas.aiohttp.ClientSession",
            return_value=_mock_aiohttp_get(status=200, json_data=payload),
        ) as mock_session_cls:
            result = (await fetch_minimax())[0]

        assert result.ok is True
        assert result.usages[0].used == 3300.0
        assert result.usages[0].limit == 4500.0
        session = mock_session_cls.return_value.__aenter__.return_value
        call_args = session.get.call_args
        assert call_args.args[0] == "https://api.minimax.io/v1/token_plan/remains"
        headers = call_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer minimax_key_1234567890abcd"
        assert "Cookie" not in headers

    @pytest.mark.asyncio
    async def test_api_key_takes_precedence_over_cookie(self, monkeypatch):
        monkeypatch.setenv("MINIMAX_API_KEY", "minimax_key_1234567890abcd")
        monkeypatch.setenv("MINIMAX_SESSION_COOKIE", "session=abcdefghijklmnop")
        payload = {
            "base_resp": {"status_code": 0, "status_msg": "success"},
            "model_remains": [
                {
                    "model_name": "MiniMax-M2.7",
                    "current_interval_total_count": 1000,
                    "current_interval_usage_count": 200,
                }
            ],
        }
        with patch(
            "serving.admin.provider_quotas.aiohttp.ClientSession",
            return_value=_mock_aiohttp_get(status=200, json_data=payload),
        ) as mock_session_cls:
            result = (await fetch_minimax())[0]

        assert result.ok is True
        session = mock_session_cls.return_value.__aenter__.return_value
        assert session.get.call_args.args[0].endswith("/token_plan/remains")

    @pytest.mark.asyncio
    async def test_api_key_auth_failed_on_401(self, monkeypatch):
        monkeypatch.setenv("MINIMAX_API_KEY", "minimax_key_1234567890abcd")
        monkeypatch.delenv("MINIMAX_SESSION_COOKIE", raising=False)
        with patch(
            "serving.admin.provider_quotas.aiohttp.ClientSession",
            return_value=_mock_aiohttp_get(status=401),
        ):
            result = (await fetch_minimax())[0]
        assert result.ok is False
        assert result.error == "auth_failed"

    @pytest.mark.asyncio
    async def test_api_key_auth_failed_on_status_1004(self, monkeypatch):
        # HTTP 200 but MiniMax signals a key/auth problem in the body.
        monkeypatch.setenv("MINIMAX_API_KEY", "minimax_key_1234567890abcd")
        monkeypatch.delenv("MINIMAX_SESSION_COOKIE", raising=False)
        payload = {
            "base_resp": {
                "status_code": 1004,
                "status_msg": "carry the API secret key in the Authorization field",
            }
        }
        with patch(
            "serving.admin.provider_quotas.aiohttp.ClientSession",
            return_value=_mock_aiohttp_get(status=200, json_data=payload),
        ):
            result = (await fetch_minimax())[0]
        assert result.ok is False
        assert result.error == "auth_failed"

    @pytest.mark.asyncio
    async def test_empty_base_url_falls_back_to_default_endpoint(self, monkeypatch):
        # MINIMAX_BASE_URL set but empty must not yield a host-less URL.
        monkeypatch.setenv("MINIMAX_API_KEY", "minimax_key_1234567890abcd")
        monkeypatch.setenv("MINIMAX_BASE_URL", "")
        monkeypatch.delenv("MINIMAX_SESSION_COOKIE", raising=False)
        payload = {
            "base_resp": {"status_code": 0, "status_msg": "success"},
            "model_remains": [
                {
                    "model_name": "MiniMax-M2.7",
                    "current_interval_total_count": 1000,
                    "current_interval_usage_count": 200,
                }
            ],
        }
        with patch(
            "serving.admin.provider_quotas.aiohttp.ClientSession",
            return_value=_mock_aiohttp_get(status=200, json_data=payload),
        ) as mock_session_cls:
            result = (await fetch_minimax())[0]
        assert result.ok is True
        session = mock_session_cls.return_value.__aenter__.return_value
        assert session.get.call_args.args[0] == "https://api.minimax.io/v1/token_plan/remains"

    @pytest.mark.asyncio
    async def test_rejected_api_key_falls_back_to_cookie(self, monkeypatch):
        # A PAYG key is rejected by /token_plan/remains; a configured cookie
        # should still be tried rather than surfacing a spurious auth error.
        monkeypatch.setenv("MINIMAX_API_KEY", "minimax_key_1234567890abcd")
        monkeypatch.setenv("MINIMAX_SESSION_COOKIE", "session=abcdefghijklmnop")
        api_key_resp = {"base_resp": {"status_code": 1004, "status_msg": "bad key"}}
        cookie_resp = {
            "base_resp": {"status_code": 0, "status_msg": "success"},
            "model_remains": [
                {
                    "model_name": "MiniMax-M2.7",
                    "current_interval_total_count": 4500,
                    "current_interval_usage_count": 1200,
                }
            ],
        }

        # First ClientSession (API key) returns 1004; second (cookie) succeeds.
        sessions = iter(
            [
                _mock_aiohttp_get(status=200, json_data=api_key_resp),
                _mock_aiohttp_get(status=200, json_data=cookie_resp),
            ]
        )
        with patch(
            "serving.admin.provider_quotas.aiohttp.ClientSession",
            side_effect=lambda *a, **k: next(sessions),
        ):
            result = (await fetch_minimax())[0]
        assert result.ok is True
        assert result.usages[0].used == 3300.0
        assert result.usages[0].limit == 4500.0


class TestFetchKimi:
    @pytest.mark.asyncio
    async def test_not_configured_when_key_missing(self, monkeypatch):
        monkeypatch.delenv("KIMI_CODING_API_KEY", raising=False)
        results = await fetch_kimi()
        assert len(results) == 1
        result = results[0]
        assert result.ok is False
        assert result.error == "not_configured"
        assert result.name == "kimi"

    @pytest.mark.asyncio
    async def test_success_parses_summary_and_limits(self, monkeypatch):
        monkeypatch.setenv("KIMI_CODING_API_KEY", "kimi_abc1234567890xyz9")
        reset_iso = "2026-06-21T05:24:18Z"
        payload = {
            "usage": {"limit": 10000, "remaining": 4000, "reset_at": reset_iso},
            "limits": [
                {
                    "detail": {"limit": 1200, "used": 300},
                    "window": {"duration": 300, "timeUnit": "MINUTE"},
                },
                {
                    "name": "Daily limit",
                    "detail": {"limit": 5000, "remaining": 5000},
                    "window": {"duration": 1, "timeUnit": "DAY"},
                },
            ],
        }
        with patch(
            "serving.admin.provider_quotas.aiohttp.ClientSession",
            return_value=_mock_aiohttp_get(status=200, json_data=payload),
        ):
            results = await fetch_kimi()
        result = results[0]
        assert result.ok is True
        assert result.name == "kimi"
        assert len(result.usages) == 3

        summary = result.usages[0]
        assert summary.label == "Weekly limit"
        assert summary.used == 6000.0  # limit - remaining
        assert summary.limit == 10000.0
        assert summary.unit == "requests"
        assert summary.reset_at == datetime(2026, 6, 21, 5, 24, 18, tzinfo=timezone.utc)

        five_hour = result.usages[1]
        assert five_hour.label == "5h limit"  # 300 minutes -> 5h
        assert five_hour.used == 300.0
        assert five_hour.limit == 1200.0

        daily = result.usages[2]
        assert daily.label == "Daily limit"
        assert daily.used == 0.0  # limit - remaining
        assert daily.limit == 5000.0

    @pytest.mark.asyncio
    async def test_window_time_unit_is_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("KIMI_CODING_API_KEY", "kimi_abc1234567890xyz9")
        payload = {
            "limits": [
                {
                    "detail": {"limit": 1200, "used": 300},
                    "window": {"duration": 300, "timeUnit": "minute"},
                },
            ],
        }
        with patch(
            "serving.admin.provider_quotas.aiohttp.ClientSession",
            return_value=_mock_aiohttp_get(status=200, json_data=payload),
        ):
            results = await fetch_kimi()
        assert results[0].usages[0].label == "5h limit"

    @pytest.mark.asyncio
    async def test_auth_failed_on_401(self, monkeypatch):
        monkeypatch.setenv("KIMI_CODING_API_KEY", "kimi_abc1234567890xyz9")
        with patch(
            "serving.admin.provider_quotas.aiohttp.ClientSession",
            return_value=_mock_aiohttp_get(status=401),
        ):
            results = await fetch_kimi()
        assert results[0].ok is False
        assert results[0].error == "auth_failed"

    @pytest.mark.asyncio
    async def test_no_quota_api_on_404(self, monkeypatch):
        monkeypatch.setenv("KIMI_CODING_API_KEY", "kimi_abc1234567890xyz9")
        with patch(
            "serving.admin.provider_quotas.aiohttp.ClientSession",
            return_value=_mock_aiohttp_get(status=404),
        ):
            results = await fetch_kimi()
        assert results[0].ok is False
        assert results[0].error == "no_quota_api"

    @pytest.mark.asyncio
    async def test_parse_error_on_empty_payload(self, monkeypatch):
        monkeypatch.setenv("KIMI_CODING_API_KEY", "kimi_abc1234567890xyz9")
        with patch(
            "serving.admin.provider_quotas.aiohttp.ClientSession",
            return_value=_mock_aiohttp_get(status=200, json_data={"unrelated": "junk"}),
        ):
            results = await fetch_kimi()
        assert results[0].ok is False
        assert results[0].error == "parse_error"

    @pytest.mark.asyncio
    async def test_parses_body_with_non_json_content_type(self, monkeypatch):
        # Kimi's gateway can serve the body with a non-application/json content
        # type; aiohttp rejects that unless content_type=None is passed. curl
        # ignores the header, so "curl works but the dashboard doesn't".
        monkeypatch.setenv("KIMI_CODING_API_KEY", "kimi_abc1234567890xyz9")
        payload = {
            "limits": [
                {
                    "detail": {"limit": 1200, "used": 300},
                    "window": {"duration": 300, "timeUnit": "MINUTE"},
                },
            ],
        }

        async def _json(content_type: Any = "application/json"):
            if content_type is not None:
                raise aiohttp.ContentTypeError(MagicMock(), ())
            return payload

        response = MagicMock()
        response.status = 200
        response.json = _json
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=response)
        cm.__aexit__ = AsyncMock(return_value=None)
        session = MagicMock()
        session.get = MagicMock(return_value=cm)
        session_cm = MagicMock()
        session_cm.__aenter__ = AsyncMock(return_value=session)
        session_cm.__aexit__ = AsyncMock(return_value=None)

        with patch(
            "serving.admin.provider_quotas.aiohttp.ClientSession",
            return_value=session_cm,
        ):
            results = await fetch_kimi()
        assert results[0].ok is True
        assert results[0].usages[0].label == "5h limit"
        assert results[0].usages[0].used == 300.0

    @pytest.mark.asyncio
    async def test_parses_data_envelope(self, monkeypatch):
        monkeypatch.setenv("KIMI_CODING_API_KEY", "kimi_abc1234567890xyz9")
        payload = {"data": {"limits": [{"detail": {"limit": 1000, "remaining": 250}}]}}
        with patch(
            "serving.admin.provider_quotas.aiohttp.ClientSession",
            return_value=_mock_aiohttp_get(status=200, json_data=payload),
        ):
            results = await fetch_kimi()
        assert results[0].ok is True
        assert results[0].usages[0].used == 750.0  # limit - remaining
        assert results[0].usages[0].limit == 1000.0

    @pytest.mark.asyncio
    async def test_parses_top_level_list(self, monkeypatch):
        # The plural ``/usages`` endpoint can return a bare array of limit
        # objects rather than an object — a clean 200 that previously parsed
        # as empty and surfaced as "parse_error" on the dashboard.
        monkeypatch.setenv("KIMI_CODING_API_KEY", "kimi_abc1234567890xyz9")
        payload = [
            {
                "name": "Weekly limit",
                "detail": {"limit": 10000, "remaining": 4000},
            },
            {
                "detail": {"limit": 1200, "used": 300},
                "window": {"duration": 300, "timeUnit": "MINUTE"},
            },
        ]
        with patch(
            "serving.admin.provider_quotas.aiohttp.ClientSession",
            return_value=_mock_aiohttp_get(status=200, json_data=payload),
        ):
            results = await fetch_kimi()
        assert results[0].ok is True
        assert len(results[0].usages) == 2
        assert results[0].usages[0].label == "Weekly limit"
        assert results[0].usages[0].used == 6000.0  # limit - remaining
        assert results[0].usages[1].label == "5h limit"
        assert results[0].usages[1].used == 300.0

    @pytest.mark.asyncio
    async def test_parses_string_valued_numbers_in_array(self, monkeypatch):
        # Kimi's /usages commonly returns quota figures as numeric strings;
        # they must coerce to floats rather than parsing as empty.
        monkeypatch.setenv("KIMI_CODING_API_KEY", "kimi_abc1234567890xyz9")
        payload = [
            {
                "name": "Weekly limit",
                "detail": {"limit": "10000", "remaining": "4000"},
            },
            {
                "detail": {"limit": "1200", "used": "300"},
                "window": {"duration": 300, "timeUnit": "MINUTE"},
            },
        ]
        with patch(
            "serving.admin.provider_quotas.aiohttp.ClientSession",
            return_value=_mock_aiohttp_get(status=200, json_data=payload),
        ):
            results = await fetch_kimi()
        assert results[0].ok is True
        assert results[0].usages[0].used == 6000.0  # limit - remaining
        assert results[0].usages[0].limit == 10000.0
        assert results[0].usages[1].label == "5h limit"
        assert results[0].usages[1].used == 300.0
        assert results[0].usages[1].limit == 1200.0

    @pytest.mark.asyncio
    async def test_parses_data_envelope_wrapping_list(self, monkeypatch):
        monkeypatch.setenv("KIMI_CODING_API_KEY", "kimi_abc1234567890xyz9")
        payload = {"data": [{"detail": {"limit": 1000, "remaining": 250}}]}
        with patch(
            "serving.admin.provider_quotas.aiohttp.ClientSession",
            return_value=_mock_aiohttp_get(status=200, json_data=payload),
        ):
            results = await fetch_kimi()
        assert results[0].ok is True
        assert results[0].usages[0].used == 750.0  # limit - remaining
        assert results[0].usages[0].limit == 1000.0

    @pytest.mark.asyncio
    async def test_parses_data_envelope_wrapping_usages_key(self, monkeypatch):
        # An envelope whose inner object uses the plural ``usages`` array
        # (the alias accepted alongside ``limits``) must still be unwrapped.
        monkeypatch.setenv("KIMI_CODING_API_KEY", "kimi_abc1234567890xyz9")
        payload = {"data": {"usages": [{"detail": {"limit": 1000, "remaining": 250}}]}}
        with patch(
            "serving.admin.provider_quotas.aiohttp.ClientSession",
            return_value=_mock_aiohttp_get(status=200, json_data=payload),
        ):
            results = await fetch_kimi()
        assert results[0].ok is True
        assert results[0].usages[0].used == 750.0  # limit - remaining
        assert results[0].usages[0].limit == 1000.0


class TestFetchOllama:
    @pytest.fixture(autouse=True)
    def _cookie_only(self, monkeypatch):
        """Default to the cookie path unless a test opts into the API key."""
        monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
        monkeypatch.delenv("OLLAMA_USAGE_URL", raising=False)

    @pytest.mark.asyncio
    async def test_not_configured_when_cookie_missing(self, monkeypatch):
        monkeypatch.delenv("OLLAMA_SESSION_COOKIE", raising=False)
        results = await fetch_ollama()
        assert len(results) == 1
        result = results[0]
        assert result.ok is False
        assert result.error == "not_configured"
        assert result.name == "ollama"

    @pytest.mark.asyncio
    async def test_redirected_to_login_returns_auth_failed(self, monkeypatch):
        monkeypatch.setenv("OLLAMA_SESSION_COOKIE", "ollama_session=abcdefghijklmnop")
        # If cookie is invalid, ollama.com redirects to a sign-in page.
        # We simulate by returning HTML with no usage data and a sign-in link.
        html = "<html><body><a href='/signin'>Sign in</a></body></html>"
        response_mock = MagicMock()
        response_mock.status = 200
        response_mock.text = AsyncMock(return_value=html)
        response_mock.json = AsyncMock(return_value={})
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=response_mock)
        cm.__aexit__ = AsyncMock(return_value=None)
        session = MagicMock()
        session.get = MagicMock(return_value=cm)
        session_cm = MagicMock()
        session_cm.__aenter__ = AsyncMock(return_value=session)
        session_cm.__aexit__ = AsyncMock(return_value=None)
        with patch("serving.admin.provider_quotas.aiohttp.ClientSession", return_value=session_cm):
            results = await fetch_ollama()
        result = results[0]
        assert result.ok is False
        assert result.error in ("auth_failed", "parse_error")

    @pytest.mark.asyncio
    async def test_parses_session_and_weekly_usage(self, monkeypatch):
        monkeypatch.setenv("OLLAMA_SESSION_COOKIE", "ollama_session=abcdefghijklmnop")
        html = """
        <html><body>
          <h2>Usage</h2>
          <div>Session usage 0% used Resets in 2 hours</div>
          <div>Weekly usage 5% used Resets in 2 days</div>
        </body></html>
        """
        response_mock = MagicMock()
        response_mock.status = 200
        response_mock.text = AsyncMock(return_value=html)
        response_mock.json = AsyncMock(return_value={})
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=response_mock)
        cm.__aexit__ = AsyncMock(return_value=None)
        session = MagicMock()
        session.get = MagicMock(return_value=cm)
        session_cm = MagicMock()
        session_cm.__aenter__ = AsyncMock(return_value=session)
        session_cm.__aexit__ = AsyncMock(return_value=None)
        with patch("serving.admin.provider_quotas.aiohttp.ClientSession", return_value=session_cm):
            results = await fetch_ollama()
        result = results[0]
        assert result.ok is True
        assert len(result.usages) >= 2
        labels = [u.label.lower() for u in result.usages]
        assert any("session" in label for label in labels)
        assert any("week" in label for label in labels)
        session_use = next(u for u in result.usages if "session" in u.label.lower())
        assert session_use.used == 0.0
        assert session_use.limit == 100.0
        assert session_use.unit == "%"
        weekly_use = next(u for u in result.usages if "week" in u.label.lower())
        assert weekly_use.used == 5.0
        assert weekly_use.limit == 100.0
        assert weekly_use.unit == "%"

    @pytest.mark.asyncio
    async def test_unparseable_authenticated_page_is_parse_error(self, monkeypatch):
        # A valid cookie returns HTTP 200 but the usage figures aren't in the
        # server-rendered HTML (client-rendered). A stray "Sign out" button or a
        # "Login history" link must not be misreported as auth_failed.
        monkeypatch.setenv("OLLAMA_SESSION_COOKIE", "ollama_session=abcdefghijklmnop")
        html = (
            "<html><body><nav>Account</nav><button>Sign out</button>"
            "<a href='/login'>Login history</a></body></html>"
        )
        with patch(
            "serving.admin.provider_quotas.aiohttp.ClientSession",
            return_value=_mock_html_session(html),
        ):
            result = (await fetch_ollama())[0]
        assert result.ok is False
        assert result.error == "parse_error"

    @pytest.mark.asyncio
    async def test_signed_out_shell_returns_auth_failed(self, monkeypatch):
        # A 200 client-rendered shell with a real sign-in CTA is auth_failed.
        monkeypatch.setenv("OLLAMA_SESSION_COOKIE", "ollama_session=abcdefghijklmnop")
        html = "<html><body><h1>Sign in to Ollama</h1></body></html>"
        with patch(
            "serving.admin.provider_quotas.aiohttp.ClientSession",
            return_value=_mock_html_session(html),
        ):
            result = (await fetch_ollama())[0]
        assert result.ok is False
        assert result.error == "auth_failed"

    @pytest.mark.asyncio
    async def test_bare_signin_cta_returns_auth_failed(self, monkeypatch):
        # An expired cookie can yield a 200 shell whose only CTA is a bare
        # "Sign in" / "Log in" button — still a real auth failure, not a parse
        # failure. The "Login history" link must not flip this to parse_error.
        monkeypatch.setenv("OLLAMA_SESSION_COOKIE", "ollama_session=abcdefghijklmnop")
        html = (
            "<html><head><title>Sign in - Ollama</title></head><body>"
            "<button>Sign in</button><a href='/account'>Login history</a></body></html>"
        )
        with patch(
            "serving.admin.provider_quotas.aiohttp.ClientSession",
            return_value=_mock_html_session(html),
        ):
            result = (await fetch_ollama())[0]
        assert result.ok is False
        assert result.error == "auth_failed"

    @pytest.mark.asyncio
    async def test_api_key_usage_endpoint_parsed_when_available(self, monkeypatch):
        monkeypatch.setenv("OLLAMA_API_KEY", "ollama_key_1234567890abcd")
        monkeypatch.delenv("OLLAMA_SESSION_COOKIE", raising=False)
        payload = {
            "session": {"percent": 12, "reset_at": "2026-06-22T05:00:00Z"},
            "weekly": {"used": 30, "limit": 100, "reset_at": "2026-06-28T00:00:00Z"},
        }
        with patch(
            "serving.admin.provider_quotas.aiohttp.ClientSession",
            return_value=_mock_aiohttp_get(status=200, json_data=payload),
        ) as mock_session_cls:
            result = (await fetch_ollama())[0]
        assert result.ok is True
        session = mock_session_cls.return_value.__aenter__.return_value
        call_args = session.get.call_args
        assert call_args.args[0] == "https://ollama.com/api/account/usage"
        assert call_args.kwargs["headers"]["Authorization"] == "Bearer ollama_key_1234567890abcd"
        assert "Cookie" not in call_args.kwargs["headers"]
        labels = {u.label.lower(): u for u in result.usages}
        assert labels["session usage"].used == 12.0
        assert labels["session usage"].limit == 100.0
        assert labels["session usage"].unit == "%"
        assert labels["weekly usage"].used == 30.0
        assert labels["weekly usage"].limit == 100.0

    @pytest.mark.asyncio
    async def test_usage_url_override_respected(self, monkeypatch):
        monkeypatch.setenv("OLLAMA_API_KEY", "ollama_key_1234567890abcd")
        monkeypatch.setenv("OLLAMA_USAGE_URL", "https://example.test/usage")
        monkeypatch.delenv("OLLAMA_SESSION_COOKIE", raising=False)
        payload = {"session": {"percent": 5}}
        with patch(
            "serving.admin.provider_quotas.aiohttp.ClientSession",
            return_value=_mock_aiohttp_get(status=200, json_data=payload),
        ) as mock_session_cls:
            result = (await fetch_ollama())[0]
        assert result.ok is True
        session = mock_session_cls.return_value.__aenter__.return_value
        assert session.get.call_args.args[0] == "https://example.test/usage"

    @pytest.mark.asyncio
    async def test_api_key_404_no_cookie_returns_no_quota_api(self, monkeypatch):
        # The usage endpoint doesn't exist yet and no cookie is configured.
        monkeypatch.setenv("OLLAMA_API_KEY", "ollama_key_1234567890abcd")
        monkeypatch.delenv("OLLAMA_SESSION_COOKIE", raising=False)
        with patch(
            "serving.admin.provider_quotas.aiohttp.ClientSession",
            return_value=_mock_aiohttp_get(status=404),
        ):
            result = (await fetch_ollama())[0]
        assert result.ok is False
        assert result.error == "no_quota_api"

    @pytest.mark.asyncio
    async def test_api_key_404_falls_back_to_cookie(self, monkeypatch):
        # API-key probe 404s (no endpoint yet) but a cookie is configured: the
        # cookie dashboard scrape should still produce usage.
        monkeypatch.setenv("OLLAMA_API_KEY", "ollama_key_1234567890abcd")
        monkeypatch.setenv("OLLAMA_SESSION_COOKIE", "ollama_session=abcdefghijklmnop")
        html = """
        <html><body>
          <div>Session usage 0% used Resets in 2 hours</div>
          <div>Weekly usage 5% used Resets in 2 days</div>
        </body></html>
        """
        sessions = iter([_mock_aiohttp_get(status=404), _mock_html_session(html)])
        with patch(
            "serving.admin.provider_quotas.aiohttp.ClientSession",
            side_effect=lambda *a, **k: next(sessions),
        ):
            result = (await fetch_ollama())[0]
        assert result.ok is True
        labels = [u.label.lower() for u in result.usages]
        assert any("session" in label for label in labels)
        assert any("week" in label for label in labels)

    @pytest.mark.asyncio
    async def test_api_key_server_error_falls_back_to_cookie(self, monkeypatch):
        # A transient 5xx (or network error -> "unexpected") from the
        # forward-looking probe must not regress a working cookie-based display.
        monkeypatch.setenv("OLLAMA_API_KEY", "ollama_key_1234567890abcd")
        monkeypatch.setenv("OLLAMA_SESSION_COOKIE", "ollama_session=abcdefghijklmnop")
        html = "<html><body><div>Session usage 7% used Resets in 2 hours</div></body></html>"
        sessions = iter([_mock_aiohttp_get(status=500), _mock_html_session(html)])
        with patch(
            "serving.admin.provider_quotas.aiohttp.ClientSession",
            side_effect=lambda *a, **k: next(sessions),
        ):
            result = (await fetch_ollama())[0]
        assert result.ok is True
        assert any("session" in u.label.lower() for u in result.usages)

    @pytest.mark.asyncio
    async def test_api_key_auth_failed_falls_back_to_cookie(self, monkeypatch):
        # A 401 from the usage probe should also defer to a configured cookie.
        monkeypatch.setenv("OLLAMA_API_KEY", "ollama_key_1234567890abcd")
        monkeypatch.setenv("OLLAMA_SESSION_COOKIE", "ollama_session=abcdefghijklmnop")
        html = "<html><body><div>Session usage 3% used Resets in 2 hours</div></body></html>"
        sessions = iter([_mock_aiohttp_get(status=401), _mock_html_session(html)])
        with patch(
            "serving.admin.provider_quotas.aiohttp.ClientSession",
            side_effect=lambda *a, **k: next(sessions),
        ):
            result = (await fetch_ollama())[0]
        assert result.ok is True
        assert any("session" in u.label.lower() for u in result.usages)

    @pytest.mark.asyncio
    async def test_managed_api_key_from_operational_store_is_probed(self, monkeypatch):
        # Keys surfaced by _discover_provider_keys (admin Provider Keys flow /
        # live KeyPool, minus disabled-env tombstones) must be probed, not just a
        # raw env OLLAMA_API_KEY.
        monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
        monkeypatch.delenv("OLLAMA_SESSION_COOKIE", raising=False)
        payload = {"session": {"percent": 9}}
        with (
            patch(
                "serving.admin.provider_quotas._discover_provider_keys",
                new=AsyncMock(return_value=[(1, "managed_ollama_key_xyz")]),
            ) as mock_discover,
            patch(
                "serving.admin.provider_quotas.aiohttp.ClientSession",
                return_value=_mock_aiohttp_get(status=200, json_data=payload),
            ) as mock_session_cls,
        ):
            result = (await fetch_ollama(operational_store=object()))[0]
        assert result.ok is True
        assert mock_discover.call_args.args[0] == "ollama"
        session = mock_session_cls.return_value.__aenter__.return_value
        assert session.get.call_args.kwargs["headers"]["Authorization"] == (
            "Bearer managed_ollama_key_xyz"
        )


class TestFetchFeatherless:
    @pytest.mark.asyncio
    async def test_returns_each_live_pool_key_with_working_probe_status(self, monkeypatch):
        monkeypatch.delenv("FEATHERLESS_API_KEY", raising=False)
        keys = [
            "rc_1111111111111111aaaa",
            "rc_2222222222222222bbbb",
            "rc_3333333333333333cccc",
            "rc_4444444444444444dddd",
            "rc_5555555555555555eeee",
        ]
        dynamic_keys.register_adapter_for_provider(
            "featherless",
            SimpleNamespace(_key_pool=KeyPool(keys[:3], "featherless")),
        )
        dynamic_keys.register_adapter_for_provider(
            "featherless",
            SimpleNamespace(_key_pool=KeyPool([keys[0], *keys[3:]], "featherless")),
        )

        probed: list[str] = []

        async def _probe(_services, *, provider, api_key, timeout_seconds):
            assert provider == "featherless"
            assert timeout_seconds == 8
            probed.append(api_key)

        monkeypatch.setattr(
            "serving.admin.provider_quotas.probe_provider_key_with_existing_route",
            _probe,
        )

        async def _no_concurrency_usage(_key):
            return None

        monkeypatch.setattr(
            "serving.admin.provider_quotas._fetch_featherless_concurrency_usage",
            _no_concurrency_usage,
        )

        results = await fetch_featherless(services=SimpleNamespace())

        assert len(results) == 5
        assert [r.key_index for r in results] == [1, 2, 3, 4, 5]
        assert [r.display_name for r in results] == [
            "Featherless #1",
            "Featherless #2",
            "Featherless #3",
            "Featherless #4",
            "Featherless #5",
        ]
        assert all(r.name == "featherless" for r in results)
        assert all(r.key_configured is True for r in results)
        assert all(r.ok is True for r in results)
        assert all(r.error is None for r in results)
        assert {r.key_masked for r in results} == {_mask_key(k) for k in keys}
        assert probed == keys

    @pytest.mark.asyncio
    async def test_returns_each_live_pool_key_with_failed_probe_status(self, monkeypatch):
        monkeypatch.delenv("FEATHERLESS_API_KEY", raising=False)
        keys = [
            "rc_1111111111111111aaaa",
            "rc_2222222222222222bbbb",
        ]
        dynamic_keys.register_adapter_for_provider(
            "featherless",
            SimpleNamespace(_key_pool=KeyPool(keys, "featherless")),
        )

        async def _probe(_services, *, provider, api_key, timeout_seconds):
            del provider, timeout_seconds
            if api_key.endswith("bbbb"):
                raise ProviderKeyProbeError("auth_failed", "bad key")

        monkeypatch.setattr(
            "serving.admin.provider_quotas.probe_provider_key_with_existing_route",
            _probe,
        )

        async def _no_concurrency_usage(_key):
            return None

        monkeypatch.setattr(
            "serving.admin.provider_quotas._fetch_featherless_concurrency_usage",
            _no_concurrency_usage,
        )

        results = await fetch_featherless(services=SimpleNamespace())

        assert [r.ok for r in results] == [True, False]
        assert [r.error for r in results] == [None, "auth_failed"]

    @pytest.mark.asyncio
    async def test_working_probe_includes_concurrency_usage(self, monkeypatch):
        monkeypatch.setenv("FEATHERLESS_API_KEY", "rc_1111111111111111aaaa")

        async def _probe(_services, *, provider, api_key, timeout_seconds):
            del _services, provider, api_key, timeout_seconds

        monkeypatch.setattr(
            "serving.admin.provider_quotas.probe_provider_key_with_existing_route",
            _probe,
        )
        with patch(
            "serving.admin.provider_quotas.aiohttp.ClientSession",
            return_value=_mock_aiohttp_get(
                status=200,
                json_data={"limit": 2, "used_cost": 0, "request_count": 0, "requests": []},
            ),
        ):
            results = await fetch_featherless(services=SimpleNamespace())

        assert len(results) == 1
        result = results[0]
        assert result.ok is True
        assert result.usages[0].label == "Concurrency"
        assert result.usages[0].used == 0.0
        assert result.usages[0].limit == 2.0
        assert result.usages[0].unit == "units"

    @pytest.mark.asyncio
    async def test_reuses_cached_result_within_ttl(self, monkeypatch):
        monkeypatch.setenv("FEATHERLESS_API_KEY", "rc_1111111111111111aaaa")
        probe_calls = 0

        async def _probe(_services, *, provider, api_key, timeout_seconds):
            nonlocal probe_calls
            del _services, provider, api_key, timeout_seconds
            probe_calls += 1

        monkeypatch.setattr(
            "serving.admin.provider_quotas.probe_provider_key_with_existing_route",
            _probe,
        )

        async def _no_concurrency_usage(_key):
            return None

        monkeypatch.setattr(
            "serving.admin.provider_quotas._fetch_featherless_concurrency_usage",
            _no_concurrency_usage,
        )

        first = await fetch_featherless(services=SimpleNamespace())
        second = await fetch_featherless(services=SimpleNamespace())

        assert probe_calls == 1
        assert first[0].ok is True
        assert second[0].ok is True
        assert first is not second
        assert first[0] is not second[0]

    @pytest.mark.asyncio
    async def test_concurrent_calls_share_single_in_flight_probe(self, monkeypatch):
        monkeypatch.setenv("FEATHERLESS_API_KEY", "rc_1111111111111111aaaa")
        started = asyncio.Event()
        release = asyncio.Event()
        probe_calls = 0

        async def _probe(_services, *, provider, api_key, timeout_seconds):
            nonlocal probe_calls
            del _services, provider, api_key, timeout_seconds
            probe_calls += 1
            started.set()
            await release.wait()

        monkeypatch.setattr(
            "serving.admin.provider_quotas.probe_provider_key_with_existing_route",
            _probe,
        )

        async def _no_concurrency_usage(_key):
            return None

        monkeypatch.setattr(
            "serving.admin.provider_quotas._fetch_featherless_concurrency_usage",
            _no_concurrency_usage,
        )

        first_task = asyncio.create_task(fetch_featherless(services=SimpleNamespace()))
        await started.wait()
        second_task = asyncio.create_task(fetch_featherless(services=SimpleNamespace()))
        release.set()
        first, second = await asyncio.gather(first_task, second_task)

        assert probe_calls == 1
        assert first[0].ok is True
        assert second[0].ok is True

    @pytest.mark.asyncio
    async def test_cancelled_probe_is_rethrown(self, monkeypatch):
        monkeypatch.setenv("FEATHERLESS_API_KEY", "rc_1111111111111111aaaa")

        async def _probe(_services, *, provider, api_key, timeout_seconds):
            del _services, provider, api_key, timeout_seconds
            raise asyncio.CancelledError

        monkeypatch.setattr(
            "serving.admin.provider_quotas.probe_provider_key_with_existing_route",
            _probe,
        )

        async def _no_concurrency_usage(_key):
            return None

        monkeypatch.setattr(
            "serving.admin.provider_quotas._fetch_featherless_concurrency_usage",
            _no_concurrency_usage,
        )

        with pytest.raises(asyncio.CancelledError):
            await fetch_featherless(services=SimpleNamespace())

    @pytest.mark.asyncio
    async def test_concurrency_usage_fetcher_uses_account_snapshot_endpoint(self):
        with patch(
            "serving.admin.provider_quotas.aiohttp.ClientSession",
            return_value=_mock_aiohttp_get(
                status=200,
                json_data={"limit": 4, "used_cost": 1, "request_count": 1, "requests": []},
            ),
        ) as session_factory:
            usage = await _fetch_featherless_concurrency_usage("rc_1111111111111111aaaa")

        assert usage is not None
        assert usage.label == "Concurrency"
        assert usage.used == 1.0
        assert usage.limit == 4.0
        assert usage.unit == "units"
        session = session_factory.return_value.__aenter__.return_value
        session.get.assert_called_once()
        assert session.get.call_args.args == ("https://api.featherless.ai/account/concurrency",)
        assert session.get.call_args.kwargs["allow_redirects"] is False

    @pytest.mark.asyncio
    async def test_returns_probe_unavailable_without_services(self, monkeypatch):
        monkeypatch.setenv("FEATHERLESS_API_KEY", "rc_1111111111111111aaaa")

        results = await fetch_featherless()

        assert len(results) == 1
        assert results[0].ok is False
        assert results[0].error == "probe_unavailable"


class TestGatherAll:
    @pytest.mark.asyncio
    async def test_gather_all_returns_five_results_when_unconfigured(self, monkeypatch):
        monkeypatch.delenv("CHUTES_API_KEY", raising=False)
        monkeypatch.delenv("ZAI_API_KEY", raising=False)
        monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
        monkeypatch.delenv("MINIMAX_SESSION_COOKIE", raising=False)
        monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
        monkeypatch.delenv("KIMI_CODING_API_KEY", raising=False)
        monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
        monkeypatch.delenv("OLLAMA_SESSION_COOKIE", raising=False)
        monkeypatch.delenv("FEATHERLESS_API_KEY", raising=False)

        results = await gather_all()
        assert len(results) == 6
        names = {r.name for r in results}
        assert names == {"chutes", "zai", "minimax", "kimi", "ollama", "featherless"}
        assert all(r.error == "not_configured" for r in results)

    @pytest.mark.asyncio
    async def test_gather_all_handles_unexpected_exception(self, monkeypatch):
        async def boom(_operational_store=None):
            raise RuntimeError("simulated failure")

        monkeypatch.setattr("serving.admin.provider_quotas.fetch_chutes", boom)
        monkeypatch.delenv("ZAI_API_KEY", raising=False)
        monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
        monkeypatch.delenv("MINIMAX_SESSION_COOKIE", raising=False)
        monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
        monkeypatch.delenv("KIMI_CODING_API_KEY", raising=False)
        monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
        monkeypatch.delenv("OLLAMA_SESSION_COOKIE", raising=False)
        monkeypatch.delenv("FEATHERLESS_API_KEY", raising=False)

        results = await gather_all()
        assert len(results) == 6
        chutes = next(r for r in results if r.name == "chutes")
        assert chutes.ok is False
        assert chutes.error == "unexpected"

    @pytest.mark.asyncio
    async def test_gather_all_includes_featherless_live_pool_keys(self, monkeypatch):
        monkeypatch.delenv("CHUTES_API_KEY", raising=False)
        monkeypatch.delenv("ZAI_API_KEY", raising=False)
        monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
        monkeypatch.delenv("MINIMAX_SESSION_COOKIE", raising=False)
        monkeypatch.delenv("KIMI_CODING_API_KEY", raising=False)
        monkeypatch.delenv("OLLAMA_SESSION_COOKIE", raising=False)
        monkeypatch.delenv("FEATHERLESS_API_KEY", raising=False)
        keys = [
            "rc_1111111111111111aaaa",
            "rc_2222222222222222bbbb",
            "rc_3333333333333333cccc",
            "rc_4444444444444444dddd",
            "rc_5555555555555555eeee",
        ]
        dynamic_keys.register_adapter_for_provider(
            "featherless",
            SimpleNamespace(_key_pool=KeyPool(keys, "featherless")),
        )

        async def _probe(_services, *, provider, api_key, timeout_seconds):
            del _services, provider, api_key, timeout_seconds

        monkeypatch.setattr(
            "serving.admin.provider_quotas.probe_provider_key_with_existing_route",
            _probe,
        )

        async def _no_concurrency_usage(_key):
            return None

        monkeypatch.setattr(
            "serving.admin.provider_quotas._fetch_featherless_concurrency_usage",
            _no_concurrency_usage,
        )

        results = await gather_all(services=SimpleNamespace())

        featherless = [r for r in results if r.name == "featherless"]
        assert len(featherless) == 5
        assert [r.key_index for r in featherless] == [1, 2, 3, 4, 5]
        assert all(r.ok is True for r in featherless)
        assert all(r.error is None for r in featherless)


class TestProviderQuotasRoute:
    @pytest.fixture
    def admin_app(self):
        """Build a minimal FastAPI app with the admin router mounted."""
        app = FastAPI(title="Admin Provider Quotas Test")
        op_store = MagicMock(name="operational_store")
        op_store.list_provider_keys_full = AsyncMock(return_value=[])
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
    async def test_route_returns_aggregated_response(self, admin_app, monkeypatch):
        async def _fake_admin() -> str:
            return "admin@test"

        admin_app.dependency_overrides[verify_admin_access] = _fake_admin

        monkeypatch.delenv("CHUTES_API_KEY", raising=False)
        monkeypatch.delenv("ZAI_API_KEY", raising=False)
        monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
        monkeypatch.delenv("MINIMAX_SESSION_COOKIE", raising=False)
        monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
        monkeypatch.delenv("KIMI_CODING_API_KEY", raising=False)
        monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
        monkeypatch.delenv("OLLAMA_SESSION_COOKIE", raising=False)
        monkeypatch.delenv("FEATHERLESS_API_KEY", raising=False)

        transport = ASGITransport(app=admin_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/admin/provider-quotas")
        admin_app.dependency_overrides.clear()
        assert resp.status_code == 200
        body = resp.json()
        assert "generated_at" in body
        assert len(body["providers"]) == 6
        assert {p["name"] for p in body["providers"]} == {
            "chutes",
            "zai",
            "minimax",
            "kimi",
            "ollama",
            "featherless",
        }


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


def _clear_provider_env(monkeypatch):
    for var in (
        "CHUTES_API_KEY",
        "ZAI_API_KEY",
        "MINIMAX_API_KEY",
        "MINIMAX_SESSION_COOKIE",
        "KIMI_CODING_API_KEY",
        "OLLAMA_API_KEY",
        "OLLAMA_SESSION_COOKIE",
        "FEATHERLESS_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)


class TestPerKeyRef:
    def test_key_ref_matches_env_key_id_hash(self):
        raw = "sk-example-key-0123456789"
        assert key_ref(raw) == dynamic_keys.env_key_hash(raw)[:32]

    @pytest.mark.asyncio
    async def test_api_key_results_carry_key_ref(self, monkeypatch):
        _clear_provider_env(monkeypatch)
        keys = ["rc_1111111111111111aaaa", "rc_2222222222222222bbbb"]
        dynamic_keys.register_adapter_for_provider(
            "featherless",
            SimpleNamespace(_key_pool=KeyPool(keys, "featherless")),
        )

        async def _probe(_services, *, provider, api_key, timeout_seconds):
            del _services, provider, api_key, timeout_seconds

        monkeypatch.setattr(
            "serving.admin.provider_quotas.probe_provider_key_with_existing_route",
            _probe,
        )

        async def _no_concurrency_usage(_key):
            return None

        monkeypatch.setattr(
            "serving.admin.provider_quotas._fetch_featherless_concurrency_usage",
            _no_concurrency_usage,
        )

        results = await fetch_featherless(services=SimpleNamespace())

        assert [r.key_ref for r in results] == [key_ref(k) for k in keys]
        assert all(r.key_disabled is False for r in results)

    @pytest.mark.asyncio
    async def test_cookie_results_have_no_key_ref(self, monkeypatch):
        """Session cookies are not managed by the provider-key endpoints."""
        _clear_provider_env(monkeypatch)
        monkeypatch.setattr("serving.admin.provider_quotas.settings", SimpleNamespace())
        monkeypatch.setenv("MINIMAX_SESSION_COOKIE", "session=abc123def456ghi789jkl")

        async def _boom(*args, **kwargs):
            raise RuntimeError("no network in tests")

        monkeypatch.setattr(
            "serving.admin.provider_quotas._fetch_minimax_for_key",
            _boom,
        )

        results = await fetch_minimax()

        assert results
        assert all(r.key_ref is None for r in results)


class TestDisabledKeyCards:
    @pytest.mark.asyncio
    async def test_gather_all_surfaces_disabled_env_and_db_keys(self, monkeypatch):
        _clear_provider_env(monkeypatch)
        env_raw = "zai-env-disabled-key-000000"
        db_raw = "zai-db-disabled-key-111111"
        store = _QuotaKeyStore(
            disabled_env={
                "zai": [(dynamic_keys.env_key_hash(env_raw), "zai-env-...0000")],
            },
            db_rows={
                "zai": [
                    SimpleNamespace(id="key-1", key_prefix="zai-db-1...1111", status="disabled"),
                    SimpleNamespace(id="key-2", key_prefix="zai-db-2...2222", status="active"),
                ],
            },
            db_raw={"key-1": ("zai", db_raw)},
        )

        results = await gather_all(store)

        disabled = [r for r in results if r.key_disabled]
        assert {r.key_ref for r in disabled} == {key_ref(env_raw), key_ref(db_raw)}
        assert all(r.name == "zai" for r in disabled)
        assert all(r.ok is False and r.error == "key_disabled" for r in disabled)
        # The active DB row is not duplicated as a disabled card.
        assert len(disabled) == 2

    @pytest.mark.asyncio
    async def test_disabled_card_is_dropped_when_the_key_is_still_live(self, monkeypatch):
        """One credential yields one card, even when two sources record it.

        A raw value present in a live pool and in a disabled DB row used to
        produce both an active and a "Key disabled" card for the same key.
        """
        _clear_provider_env(monkeypatch)
        shared = "featherless-shared-key-000000"
        store = _QuotaKeyStore(
            db_rows={
                "featherless": [
                    SimpleNamespace(
                        id="key-1",
                        key_prefix="feathe...0000",
                        status="disabled",
                    ),
                ],
            },
            db_raw={"key-1": ("featherless", shared)},
        )
        dynamic_keys.register_adapter_for_provider(
            "featherless",
            SimpleNamespace(_key_pool=KeyPool([shared], "featherless")),
        )

        async def _probe(_services, *, provider, api_key, timeout_seconds):
            del _services, provider, api_key, timeout_seconds

        monkeypatch.setattr(
            "serving.admin.provider_quotas.probe_provider_key_with_existing_route",
            _probe,
        )

        async def _no_concurrency_usage(_key):
            return None

        monkeypatch.setattr(
            "serving.admin.provider_quotas._fetch_featherless_concurrency_usage",
            _no_concurrency_usage,
        )

        results = await gather_all(store, services=SimpleNamespace())

        cards = [r for r in results if r.key_ref == key_ref(shared)]
        assert len(cards) == 1
        assert cards[0].key_disabled is False

    @pytest.mark.asyncio
    async def test_gather_all_tolerates_store_failures(self, monkeypatch):
        _clear_provider_env(monkeypatch)

        class _BrokenStore(_QuotaKeyStore):
            async def list_disabled_provider_env_keys(self, provider: str):
                raise RuntimeError("boom")

            async def list_provider_keys(self, provider: str | None = None):
                raise RuntimeError("boom")

        results = await gather_all(_BrokenStore())

        assert len(results) == 6
        assert not any(r.key_disabled for r in results)
