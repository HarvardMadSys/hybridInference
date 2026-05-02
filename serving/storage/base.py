"""Abstract base classes for the storage layer.

Two separate store contracts reflecting the hybrid architecture:
- OperationalStore: users, api_keys, auth_sessions, tokens, audit (may live in D1)
- LogStore: api_logs, api_stats_hourly (always PostgreSQL)

No implementation details or SQL in this file — just the contracts.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from datetime import datetime
    from decimal import Decimal


# ---------------------------------------------------------------------------
# Shared types
# ---------------------------------------------------------------------------

Row = dict[str, Any]
"""Generic row type returned by store methods (column-name → value)."""


# ---------------------------------------------------------------------------
# Column allowlists for dynamic UPDATE methods
# ---------------------------------------------------------------------------
# Maps external field name → canonical DB column name.  Only these columns
# may be passed to update_user_fields / update_key.  The mapping is 1:1
# today but interpolating from .values() guarantees only known-safe
# identifiers ever reach SQL, even if external names diverge later.

USERS_MUTABLE_COLUMNS: dict[str, str] = {
    "email": "email",
    "password_hash": "password_hash",
    "user_name": "user_name",
    "preferences": "preferences",
    "role": "role",
    "email_verified": "email_verified",
    "status": "status",
    "approval_note": "approval_note",
    "reviewed_at": "reviewed_at",
    "reviewed_by": "reviewed_by",
    "last_login_at": "last_login_at",
}

API_KEYS_MUTABLE_COLUMNS: dict[str, str] = {
    "user_name": "user_name",
    "status": "status",
    "quota_daily_cost_usd": "quota_daily_cost_usd",
    "quota_monthly_cost_usd": "quota_monthly_cost_usd",
    "expires_at": "expires_at",
    "last_used_at": "last_used_at",
    "notes": "notes",
    "metadata": "metadata",
    "account_id": "account_id",
}


# ---------------------------------------------------------------------------
# OperationalStore — users, keys, sessions, tokens, audit
# ---------------------------------------------------------------------------


class OperationalStore(ABC):
    """Abstract interface for operational data (users, keys, sessions, tokens, audit).

    Every method is async.  Implementations must never raise
    ``NotImplementedError`` — each store fully satisfies this contract.
    """

    # -- lifecycle -----------------------------------------------------------

    @abstractmethod
    async def initialize(self) -> None:
        """Create tables/indexes and run any idempotent migrations."""

    @abstractmethod
    async def cleanup(self) -> None:
        """Release connections / close pools."""

    @abstractmethod
    async def health_check(self) -> bool:
        """Return True if the store is reachable and healthy."""

    # -- users ---------------------------------------------------------------

    @abstractmethod
    async def get_user_by_id(self, user_id: str) -> Row | None:
        """Fetch a single user row by primary key.

        Returns columns: id, email, user_name, role, status, email_verified,
        created_at, last_login_at, password_hash, preferences.
        """

    @abstractmethod
    async def get_user_by_email(self, email: str) -> Row | None:
        """Fetch a single user row by (lowercased) email.

        Returns the same columns as ``get_user_by_id``.
        """

    @abstractmethod
    async def create_user(
        self,
        *,
        user_id: str,
        email: str,
        password_hash: str,
        user_name: str | None = None,
        email_verified: bool = False,
        status: str = "active",
    ) -> None:
        """Insert a new user row."""

    @abstractmethod
    async def update_user_fields(
        self,
        user_id: str,
        **fields: Any,
    ) -> None:
        """Update one or more columns on the users table for *user_id*.

        Accepted keyword arguments map directly to column names, e.g.
        ``update_user_fields(uid, role="admin", status="active")``.
        """

    @abstractmethod
    async def update_user_last_login(self, user_id: str) -> None:
        """Set ``last_login_at`` to the current timestamp."""

    @abstractmethod
    async def delete_user(
        self,
        user_id: str,
        *,
        admin_ip: str,
        admin_id: str,
        reason: str | None = None,
        email: str | None = None,
    ) -> None:
        """Soft-delete: set status='deleted', revoke keys, purge sessions/tokens.

        All mutations and the audit-log insert must be atomic (single
        transaction).
        """

    @abstractmethod
    async def list_users(
        self,
        *,
        status: str | None = None,
        search: str | None = None,
        sort_by: Literal[
            "created", "cost_today", "cost_month", "cost_alltime", "last_login"
        ] = "created",
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[int, list[Row], Row]:
        """Return ``(total_count, user_rows, status_counts_row)``.

        *sort_by* values that reference cost (``cost_today``, ``cost_month``,
        ``cost_alltime``) require joining against ``api_logs`` which lives in
        the **LogStore**.  Implementations that cannot access ``api_logs``
        directly must accept an optional *usage_provider* callback or return
        cost columns as zero and let the caller enrich them.

        ``status_counts_row`` is a dict with keys ``all``, ``pending_approval``,
        ``active``, ``suspended``, ``rejected``, ``deleted``.
        """

    @abstractmethod
    async def approve_user(
        self,
        user_id: str,
        *,
        admin_id: str,
        note: str | None = None,
    ) -> None:
        """Set status='active', record reviewer and note."""

    @abstractmethod
    async def reject_user(
        self,
        user_id: str,
        *,
        admin_id: str,
        reason: str,
    ) -> None:
        """Set status='rejected', record reviewer and reason."""

    @abstractmethod
    async def get_user_counts_by_status(self) -> dict[str, int]:
        """Return ``{status_value: count}`` for all statuses."""

    @abstractmethod
    async def get_active_user_counts(self) -> dict[str, int]:
        """Return ``{"total": N, "dau": N, "mau": N}``.

        Derived from ``users.last_login_at`` — does not touch api_logs.
        """

    # -- api keys ------------------------------------------------------------

    @abstractmethod
    async def get_auth_context_by_key_hash(self, key_hash: str) -> Row | None:
        """Materialized auth lookup for ``verify_api_key``.

        Returns the full projection needed in a **single** call:
        ``id, user_id, user_name, quota_daily_cost_usd, email, role``.

        The query joins ``api_keys`` with ``users`` and filters on
        ``status='active'``, unexpired key, and active user.

        Do NOT split into narrower lookups — this is the hot-path method
        and must resolve in one round-trip.
        """

    @abstractmethod
    async def get_auth_context_lightweight(self, key_hash: str) -> Row | None:
        """Lightweight identity lookup (no quota check, no last_used write).

        Returns: ``user_id, email, role``.
        """

    @abstractmethod
    async def update_key_last_used(self, key_id: int) -> None:
        """Set ``last_used_at = NOW()`` for the given key id. Fire-and-forget."""

    @abstractmethod
    async def create_key(
        self,
        *,
        key_hash: str,
        key_prefix: str,
        user_id: str,
        user_name: str | None = None,
        quota_daily_cost_usd: Decimal | float = 1000.0,
        quota_monthly_cost_usd: Decimal | float | None = None,
        expires_at: datetime | None = None,
        notes: str | None = None,
        metadata: str | None = None,
        account_id: str | None = None,
    ) -> Row:
        """Insert a new API key. Returns the inserted row (at least ``id``, ``created_at``)."""

    @abstractmethod
    async def check_active_key_exists(self, user_id: str) -> bool:
        """Return True if *user_id* already has an active key."""

    @abstractmethod
    async def list_keys(
        self,
        *,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[int, list[Row]]:
        """Return ``(total_count, key_rows)`` with optional filters."""

    @abstractmethod
    async def get_key_detail(self, user_id: str) -> Row | None:
        """Fetch full key row for a given *user_id* (admin detail view)."""

    @abstractmethod
    async def update_key(self, user_id: str, **fields: Any) -> None:
        """Dynamically update key columns for *user_id*."""

    @abstractmethod
    async def revoke_key(self, user_id: str, *, hard_delete: bool = False) -> None:
        """Soft-revoke (status='revoked') or hard-delete the key."""

    @abstractmethod
    async def regenerate_key(
        self,
        user_id: str,
        *,
        new_key_hash: str,
        new_key_prefix: str,
    ) -> str:
        """Atomically replace the key hash/prefix. Returns old key_prefix."""

    @abstractmethod
    async def get_key_by_account_or_user(self, account_id: str) -> Row | None:
        """Fetch key row by account_id or user_id (for login lookup)."""

    @abstractmethod
    async def get_active_key_by_account(self, account_id: str) -> Row | None:
        """Fetch the active key for self-registered user by account_id."""

    # -- auth sessions -------------------------------------------------------

    @abstractmethod
    async def create_session(
        self,
        *,
        session_id: str,
        user_id: str,
        refresh_token_hash: str,
        jti: str,
        sid: str,
        expires_at: datetime,
    ) -> None:
        """Insert a new auth session."""

    @abstractmethod
    async def get_session_by_token_hash(self, token_hash: str) -> Row | None:
        """Fetch session row by refresh_token_hash.

        Returns: ``user_id, expires_at, revoked``.
        """

    @abstractmethod
    async def rotate_session(
        self,
        session_id: str,
        *,
        new_refresh_token_hash: str,
        new_jti: str,
    ) -> None:
        """Update the session with a new refresh token hash and JTI."""

    @abstractmethod
    async def revoke_session(self, session_id: str) -> None:
        """Set ``revoked = TRUE`` on the session."""

    @abstractmethod
    async def delete_user_sessions(self, user_id: str) -> None:
        """Delete all sessions for a user (used during user deletion)."""

    # -- email verification tokens -------------------------------------------

    @abstractmethod
    async def create_verification_token(
        self,
        *,
        token: str,
        user_id: str,
        expires_at: datetime,
    ) -> None:
        """Insert a new email verification token."""

    @abstractmethod
    async def get_verification_token(self, token: str) -> Row | None:
        """Fetch verification token row.

        Returns: ``token, user_id, created_at, expires_at, used_at``.
        """

    @abstractmethod
    async def mark_verification_used(self, token: str) -> None:
        """Set ``used_at = NOW()`` on the token."""

    @abstractmethod
    async def mark_user_email_verified(self, user_id: str) -> None:
        """Set ``email_verified = TRUE`` on the user."""

    @abstractmethod
    async def delete_user_verification_tokens(self, user_id: str) -> None:
        """Delete all verification tokens for a user."""

    # -- password reset tokens -----------------------------------------------

    @abstractmethod
    async def create_reset_token(
        self,
        *,
        token: str,
        user_id: str,
        expires_at: datetime,
    ) -> None:
        """Insert a new password reset token."""

    @abstractmethod
    async def get_reset_token(self, token: str) -> Row | None:
        """Fetch reset token row.

        Returns: ``token, user_id, created_at, expires_at, used_at``.
        """

    @abstractmethod
    async def mark_reset_used(self, token: str) -> None:
        """Set ``used_at = NOW()`` on the token."""

    @abstractmethod
    async def delete_user_reset_tokens(self, user_id: str) -> None:
        """Delete all reset tokens for a user."""

    # -- admin audit log -----------------------------------------------------

    @abstractmethod
    async def log_admin_action(
        self,
        *,
        admin_ip: str,
        action: str,
        target_user_id: str | None = None,
        details: dict[str, Any] | None = None,
        success: bool = True,
    ) -> None:
        """Insert a row into admin_audit_log."""

    @abstractmethod
    async def list_audit_log(
        self,
        *,
        action: str | None = None,
        target_user_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[int, list[Row]]:
        """Return ``(total_count, audit_rows)`` with optional filters."""

    # -- cost counters (user_daily_cost table) ---------------------------------

    @abstractmethod
    async def increment_user_cost(
        self,
        user_id: str,
        cost_usd: float,
        *,
        day: str | None = None,
    ) -> None:
        """Atomically increment the daily cost counter for *user_id*.

        Uses upsert: inserts a new row if none exists for (user_id, day),
        otherwise adds *cost_usd* to the existing total and increments
        the request count.

        Only call this for billed requests (successful responses where
        cost > 0), not for auth failures or 4xx errors.

        Args:
            user_id: The user whose cost to increment.
            cost_usd: Amount to add (must be >= 0).
            day: Date string (YYYY-MM-DD). Defaults to UTC today.
        """

    @abstractmethod
    async def get_user_cost_today(self, user_id: str) -> float:
        """Return total cost_usd for *user_id* since UTC midnight today.

        Reads from the ``user_daily_cost`` counter table, not from logs.
        """

    @abstractmethod
    async def get_user_cost_period(
        self,
        user_id: str,
        period: Literal["today", "month"],
    ) -> float:
        """Return total cost_usd for *user_id* within the given period.

        Reads from the ``user_daily_cost`` counter table.
        ``today`` returns the current day's total.
        ``month`` sums all days in the current UTC month.
        """

    @abstractmethod
    async def get_batch_usage(
        self,
        user_ids: list[str],
        period: Literal["today", "month"],
    ) -> dict[str, float]:
        """Return ``{user_id: cost_usd}`` for a batch of users.

        Reads from the ``user_daily_cost`` counter table.
        Used by admin list endpoints to avoid N+1 queries.
        """

    # -- user preferences (JSONB on users table) -----------------------------

    @abstractmethod
    async def get_user_preferences(self, user_id: str) -> dict[str, Any]:
        """Return the preferences JSONB column for *user_id*, parsed as dict."""

    @abstractmethod
    async def update_user_preferences(
        self,
        user_id: str,
        preferences: dict[str, Any],
    ) -> None:
        """Atomically replace the full preferences JSONB column."""


# ---------------------------------------------------------------------------
# LogStore — api_logs, api_stats_hourly
# ---------------------------------------------------------------------------


class LogStore(ABC):
    """Abstract interface for request logging and usage analytics.

    Implementations:
    - ``PostgresLogStore``: full rows in PostgreSQL (prompt/response content).
    - ``D1LogStore``: slim rows in Cloudflare D1 (no content, buffered writes).
    """

    # -- lifecycle -----------------------------------------------------------

    @abstractmethod
    async def initialize(self) -> None:
        """Create tables/indexes and run any idempotent migrations."""

    @abstractmethod
    async def cleanup(self) -> None:
        """Release connections / close pools."""

    @abstractmethod
    async def health_check(self) -> bool:
        """Return True if the store is reachable and healthy."""

    # -- request logging -----------------------------------------------------

    @abstractmethod
    async def log_request(
        self,
        *,
        request_id: str,
        model_id: str,
        provider: str,
        prompt: list[dict[str, Any]] | str,
        response: dict[str, Any] | str | None,
        usage: dict[str, int] | None,
        latency_ms: int,
        status_code: int,
        error: str | None = None,
        params: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        ttft_ms: int | None = None,
        prompt_hash: str | None = None,
        response_hash: str | None = None,
        store_full_content: bool | None = None,
        pricing: dict[str, str] | None = None,
    ) -> None:
        """Insert a single request log row. Idempotent (ON CONFLICT DO NOTHING)."""

    # -- usage / cost queries ------------------------------------------------

    @abstractmethod
    async def get_user_cost_today(self, user_id: str) -> float:
        """Return total cost_usd for *user_id* since UTC midnight today."""

    @abstractmethod
    async def get_user_cost_period(
        self,
        user_id: str,
        period: Literal["today", "month"],
    ) -> float:
        """Return total cost_usd for *user_id* within the given period."""

    @abstractmethod
    async def get_user_usage_detail(
        self,
        user_id: str,
    ) -> dict[str, Any]:
        """Return detailed usage stats for a user (today, week, month, all-time).

        Used by the user dashboard ``/user/usage`` endpoint.
        Returns dict with keys ``today``, ``week``, ``month``, ``alltime``
        each containing ``cost_usd`` and ``requests``.
        """

    @abstractmethod
    async def get_batch_usage(
        self,
        user_ids: list[str],
        period: Literal["today", "month"],
    ) -> dict[str, float]:
        """Return ``{user_id: cost_usd}`` for a batch of users within the period.

        Used by admin list endpoints to avoid N+1 queries.
        """

    @abstractmethod
    async def get_user_detail_usage(
        self,
        user_id: str,
    ) -> dict[str, Any]:
        """Return usage detail for admin user-detail view.

        Returns dict with keys: ``usage_today_usd``, ``usage_today_requests``,
        ``usage_month_usd``, ``usage_month_requests``, ``models_used``,
        ``last_request_at``.
        """

    @abstractmethod
    async def get_key_detail_usage(
        self,
        user_id: str,
    ) -> dict[str, Any]:
        """Return usage detail for admin key-detail view.

        Returns dict with keys: ``today`` (cost_usd, requests),
        ``this_month`` (cost_usd, requests), ``models_used``,
        ``last_request_at``.
        """

    # -- analytics -----------------------------------------------------------

    @abstractmethod
    async def get_model_activity(self, window_minutes: int = 10) -> dict[str, Any]:
        """Aggregate recent real-user traffic per (model_id, provider).

        Returns dict keyed by ``"model_id::provider"`` with per-route stats.
        Synthetic probes (``user_id IS NULL``) are excluded.
        """

    @abstractmethod
    async def get_stats(
        self,
        *,
        model_id: str | None = None,
        provider: str | None = None,
        hours: int = 24,
    ) -> list[Row]:
        """Fetch aggregated hourly stats from api_stats_hourly."""
