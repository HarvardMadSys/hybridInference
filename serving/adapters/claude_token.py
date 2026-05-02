"""Credential provider for Claude subscription access.

Manages OAuth token lifecycle (refresh, persist) for Claude subscription
accounts. Uses the same ``AccountPool`` from ``codex_token`` for
health-aware rotation — only the credential type and OAuth endpoint differ.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import time
from dataclasses import dataclass

import aiohttp

from serving.utils.logging import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants  (from CLIProxyAPI — provisional until real traffic validates)
# ---------------------------------------------------------------------------

_OAUTH_TOKEN_URL = "https://api.anthropic.com/v1/oauth/token"
_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"  # gitleaks:allow (public OAuth client ID)

# Data format version. Bump when the on-disk schema changes.
_FORMAT_VERSION = 2

# Valid account states
_VALID_STATES = {"active", "cooldown", "revoked", "disabled"}


# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------


class TokenRefreshError(Exception):
    """Base class for token refresh failures."""


class RefreshTokenRevokedError(TokenRefreshError):
    """Refresh token permanently invalidated (invalid_grant)."""

    def __init__(self, account_id: str, body: str = "") -> None:
        self.account_id = account_id
        self.body = body
        super().__init__(f"Refresh token revoked for {account_id}: {body[:200]}")


class RefreshTokenTransientError(TokenRefreshError):
    """Transient refresh failure (network, server error, etc.)."""

    def __init__(self, account_id: str, status: int, body: str = "") -> None:
        self.account_id = account_id
        self.status = status
        self.body = body
        super().__init__(f"Transient refresh failure for {account_id}: HTTP {status} {body[:200]}")


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
    state: str = "active"  # active / cooldown / revoked / disabled
    state_changed_at: int = 0  # Unix epoch ms when state last changed
    revoke_reason: str = ""
    consecutive_failures: int = 0


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
        """Read the JSON file and return active accounts.

        Handles v1→v2 migration (``enabled`` → ``state``).
        Filters out ``revoked``/``disabled`` accounts from the returned list.
        Promotes persisted ``cooldown`` → ``active`` (cooldown is transient).
        Skips entries missing required fields with a warning.
        """
        with open(self._accounts_file) as f:
            data = json.load(f)

        is_v1 = "version" not in data
        needs_persist = False

        accounts: list[ClaudeAccountCredential] = []
        for entry in data.get("accounts", []):
            # Validate required fields
            missing = [
                k for k in ("id", "access_token", "refresh_token", "expires_at") if k not in entry
            ]
            if missing:
                logger.warning(
                    f"Skipping account entry with missing fields {missing}: "
                    f"{entry.get('id', '<no id>')}"
                )
                continue

            # v1→v2 migration: convert enabled → state
            if is_v1:
                enabled = entry.get("enabled", True)
                if not enabled:
                    entry.setdefault("state", "disabled")
                else:
                    entry.setdefault("state", "active")
                needs_persist = True

            state = entry.get("state", "active")

            # Promote cooldown → active on load (cooldown is transient runtime state)
            if state == "cooldown":
                state = "active"
                needs_persist = True

            acct = ClaudeAccountCredential(
                id=entry["id"],
                label=entry.get("label", entry["id"]),
                access_token=entry["access_token"],
                refresh_token=entry["refresh_token"],
                expires_at=int(entry["expires_at"]),
                organization_id=entry.get("organization_id", ""),
                email=entry.get("email", ""),
                plan=entry.get("plan", "pro"),
                state=state,
                state_changed_at=int(entry.get("state_changed_at", 0)),
                revoke_reason=entry.get("revoke_reason", ""),
                consecutive_failures=int(entry.get("consecutive_failures", 0)),
            )

            # Always store in internal registry (including revoked/disabled)
            self._accounts[acct.id] = acct

            # Only return active accounts for the pool
            if acct.state == "active":
                accounts.append(acct)
            else:
                logger.info(f"Account {acct.id} not added to pool (state={acct.state})")

        logger.info(
            f"Loaded {len(accounts)} active Claude accounts from "
            f"{self._accounts_file} (total: {len(self._accounts)})"
        )

        if needs_persist:
            # Schedule async persist for migration — best-effort
            try:
                loop = asyncio.get_running_loop()
                task = loop.create_task(self._persist())
                # Fire-and-forget: log if it fails, but don't block load
                task.add_done_callback(
                    lambda t: (
                        t.exception()
                        and logger.warning(f"Migration persist failed: {t.exception()}")
                    )
                )
            except RuntimeError:
                # No running loop (called from sync context) — skip
                pass

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

        Raises:
            RefreshTokenRevokedError: If the refresh token is permanently invalid.
            RefreshTokenTransientError: If the refresh fails transiently.
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
        """Exchange refresh_token for a new access_token via Anthropic OAuth.

        Raises:
            RefreshTokenRevokedError: On ``invalid_grant`` (permanent).
            RefreshTokenTransientError: On other non-200 responses.
        """
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

                # Parse error body to distinguish permanent vs transient
                error_code = ""
                try:
                    error_data = json.loads(body)
                    error_code = error_data.get("error", "")
                except (json.JSONDecodeError, AttributeError):
                    pass

                if error_code == "invalid_grant":
                    raise RefreshTokenRevokedError(account.id, body)
                raise RefreshTokenTransientError(account.id, resp.status, body)

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

    async def transition_state(
        self,
        account: ClaudeAccountCredential,
        new_state: str,
        reason: str = "",
        *,
        pool=None,
    ) -> None:
        """Transition an account to a new state and persist.

        Args:
            account: The account to transition.
            new_state: Target state (active/cooldown/revoked/disabled).
            reason: Human-readable reason for the transition.
            pool: Optional AccountPool to deactivate/activate the account in.
        """
        old_state = account.state
        account.state = new_state
        account.state_changed_at = int(time.time() * 1000)

        if new_state == "active":
            account.consecutive_failures = 0
            account.revoke_reason = ""
        elif new_state in ("revoked", "disabled") or new_state == "cooldown":
            account.revoke_reason = reason

        self._accounts[account.id] = account
        await self._persist()

        logger.warning(
            f"Account {account.id} state transition: {old_state} → {new_state}"
            + (f" (reason: {reason})" if reason else "")
        )

        # Update pool membership.
        # Note: cooldown does NOT deactivate — the pool's built-in cooldown
        # mechanism (health check + cooldown_until) handles transient recovery.
        # Only terminal states (revoked/disabled) remove from pool.
        if pool is not None:
            if new_state in ("revoked", "disabled"):
                pool.deactivate(account.id)
            elif new_state == "active":
                pool.activate(account)

    async def _persist(self) -> None:
        """Atomically write accounts back to disk (backup + write tmp + rename)."""
        # Backup current file before overwriting
        if os.path.exists(self._accounts_file):
            backup_path = self._accounts_file + ".bak"
            try:
                shutil.copy2(self._accounts_file, backup_path)
            except OSError as e:
                logger.warning(f"Failed to create backup: {e}")

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
                    "state": acct.state,
                    "state_changed_at": acct.state_changed_at,
                    "revoke_reason": acct.revoke_reason,
                    "consecutive_failures": acct.consecutive_failures,
                }
            )

        payload = {"version": _FORMAT_VERSION, "accounts": entries}
        data = json.dumps(payload, indent=2)
        dir_name = os.path.dirname(self._accounts_file)

        # Atomic write via temp file + rename
        fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
        try:
            os.write(fd, data.encode())
            os.close(fd)
            # File holds OAuth refresh tokens — long-lived account access.
            # chmod the temp file BEFORE rename so the destination is never
            # observable with looser perms (eliminates TOCTOU window).
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, self._accounts_file)
        except Exception:
            os.close(fd) if not os.get_inheritable(fd) else None
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise
