"""Credential provider for Claude subscription access.

Manages OAuth token lifecycle (refresh, persist) for Claude subscription
accounts. Uses the same ``AccountPool`` from ``codex_token`` for
health-aware rotation — only the credential type and OAuth endpoint differ.
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
# Constants  (⚠️ from CLIProxyAPI — provisional until real traffic validates)
# ---------------------------------------------------------------------------

_OAUTH_TOKEN_URL = "https://api.anthropic.com/v1/oauth/token"
_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"  # gitleaks:allow (public OAuth client ID)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class ClaudeAccountCredential:
    """A single Claude subscription account's credentials."""

    id: str
    label: str
    access_token: str
    refresh_token: str
    expires_at: int  # Unix epoch in milliseconds
    organization_id: str  # From token response organization.uuid
    email: str = ""
    plan: str = "pro"  # pro / max / team / enterprise
    enabled: bool = True


# ---------------------------------------------------------------------------
# ClaudeCredentialProvider — OAuth token lifecycle
# ---------------------------------------------------------------------------


class ClaudeCredentialProvider:
    """Loads, refreshes, and persists Claude OAuth credentials.

    Args:
        accounts_file: Path to the JSON credentials file.
        refresh_margin: Seconds before expiry to trigger proactive refresh.
    """

    def __init__(self, accounts_file: str, refresh_margin: int = 300) -> None:
        self._accounts: dict[str, ClaudeAccountCredential] = {}
        self._accounts_file = accounts_file
        self._refresh_margin = refresh_margin
        self._refresh_locks: dict[str, asyncio.Lock] = {}

    def load_accounts(self) -> list[ClaudeAccountCredential]:
        """Read the JSON file and return enabled accounts."""
        with open(self._accounts_file) as f:
            data = json.load(f)

        accounts: list[ClaudeAccountCredential] = []
        for entry in data.get("accounts", []):
            acct = ClaudeAccountCredential(
                id=entry["id"],
                label=entry.get("label", entry["id"]),
                access_token=entry["access_token"],
                refresh_token=entry["refresh_token"],
                expires_at=int(entry["expires_at"]),
                organization_id=entry["organization_id"],
                email=entry.get("email", ""),
                plan=entry.get("plan", "pro"),
                enabled=entry.get("enabled", True),
            )
            if acct.enabled:
                accounts.append(acct)
            self._accounts[acct.id] = acct

        logger.info(f"Loaded {len(accounts)} enabled Claude accounts from {self._accounts_file}")
        return accounts

    async def get_valid_token(
        self, account: ClaudeAccountCredential, *, force_refresh: bool = False
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

    async def _refresh_token(self, account: ClaudeAccountCredential) -> None:
        """Exchange refresh_token for a new access_token via Anthropic OAuth."""
        logger.info(f"Refreshing token for Claude account {account.id} ({account.label})")

        payload = {
            "grant_type": "refresh_token",
            "refresh_token": account.refresh_token,
            "client_id": _CLIENT_ID,
        }

        async with (
            aiohttp.ClientSession() as session,
            session.post(_OAUTH_TOKEN_URL, json=payload) as resp,
        ):
            if resp.status != 200:
                body = await resp.text()
                logger.error(
                    f"Token refresh failed for Claude {account.id}: "
                    f"status={resp.status} body={body[:200]}"
                )
                raise RuntimeError(
                    f"Token refresh failed for Claude account {account.id}: HTTP {resp.status}"
                )
            data = await resp.json()

        now_ms = int(time.time() * 1000)
        account.access_token = data["access_token"]
        account.refresh_token = data.get("refresh_token", account.refresh_token)
        account.expires_at = now_ms + int(data["expires_in"]) * 1000

        # Update org info if returned
        org = data.get("organization")
        if isinstance(org, dict) and org.get("uuid"):
            account.organization_id = org["uuid"]

        acct_info = data.get("account")
        if isinstance(acct_info, dict) and acct_info.get("email_address"):
            account.email = acct_info["email_address"]

        # Update internal registry and persist
        self._accounts[account.id] = account
        await self._persist()
        logger.info(
            f"Token refreshed for Claude account {account.id}, expires in {data['expires_in']}s"
        )

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
                    "organization_id": acct.organization_id,
                    "email": acct.email,
                    "plan": acct.plan,
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
        except Exception:
            os.close(fd) if not os.get_inheritable(fd) else None
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise
