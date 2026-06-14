"""Tests for the admin provider-quotas module."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.admin.provider_quotas import (
    _discover_env_keys,
    _mask_key,
    _next_reset,
    _parse_iso,
    fetch_chutes,
    fetch_minimax,
    fetch_ollama,
    fetch_zai,
    gather_all,
)
from serving.servers.deps import AppServices, verify_admin_access
from serving.servers.routers import admin as admin_router


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


class TestFetchOllama:
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


class TestGatherAll:
    @pytest.mark.asyncio
    async def test_gather_all_returns_five_results_when_unconfigured(self, monkeypatch):
        monkeypatch.delenv("CHUTES_API_KEY", raising=False)
        monkeypatch.delenv("ZAI_API_KEY", raising=False)
        monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
        monkeypatch.delenv("MINIMAX_SESSION_COOKIE", raising=False)
        monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
        monkeypatch.delenv("OLLAMA_SESSION_COOKIE", raising=False)
        monkeypatch.delenv("FEATHERLESS_API_KEY", raising=False)

        results = await gather_all()
        assert len(results) == 5
        names = {r.name for r in results}
        assert names == {"chutes", "zai", "minimax", "ollama", "featherless"}
        assert all(r.error == "not_configured" for r in results)

    @pytest.mark.asyncio
    async def test_gather_all_handles_unexpected_exception(self, monkeypatch):
        async def boom():
            raise RuntimeError("simulated failure")

        monkeypatch.setattr("serving.admin.provider_quotas.fetch_chutes", boom)
        monkeypatch.delenv("ZAI_API_KEY", raising=False)
        monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
        monkeypatch.delenv("MINIMAX_SESSION_COOKIE", raising=False)
        monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
        monkeypatch.delenv("OLLAMA_SESSION_COOKIE", raising=False)
        monkeypatch.delenv("FEATHERLESS_API_KEY", raising=False)

        results = await gather_all()
        assert len(results) == 5
        chutes = next(r for r in results if r.name == "chutes")
        assert chutes.ok is False
        assert chutes.error == "unexpected"


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
        monkeypatch.delenv("OLLAMA_SESSION_COOKIE", raising=False)
        monkeypatch.delenv("FEATHERLESS_API_KEY", raising=False)

        transport = ASGITransport(app=admin_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/admin/provider-quotas")
        admin_app.dependency_overrides.clear()
        assert resp.status_code == 200
        body = resp.json()
        assert "generated_at" in body
        assert len(body["providers"]) == 5
        assert {p["name"] for p in body["providers"]} == {
            "chutes",
            "zai",
            "minimax",
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
