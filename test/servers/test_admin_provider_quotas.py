"""Tests for the admin provider-quotas module."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from serving.admin.provider_quotas import _mask_key, fetch_chutes


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


def _mock_aiohttp_get(*, status: int = 200, json_data: dict | None = None, raise_exc: Exception | None = None):
    """Build a context-manager mock for `aiohttp.ClientSession().get(...)`."""
    response = MagicMock()
    response.status = status
    response.json = AsyncMock(return_value=json_data or {})
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


class TestFetchChutes:
    @pytest.mark.asyncio
    async def test_not_configured_when_key_missing(self, monkeypatch):
        monkeypatch.delenv("CHUTES_API_KEY", raising=False)
        result = await fetch_chutes()
        assert result.ok is False
        assert result.error == "not_configured"
        assert result.key_configured is False
        assert result.name == "chutes"
        assert result.display_name == "Chutes"

    @pytest.mark.asyncio
    async def test_success_returns_usages(self, monkeypatch):
        monkeypatch.setenv("CHUTES_API_KEY", "cpk_abcdef1234567890xyz")
        payload = {
            "monthly": {"used": 4.20, "limit": 100.0, "reset_at": "2026-05-01T00:00:00Z"},
            "rolling": {"used": 1.10, "limit": 10.0, "window": "4h"},
        }
        with patch("serving.admin.provider_quotas.aiohttp.ClientSession", return_value=_mock_aiohttp_get(status=200, json_data=payload)):
            result = await fetch_chutes()
        assert result.ok is True
        assert result.key_configured is True
        assert result.key_masked == "cpk_abcd...3xyz" or result.key_masked.startswith("cpk_abcd")
        assert any(u.label.lower().startswith("month") for u in result.usages)
        assert any("4" in u.label or "rolling" in u.label.lower() for u in result.usages)

    @pytest.mark.asyncio
    async def test_auth_failed_on_401(self, monkeypatch):
        monkeypatch.setenv("CHUTES_API_KEY", "cpk_abcdef1234567890xyz")
        with patch("serving.admin.provider_quotas.aiohttp.ClientSession", return_value=_mock_aiohttp_get(status=401)):
            result = await fetch_chutes()
        assert result.ok is False
        assert result.error == "auth_failed"

    @pytest.mark.asyncio
    async def test_timeout_returns_timeout_error(self, monkeypatch):
        import asyncio
        monkeypatch.setenv("CHUTES_API_KEY", "cpk_abcdef1234567890xyz")
        with patch("serving.admin.provider_quotas.aiohttp.ClientSession", return_value=_mock_aiohttp_get(raise_exc=asyncio.TimeoutError())):
            result = await fetch_chutes()
        assert result.ok is False
        assert result.error == "timeout"
