"""Tests for ClaudeCredentialProvider and ClaudeAccountCredential."""

from __future__ import annotations

import json
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from serving.adapters.claude_token import (
    ClaudeAccountCredential,
    ClaudeCredentialProvider,
    RefreshTokenRevokedError,
    RefreshTokenTransientError,
    TokenRefreshError,
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


def _write_accounts_file(path, accounts: list[dict], *, version: int | None = None):
    data = {"accounts": accounts}
    if version is not None:
        data["version"] = version
    path.write_text(json.dumps(data))


def _account_to_dict_v2(acct: ClaudeAccountCredential) -> dict:
    return {
        "id": acct.id,
        "label": acct.label,
        "access_token": acct.access_token,
        "refresh_token": acct.refresh_token,
        "expires_at": acct.expires_at,
        "organization_id": acct.organization_id,
        "email": acct.email,
        "plan": acct.plan,
        "state": acct.state,
        "state_changed_at": acct.state_changed_at,
        "revoke_reason": acct.revoke_reason,
        "consecutive_failures": acct.consecutive_failures,
    }


def _account_to_dict_v1(acct: ClaudeAccountCredential, enabled: bool = True) -> dict:
    """Build a v1-format dict (uses ``enabled`` field, no ``state``)."""
    return {
        "id": acct.id,
        "label": acct.label,
        "access_token": acct.access_token,
        "refresh_token": acct.refresh_token,
        "expires_at": acct.expires_at,
        "organization_id": acct.organization_id,
        "email": acct.email,
        "plan": acct.plan,
        "enabled": enabled,
    }


def _mock_refresh_response(status=200, body=None, json_data=None):
    """Create a mock aiohttp response for token refresh."""
    mock_response = MagicMock()
    mock_response.status = status
    if json_data is not None:
        mock_response.json = AsyncMock(return_value=json_data)
    mock_response.text = AsyncMock(return_value=body or "")
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=mock_response)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    return mock_session


# ---------------------------------------------------------------------------
# Load accounts
# ---------------------------------------------------------------------------


class TestLoadAccounts:
    def test_loads_active_accounts(self, tmp_path):
        f = tmp_path / "accounts.json"
        _write_accounts_file(
            f,
            [
                _account_to_dict_v2(_make_credential("a")),
                _account_to_dict_v2(_make_credential("b")),
            ],
            version=2,
        )

        provider = ClaudeCredentialProvider(str(f))
        accounts = provider.load_accounts()

        assert len(accounts) == 2
        assert accounts[0].id == "a"
        assert accounts[1].id == "b"
        assert accounts[0].plan == "max"

    def test_filters_revoked_accounts(self, tmp_path):
        f = tmp_path / "accounts.json"
        active = _account_to_dict_v2(_make_credential("a"))
        revoked = _account_to_dict_v2(_make_credential("b", state="revoked", revoke_reason="invalid_grant"))
        _write_accounts_file(f, [active, revoked], version=2)

        provider = ClaudeCredentialProvider(str(f))
        accounts = provider.load_accounts()

        assert len(accounts) == 1
        assert accounts[0].id == "a"
        # Revoked account should still be in internal registry
        assert "b" in provider._accounts
        assert provider._accounts["b"].state == "revoked"

    def test_filters_disabled_accounts(self, tmp_path):
        f = tmp_path / "accounts.json"
        active = _account_to_dict_v2(_make_credential("a"))
        disabled = _account_to_dict_v2(_make_credential("b", state="disabled"))
        _write_accounts_file(f, [active, disabled], version=2)

        provider = ClaudeCredentialProvider(str(f))
        accounts = provider.load_accounts()

        assert len(accounts) == 1
        assert accounts[0].id == "a"

    def test_promotes_cooldown_to_active(self, tmp_path):
        f = tmp_path / "accounts.json"
        cooldown = _account_to_dict_v2(_make_credential("a", state="cooldown"))
        _write_accounts_file(f, [cooldown], version=2)

        provider = ClaudeCredentialProvider(str(f))
        accounts = provider.load_accounts()

        assert len(accounts) == 1
        assert accounts[0].id == "a"
        assert accounts[0].state == "active"

    def test_default_plan_is_pro(self, tmp_path):
        f = tmp_path / "accounts.json"
        entry = _account_to_dict_v2(_make_credential("a"))
        del entry["plan"]
        _write_accounts_file(f, [entry], version=2)

        provider = ClaudeCredentialProvider(str(f))
        accounts = provider.load_accounts()

        assert accounts[0].plan == "pro"

    def test_skips_entry_missing_required_fields(self, tmp_path):
        f = tmp_path / "accounts.json"
        good = _account_to_dict_v2(_make_credential("a"))
        bad = {"id": "b", "label": "bad"}  # missing access_token, refresh_token, expires_at
        _write_accounts_file(f, [good, bad], version=2)

        provider = ClaudeCredentialProvider(str(f))
        accounts = provider.load_accounts()

        assert len(accounts) == 1
        assert accounts[0].id == "a"


class TestLoadAccountsMigration:
    """Test v1 → v2 migration (``enabled`` → ``state``)."""

    def test_v1_enabled_true_becomes_active(self, tmp_path):
        f = tmp_path / "accounts.json"
        entry = _account_to_dict_v1(_make_credential("a"), enabled=True)
        _write_accounts_file(f, [entry])  # No version → v1

        provider = ClaudeCredentialProvider(str(f))
        accounts = provider.load_accounts()

        assert len(accounts) == 1
        assert accounts[0].state == "active"

    def test_v1_enabled_false_becomes_disabled(self, tmp_path):
        f = tmp_path / "accounts.json"
        entry = _account_to_dict_v1(_make_credential("a"), enabled=False)
        _write_accounts_file(f, [entry])  # No version → v1

        provider = ClaudeCredentialProvider(str(f))
        accounts = provider.load_accounts()

        assert len(accounts) == 0
        assert provider._accounts["a"].state == "disabled"


# ---------------------------------------------------------------------------
# Token refresh error handling
# ---------------------------------------------------------------------------


class TestRefreshTokenErrors:
    @pytest.mark.asyncio
    async def test_invalid_grant_raises_revoked_error(self, tmp_path):
        f = tmp_path / "accounts.json"
        acct = _make_credential("a", expires_at=int(time.time() * 1000) - 1000)
        _write_accounts_file(f, [_account_to_dict_v2(acct)], version=2)

        provider = ClaudeCredentialProvider(str(f))
        provider.load_accounts()

        error_body = json.dumps({"error": "invalid_grant", "error_description": "token revoked"})
        mock_session = _mock_refresh_response(status=400, body=error_body)

        with patch("serving.adapters.claude_token.aiohttp.ClientSession", return_value=mock_session):
            with pytest.raises(RefreshTokenRevokedError) as exc_info:
                await provider._refresh_token(acct)

        assert exc_info.value.account_id == "a"
        assert "invalid_grant" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_other_error_raises_transient_error(self, tmp_path):
        f = tmp_path / "accounts.json"
        acct = _make_credential("a", expires_at=int(time.time() * 1000) - 1000)
        _write_accounts_file(f, [_account_to_dict_v2(acct)], version=2)

        provider = ClaudeCredentialProvider(str(f))
        provider.load_accounts()

        error_body = json.dumps({"error": "server_error", "error_description": "try again"})
        mock_session = _mock_refresh_response(status=500, body=error_body)

        with patch("serving.adapters.claude_token.aiohttp.ClientSession", return_value=mock_session):
            with pytest.raises(RefreshTokenTransientError) as exc_info:
                await provider._refresh_token(acct)

        assert exc_info.value.account_id == "a"
        assert exc_info.value.status == 500

    @pytest.mark.asyncio
    async def test_non_json_error_body_raises_transient(self, tmp_path):
        f = tmp_path / "accounts.json"
        acct = _make_credential("a", expires_at=int(time.time() * 1000) - 1000)
        _write_accounts_file(f, [_account_to_dict_v2(acct)], version=2)

        provider = ClaudeCredentialProvider(str(f))
        provider.load_accounts()

        mock_session = _mock_refresh_response(status=502, body="Bad Gateway")

        with patch("serving.adapters.claude_token.aiohttp.ClientSession", return_value=mock_session):
            with pytest.raises(RefreshTokenTransientError):
                await provider._refresh_token(acct)

    def test_error_hierarchy(self):
        assert issubclass(RefreshTokenRevokedError, TokenRefreshError)
        assert issubclass(RefreshTokenTransientError, TokenRefreshError)
        assert issubclass(TokenRefreshError, Exception)


# ---------------------------------------------------------------------------
# Token refresh (success path)
# ---------------------------------------------------------------------------


class TestGetValidToken:
    @pytest.mark.asyncio
    async def test_returns_cached_token_when_valid(self, tmp_path):
        f = tmp_path / "accounts.json"
        _write_accounts_file(f, [_account_to_dict_v2(_make_credential("a"))], version=2)

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
        _write_accounts_file(f, [_account_to_dict_v2(acct)], version=2)

        provider = ClaudeCredentialProvider(str(f), refresh_margin=30)
        provider.load_accounts()

        new_token = "sk-ant-oat-refreshed"
        mock_session = _mock_refresh_response(
            json_data={
                "access_token": new_token,
                "refresh_token": "new_refresh",
                "expires_in": 3600,
            }
        )

        with patch("serving.adapters.claude_token.aiohttp.ClientSession", return_value=mock_session):
            token = await provider.get_valid_token(acct)

        assert token == new_token
        assert acct.access_token == new_token

    @pytest.mark.asyncio
    async def test_force_refresh_bypasses_expiry_check(self, tmp_path):
        f = tmp_path / "accounts.json"
        acct = _make_credential("a")  # Token has 1 hour left
        _write_accounts_file(f, [_account_to_dict_v2(acct)], version=2)

        provider = ClaudeCredentialProvider(str(f), refresh_margin=30)
        provider.load_accounts()

        new_token = "sk-ant-oat-forced"
        mock_session = _mock_refresh_response(
            json_data={
                "access_token": new_token,
                "refresh_token": "new_refresh",
                "expires_in": 3600,
            }
        )

        with patch("serving.adapters.claude_token.aiohttp.ClientSession", return_value=mock_session):
            token = await provider.get_valid_token(acct, force_refresh=True)

        assert token == new_token


# ---------------------------------------------------------------------------
# State transitions
# ---------------------------------------------------------------------------


class TestStateTransition:
    @pytest.mark.asyncio
    async def test_transition_to_revoked(self, tmp_path):
        f = tmp_path / "accounts.json"
        acct = _make_credential("a")
        _write_accounts_file(f, [_account_to_dict_v2(acct)], version=2)

        provider = ClaudeCredentialProvider(str(f))
        provider.load_accounts()

        mock_pool = MagicMock()
        await provider.transition_state(acct, "revoked", "invalid_grant", pool=mock_pool)

        assert acct.state == "revoked"
        assert acct.revoke_reason == "invalid_grant"
        assert acct.state_changed_at > 0
        mock_pool.deactivate.assert_called_once_with("a")

        # Verify persisted
        data = json.loads(f.read_text())
        entry = data["accounts"][0]
        assert entry["state"] == "revoked"
        assert entry["revoke_reason"] == "invalid_grant"

    @pytest.mark.asyncio
    async def test_transition_to_active_clears_failures(self, tmp_path):
        f = tmp_path / "accounts.json"
        acct = _make_credential("a", state="revoked", consecutive_failures=5, revoke_reason="test")
        _write_accounts_file(f, [_account_to_dict_v2(acct)], version=2)

        provider = ClaudeCredentialProvider(str(f))
        provider.load_accounts()

        mock_pool = MagicMock()
        await provider.transition_state(acct, "active", pool=mock_pool)

        assert acct.state == "active"
        assert acct.consecutive_failures == 0
        assert acct.revoke_reason == ""
        mock_pool.activate.assert_called_once_with(acct)

    @pytest.mark.asyncio
    async def test_cooldown_does_not_deactivate_pool(self, tmp_path):
        """Cooldown is transient — account stays in pool for runtime recovery."""
        f = tmp_path / "accounts.json"
        acct = _make_credential("a")
        _write_accounts_file(f, [_account_to_dict_v2(acct)], version=2)

        provider = ClaudeCredentialProvider(str(f))
        provider.load_accounts()

        mock_pool = MagicMock()
        await provider.transition_state(acct, "cooldown", "rate_limited", pool=mock_pool)

        assert acct.state == "cooldown"
        mock_pool.deactivate.assert_not_called()
        mock_pool.activate.assert_not_called()

    @pytest.mark.asyncio
    async def test_transition_without_pool(self, tmp_path):
        f = tmp_path / "accounts.json"
        acct = _make_credential("a")
        _write_accounts_file(f, [_account_to_dict_v2(acct)], version=2)

        provider = ClaudeCredentialProvider(str(f))
        provider.load_accounts()

        await provider.transition_state(acct, "disabled", "manual")

        assert acct.state == "disabled"
        assert acct.revoke_reason == "manual"


# ---------------------------------------------------------------------------
# Persist
# ---------------------------------------------------------------------------


class TestPersist:
    @pytest.mark.asyncio
    async def test_atomic_write_v2(self, tmp_path):
        f = tmp_path / "accounts.json"
        acct = _make_credential("a")
        _write_accounts_file(f, [_account_to_dict_v2(acct)], version=2)

        provider = ClaudeCredentialProvider(str(f))
        provider.load_accounts()

        await provider._persist()

        data = json.loads(f.read_text())
        assert data["version"] == 2
        assert len(data["accounts"]) == 1
        assert data["accounts"][0]["id"] == "a"
        assert data["accounts"][0]["state"] == "active"
        assert "enabled" not in data["accounts"][0]

    @pytest.mark.asyncio
    async def test_backup_created(self, tmp_path):
        f = tmp_path / "accounts.json"
        acct = _make_credential("a")
        _write_accounts_file(f, [_account_to_dict_v2(acct)], version=2)

        provider = ClaudeCredentialProvider(str(f))
        provider.load_accounts()

        # Trigger persist
        await provider._persist()

        backup = tmp_path / "accounts.json.bak"
        assert backup.exists()
        backup_data = json.loads(backup.read_text())
        assert len(backup_data["accounts"]) == 1

    @pytest.mark.asyncio
    async def test_persist_includes_all_accounts(self, tmp_path):
        """Persist should include revoked/disabled accounts, not just active."""
        f = tmp_path / "accounts.json"
        active = _account_to_dict_v2(_make_credential("a"))
        revoked = _account_to_dict_v2(_make_credential("b", state="revoked"))
        _write_accounts_file(f, [active, revoked], version=2)

        provider = ClaudeCredentialProvider(str(f))
        provider.load_accounts()

        await provider._persist()

        data = json.loads(f.read_text())
        assert len(data["accounts"]) == 2
        ids = {a["id"] for a in data["accounts"]}
        assert ids == {"a", "b"}
