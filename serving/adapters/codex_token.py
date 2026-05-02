"""Credential provider and account pool for Codex subscription access.

Manages OAuth token lifecycle (refresh, persist) and health-aware
round-robin rotation across multiple subscription accounts.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from dataclasses import dataclass

import aiohttp

from serving.utils.logging import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

_AUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"


@dataclass
class AccountCredential:
    """A single Codex subscription account's credentials."""

    id: str
    label: str
    access_token: str
    refresh_token: str
    expires_at: int  # Unix epoch in milliseconds
    account_id: str  # ChatGPT-Account-Id header value
    tier: str = "plus"
    enabled: bool = True


@dataclass
class AccountHealth:
    """Runtime health state for a single account."""

    healthy: bool = True
    consecutive_failures: int = 0
    last_success: float = 0.0
    last_failure: float = 0.0
    cooldown_until: float = 0.0


class NoHealthyAccountError(Exception):
    """Raised when all subscription accounts are unhealthy."""


# ---------------------------------------------------------------------------
# CredentialProvider — OAuth token lifecycle
# ---------------------------------------------------------------------------


class CredentialProvider:
    """Loads, refreshes, and persists Codex OAuth credentials.

    Args:
        accounts_file: Path to the JSON credentials file.
        refresh_margin: Seconds before expiry to trigger proactive refresh.
    """

    def __init__(self, accounts_file: str, refresh_margin: int = 30) -> None:
        self._accounts: dict[str, AccountCredential] = {}
        self._accounts_file = accounts_file
        self._refresh_margin = refresh_margin
        self._refresh_locks: dict[str, asyncio.Lock] = {}

    def load_accounts(self) -> list[AccountCredential]:
        """Read the JSON file and return enabled accounts."""
        with open(self._accounts_file) as f:
            data = json.load(f)

        accounts: list[AccountCredential] = []
        for entry in data.get("accounts", []):
            acct = AccountCredential(
                id=entry["id"],
                label=entry.get("label", entry["id"]),
                access_token=entry["access_token"],
                refresh_token=entry["refresh_token"],
                expires_at=int(entry["expires_at"]),
                account_id=entry["account_id"],
                tier=entry.get("tier", "plus"),
                enabled=entry.get("enabled", True),
            )
            if acct.enabled:
                accounts.append(acct)
            self._accounts[acct.id] = acct

        logger.info(f"Loaded {len(accounts)} enabled Codex accounts from {self._accounts_file}")
        return accounts

    async def get_valid_token(
        self, account: AccountCredential, *, force_refresh: bool = False
    ) -> str:
        """Return a valid access token, refreshing proactively if near expiry.

        Args:
            account: The account to get a token for.
            force_refresh: If True, bypass expiry check and always refresh.
                Use after receiving a 401 where the token was rejected
                server-side despite not being locally expired.
        """
        if not force_refresh:
            now_ms = int(time.time() * 1000)
            if account.expires_at - now_ms > self._refresh_margin * 1000:
                return account.access_token

        # Refresh under per-account lock (single-flight)
        lock = self._refresh_locks.setdefault(account.id, asyncio.Lock())
        async with lock:
            # Double-check after acquiring lock (skip if forced)
            if not force_refresh:
                now_ms = int(time.time() * 1000)
                if account.expires_at - now_ms > self._refresh_margin * 1000:
                    return account.access_token

            await self._refresh_token(account)
            return account.access_token

    async def _refresh_token(self, account: AccountCredential) -> None:
        """Exchange refresh_token for a new access_token via OpenAI OAuth."""
        logger.info(f"Refreshing token for account {account.id} ({account.label})")

        payload = {
            "grant_type": "refresh_token",
            "refresh_token": account.refresh_token,
            "client_id": _CLIENT_ID,
        }

        async with (
            aiohttp.ClientSession() as session,
            session.post(_AUTH_TOKEN_URL, json=payload) as resp,
        ):
            if resp.status != 200:
                body = await resp.text()
                logger.error(
                    f"Token refresh failed for {account.id}: status={resp.status} body={body[:200]}"
                )
                raise RuntimeError(
                    f"Token refresh failed for account {account.id}: HTTP {resp.status}"
                )
            data = await resp.json()

        now_ms = int(time.time() * 1000)
        account.access_token = data["access_token"]
        account.refresh_token = data.get("refresh_token", account.refresh_token)
        account.expires_at = now_ms + int(data["expires_in"]) * 1000

        # Update internal registry and persist
        self._accounts[account.id] = account
        await self._persist()
        logger.info(f"Token refreshed for account {account.id}, expires in {data['expires_in']}s")

    async def _persist(self) -> None:
        """Atomically write accounts back to disk (write tmp + rename)."""
        entries = []
        for acct in self._accounts.values():
            entries.append(
                {
                    "id": acct.id,
                    "label": acct.label,
                    "type": "oauth",
                    "access_token": acct.access_token,
                    "refresh_token": acct.refresh_token,
                    "expires_at": acct.expires_at,
                    "account_id": acct.account_id,
                    "tier": acct.tier,
                    "enabled": acct.enabled,
                }
            )

        data = json.dumps({"accounts": entries}, indent=2)
        dir_name = os.path.dirname(self._accounts_file)

        # Atomic write via temp file + rename
        fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
        try:
            os.write(fd, data.encode())
            os.close(fd)
            os.replace(tmp_path, self._accounts_file)
            # Restrict perms: file holds OAuth refresh tokens that grant
            # long-lived account access. mkstemp creates 0o600 by default
            # but os.replace preserves the destination's mode if it
            # already existed, so we re-assert the tight perms here.
            os.chmod(self._accounts_file, 0o600)
        except Exception:
            os.close(fd) if not os.get_inheritable(fd) else None
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise


# ---------------------------------------------------------------------------
# AccountPool — health-aware round-robin rotation
# ---------------------------------------------------------------------------


class AccountPool:
    """Manages a pool of Codex accounts with health-aware rotation.

    Args:
        accounts: List of enabled account credentials.
        cooldown: Seconds to cool down after reaching failure threshold.
        failure_threshold: Consecutive account-level failures before marking unhealthy.
    """

    def __init__(
        self,
        accounts: list[AccountCredential],
        cooldown: int = 60,
        failure_threshold: int = 3,
    ) -> None:
        self._accounts = accounts
        self._index = 0
        self._health: dict[str, AccountHealth] = {}
        self._lock = asyncio.Lock()
        self._cooldown = cooldown
        self._failure_threshold = failure_threshold

    async def acquire(self) -> AccountCredential:
        """Select the next healthy account via round-robin.

        Raises:
            NoHealthyAccountError: If all accounts are unhealthy.
        """
        async with self._lock:
            for _ in range(len(self._accounts)):
                account = self._accounts[self._index]
                self._index = (self._index + 1) % len(self._accounts)
                if self._is_healthy(account.id):
                    return account
            raise NoHealthyAccountError("All subscription accounts are unhealthy")

    def report_success(self, account_id: str) -> None:
        """Mark account as healthy after a successful request."""
        self._health[account_id] = AccountHealth(
            healthy=True,
            consecutive_failures=0,
            last_success=time.time(),
        )

    def report_failure(self, account_id: str, status_code: int) -> None:
        """Track failure with error classification.

        Only account-level errors (401/403/429) degrade health.
        Client errors and upstream errors do NOT count against the account.
        """
        category = self._classify_error(status_code)
        health = self._health.get(account_id, AccountHealth())
        health.last_failure = time.time()

        if category == "account":
            health.consecutive_failures += 1
            if status_code == 429:
                # Quota exhaustion → immediate cooldown (120s)
                health.healthy = False
                health.cooldown_until = time.time() + 120
            elif health.consecutive_failures >= self._failure_threshold:
                health.healthy = False
                health.cooldown_until = time.time() + self._cooldown
        # upstream (5xx) and client (4xx) errors: no health impact

        self._health[account_id] = health

    def _is_healthy(self, account_id: str) -> bool:
        """Check if an account is healthy, auto-recovering after cooldown."""
        health = self._health.get(account_id)
        if health is None:
            return True
        if not health.healthy:
            if time.time() >= health.cooldown_until:
                # Auto-recover after cooldown
                health.healthy = True
                health.consecutive_failures = 0
                return True
            return False
        return True

    def deactivate(self, account_id: str) -> None:
        """Remove an account from the active pool (e.g., revoked).

        Safe to call if the account is not in the pool.
        """
        original_len = len(self._accounts)
        self._accounts = [a for a in self._accounts if a.id != account_id]
        self._health.pop(account_id, None)
        if self._accounts and self._index >= len(self._accounts):
            self._index = 0
        if len(self._accounts) < original_len:
            logger.info(f"Deactivated account {account_id} from pool")

    def activate(self, account) -> None:
        """Add an account back to the active pool.

        No-op if the account is already present.
        """
        if any(a.id == account.id for a in self._accounts):
            return
        self._accounts.append(account)
        logger.info(f"Activated account {account.id} in pool")

    @staticmethod
    def _classify_error(status_code: int) -> str:
        """Classify HTTP error into account / upstream / client."""
        if status_code in (401, 403, 429):
            return "account"
        if status_code >= 500:
            return "upstream"
        return "client"
