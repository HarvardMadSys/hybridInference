"""Tests for Codex credential provider and account pool."""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import patch

import pytest

from serving.adapters.codex_token import (
    AccountCredential,
    AccountPool,
    CredentialProvider,
    NoHealthyAccountError,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_account(id: str = "acct_01", **overrides) -> AccountCredential:
    defaults = {
        "id": id,
        "label": f"test-{id}",
        "access_token": f"token_{id}",
        "refresh_token": f"refresh_{id}",
        "expires_at": int(time.time() * 1000) + 3600_000,  # 1 hour ahead
        "account_id": f"org_{id}",
        "tier": "plus",
        "enabled": True,
    }
    defaults.update(overrides)
    return AccountCredential(**defaults)


def _make_pool(*account_ids: str, **kwargs) -> AccountPool:
    accounts = [_make_account(id=aid) for aid in account_ids]
    return AccountPool(accounts, **kwargs)


# ---------------------------------------------------------------------------
# AccountPool
# ---------------------------------------------------------------------------


class TestAccountPool:
    @pytest.mark.asyncio
    async def test_round_robin(self):
        pool = _make_pool("a", "b", "c")
        ids = []
        for _ in range(6):
            acct = await pool.acquire()
            ids.append(acct.id)
        assert ids == ["a", "b", "c", "a", "b", "c"]

    @pytest.mark.asyncio
    async def test_skip_unhealthy(self):
        pool = _make_pool("a", "b", "c")
        # Mark "b" as unhealthy via 429
        pool.report_failure("b", 429)

        ids = []
        for _ in range(4):
            acct = await pool.acquire()
            ids.append(acct.id)
        assert "b" not in ids
        assert ids == ["a", "c", "a", "c"]

    @pytest.mark.asyncio
    async def test_all_unhealthy_raises(self):
        pool = _make_pool("a", "b")
        pool.report_failure("a", 429)
        pool.report_failure("b", 429)

        with pytest.raises(NoHealthyAccountError):
            await pool.acquire()

    @pytest.mark.asyncio
    async def test_cooldown_recovery(self):
        pool = _make_pool("a", "b", cooldown=1)
        # Mark "a" unhealthy with threshold failures
        for _ in range(3):
            pool.report_failure("a", 401)

        # "a" should be skipped
        acct = await pool.acquire()
        assert acct.id == "b"

        # Wait for cooldown
        pool._health["a"].cooldown_until = time.time() - 1

        # Now "a" should be recovered
        acct = await pool.acquire()
        assert acct.id == "a"

    def test_report_success_resets(self):
        pool = _make_pool("a")
        pool.report_failure("a", 401)
        pool.report_failure("a", 401)
        assert pool._health["a"].consecutive_failures == 2

        pool.report_success("a")
        assert pool._health["a"].consecutive_failures == 0
        assert pool._health["a"].healthy is True


class TestDeactivateActivate:
    @pytest.mark.asyncio
    async def test_deactivate_removes_account(self):
        pool = _make_pool("a", "b", "c")
        pool.deactivate("b")

        ids = []
        for _ in range(4):
            acct = await pool.acquire()
            ids.append(acct.id)
        assert "b" not in ids
        assert ids == ["a", "c", "a", "c"]

    @pytest.mark.asyncio
    async def test_deactivate_clears_health(self):
        pool = _make_pool("a", "b")
        pool.report_failure("a", 401)
        assert "a" in pool._health

        pool.deactivate("a")
        assert "a" not in pool._health

    @pytest.mark.asyncio
    async def test_deactivate_adjusts_index(self):
        pool = _make_pool("a", "b")
        # Advance index past end after deactivation
        pool._index = 1
        pool.deactivate("b")
        assert pool._index == 0

    def test_deactivate_nonexistent_is_noop(self):
        pool = _make_pool("a", "b")
        pool.deactivate("z")
        assert len(pool._accounts) == 2

    @pytest.mark.asyncio
    async def test_activate_adds_account(self):
        pool = _make_pool("a")
        new_acct = _make_account("b")
        pool.activate(new_acct)

        ids = []
        for _ in range(4):
            acct = await pool.acquire()
            ids.append(acct.id)
        assert ids == ["a", "b", "a", "b"]

    def test_activate_noop_if_already_present(self):
        pool = _make_pool("a", "b")
        existing = pool._accounts[0]
        pool.activate(existing)
        assert len(pool._accounts) == 2

    @pytest.mark.asyncio
    async def test_deactivate_all_then_activate(self):
        pool = _make_pool("a", "b")
        pool.deactivate("a")
        pool.deactivate("b")

        with pytest.raises(NoHealthyAccountError):
            await pool.acquire()

        new_acct = _make_account("c")
        pool.activate(new_acct)

        acct = await pool.acquire()
        assert acct.id == "c"


class TestErrorClassification:
    @pytest.mark.parametrize(
        "status,expected",
        [
            (401, "account"),
            (403, "account"),
            (429, "account"),
            (500, "upstream"),
            (502, "upstream"),
            (503, "upstream"),
            (504, "upstream"),
            (400, "client"),
            (422, "client"),
            (404, "client"),
        ],
    )
    def test_classify(self, status, expected):
        assert AccountPool._classify_error(status) == expected


# ---------------------------------------------------------------------------
# CredentialProvider
# ---------------------------------------------------------------------------


class TestCredentialProvider:
    def test_load_accounts(self, tmp_path):
        data = {
            "accounts": [
                {
                    "id": "acct_01",
                    "label": "test-1",
                    "access_token": "tok1",
                    "refresh_token": "ref1",
                    "expires_at": 9999999999999,
                    "account_id": "org1",
                    "tier": "plus",
                    "enabled": True,
                },
                {
                    "id": "acct_02",
                    "label": "test-2",
                    "access_token": "tok2",
                    "refresh_token": "ref2",
                    "expires_at": 9999999999999,
                    "account_id": "org2",
                    "tier": "pro",
                    "enabled": False,
                },
            ]
        }
        f = tmp_path / "accounts.json"
        f.write_text(json.dumps(data))

        provider = CredentialProvider(str(f))
        accounts = provider.load_accounts()

        # Only enabled accounts returned
        assert len(accounts) == 1
        assert accounts[0].id == "acct_01"
        assert accounts[0].tier == "plus"

    @pytest.mark.asyncio
    async def test_get_valid_token_no_refresh_needed(self, tmp_path):
        f = tmp_path / "accounts.json"
        f.write_text(json.dumps({"accounts": []}))

        provider = CredentialProvider(str(f), refresh_margin=30)
        account = _make_account(expires_at=int(time.time() * 1000) + 3600_000)

        token = await provider.get_valid_token(account)
        assert token == account.access_token

    @pytest.mark.asyncio
    async def test_refresh_token(self, tmp_path):
        """Verify token refresh updates account credentials."""
        data = {
            "accounts": [
                {
                    "id": "acct_01",
                    "label": "test",
                    "access_token": "old_token",
                    "refresh_token": "old_refresh",
                    "expires_at": int(time.time() * 1000) - 1000,  # expired
                    "account_id": "org1",
                    "tier": "plus",
                    "enabled": True,
                }
            ]
        }
        f = tmp_path / "accounts.json"
        f.write_text(json.dumps(data))

        provider = CredentialProvider(str(f), refresh_margin=30)
        accounts = provider.load_accounts()
        account = accounts[0]

        async def mock_refresh(acct):
            acct.access_token = "new_token"
            acct.refresh_token = "new_refresh"
            acct.expires_at = int(time.time() * 1000) + 3600_000

        with patch.object(provider, "_refresh_token", side_effect=mock_refresh):
            token = await provider.get_valid_token(account)

        assert token == "new_token"
        assert account.access_token == "new_token"
        assert account.refresh_token == "new_refresh"

    @pytest.mark.asyncio
    async def test_single_flight_refresh(self, tmp_path):
        """Concurrent refreshes should be coalesced under the per-account lock."""
        data = {
            "accounts": [
                {
                    "id": "acct_01",
                    "label": "test",
                    "access_token": "old",
                    "refresh_token": "ref",
                    "expires_at": int(time.time() * 1000) - 1000,
                    "account_id": "org1",
                    "tier": "plus",
                    "enabled": True,
                }
            ]
        }
        f = tmp_path / "accounts.json"
        f.write_text(json.dumps(data))

        provider = CredentialProvider(str(f), refresh_margin=30)
        accounts = provider.load_accounts()
        account = accounts[0]

        call_count = 0

        async def mock_refresh(acct):
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.05)
            acct.access_token = "new_token"
            acct.expires_at = int(time.time() * 1000) + 3600_000

        with patch.object(provider, "_refresh_token", side_effect=mock_refresh):
            # Launch 5 concurrent refreshes
            results = await asyncio.gather(*[provider.get_valid_token(account) for _ in range(5)])

        # Only 1 refresh should have happened (single-flight)
        assert call_count == 1
        assert all(r == "new_token" for r in results)
