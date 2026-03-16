"""Tests for ClaudeCredentialProvider and ClaudeAccountCredential."""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from serving.adapters.claude_token import (
    ClaudeAccountCredential,
    ClaudeCredentialProvider,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_credential(id: str = "acct_01", **overrides) -> ClaudeAccountCredential:
    defaults = {
        "id": id,
        "label": f"test-{id}",
        "access_token": f"sk-ant-oat-{id}",
        "refresh_token": f"refresh_{id}",
        "expires_at": int(time.time() * 1000) + 3600_000,
        "organization_id": f"org-uuid-{id}",
        "email": f"{id}@test.com",
        "plan": "max",
    }
    defaults.update(overrides)
    return ClaudeAccountCredential(**defaults)


def _write_accounts_file(path, accounts: list[dict]):
    data = {"accounts": accounts}
    path.write_text(json.dumps(data))


def _account_to_dict(acct: ClaudeAccountCredential) -> dict:
    return {
        "id": acct.id,
        "label": acct.label,
        "access_token": acct.access_token,
        "refresh_token": acct.refresh_token,
        "expires_at": acct.expires_at,
        "organization_id": acct.organization_id,
        "email": acct.email,
        "plan": acct.plan,
        "enabled": acct.enabled,
    }


# ---------------------------------------------------------------------------
# Load accounts
# ---------------------------------------------------------------------------


class TestLoadAccounts:
    def test_loads_enabled_accounts(self, tmp_path):
        f = tmp_path / "accounts.json"
        _write_accounts_file(
            f,
            [
                _account_to_dict(_make_credential("a")),
                _account_to_dict(_make_credential("b")),
            ],
        )

        provider = ClaudeCredentialProvider(str(f))
        accounts = provider.load_accounts()

        assert len(accounts) == 2
        assert accounts[0].id == "a"
        assert accounts[1].id == "b"
        assert accounts[0].plan == "max"

    def test_skips_disabled_accounts(self, tmp_path):
        f = tmp_path / "accounts.json"
        enabled = _account_to_dict(_make_credential("a"))
        disabled = _account_to_dict(_make_credential("b"))
        disabled["enabled"] = False
        _write_accounts_file(f, [enabled, disabled])

        provider = ClaudeCredentialProvider(str(f))
        accounts = provider.load_accounts()

        assert len(accounts) == 1
        assert accounts[0].id == "a"

    def test_default_plan_is_pro(self, tmp_path):
        f = tmp_path / "accounts.json"
        entry = _account_to_dict(_make_credential("a"))
        del entry["plan"]
        _write_accounts_file(f, [entry])

        provider = ClaudeCredentialProvider(str(f))
        accounts = provider.load_accounts()

        assert accounts[0].plan == "pro"


# ---------------------------------------------------------------------------
# Token refresh
# ---------------------------------------------------------------------------


class TestGetValidToken:
    @pytest.mark.asyncio
    async def test_returns_cached_token_when_valid(self, tmp_path):
        f = tmp_path / "accounts.json"
        _write_accounts_file(f, [_account_to_dict(_make_credential("a"))])

        provider = ClaudeCredentialProvider(str(f), refresh_margin=30)
        provider.load_accounts()

        acct = _make_credential("a")
        token = await provider.get_valid_token(acct)
        assert token == acct.access_token

    @pytest.mark.asyncio
    async def test_refreshes_near_expiry(self, tmp_path):
        f = tmp_path / "accounts.json"
        acct = _make_credential(
            "a", expires_at=int(time.time() * 1000) + 5_000  # 5 seconds left
        )
        _write_accounts_file(f, [_account_to_dict(acct)])

        provider = ClaudeCredentialProvider(str(f), refresh_margin=30)
        provider.load_accounts()

        # Mock the refresh
        new_token = "sk-ant-oat-refreshed"
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(
            return_value={
                "access_token": new_token,
                "refresh_token": "new_refresh",
                "expires_in": 3600,
            }
        )
        mock_response.text = AsyncMock(return_value="")
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("serving.adapters.claude_token.aiohttp.ClientSession", return_value=mock_session):
            token = await provider.get_valid_token(acct)

        assert token == new_token
        assert acct.access_token == new_token

    @pytest.mark.asyncio
    async def test_force_refresh_bypasses_expiry_check(self, tmp_path):
        f = tmp_path / "accounts.json"
        acct = _make_credential("a")  # Token has 1 hour left
        _write_accounts_file(f, [_account_to_dict(acct)])

        provider = ClaudeCredentialProvider(str(f), refresh_margin=30)
        provider.load_accounts()

        new_token = "sk-ant-oat-forced"
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(
            return_value={
                "access_token": new_token,
                "refresh_token": "new_refresh",
                "expires_in": 3600,
            }
        )
        mock_response.text = AsyncMock(return_value="")
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("serving.adapters.claude_token.aiohttp.ClientSession", return_value=mock_session):
            token = await provider.get_valid_token(acct, force_refresh=True)

        assert token == new_token


# ---------------------------------------------------------------------------
# Persist
# ---------------------------------------------------------------------------


class TestPersist:
    @pytest.mark.asyncio
    async def test_atomic_write(self, tmp_path):
        f = tmp_path / "accounts.json"
        acct = _make_credential("a")
        _write_accounts_file(f, [_account_to_dict(acct)])

        provider = ClaudeCredentialProvider(str(f))
        provider.load_accounts()

        # Trigger persist
        await provider._persist()

        # Verify file was written correctly
        data = json.loads(f.read_text())
        assert len(data["accounts"]) == 1
        assert data["accounts"][0]["id"] == "a"
        assert data["accounts"][0]["organization_id"] == acct.organization_id
        assert data["accounts"][0]["type"] == "oauth"
