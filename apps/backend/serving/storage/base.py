"""Abstract base classes for the storage layer.

Two separate store contracts reflecting the gateway architecture:
- OperationalStore: users, api_keys, auth_sessions, tokens, audit
- LogStore: api_logs, api_stats_hourly

No implementation details or SQL in this file — just the contracts.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from datetime import datetime
    from decimal import Decimal


@dataclass
class ProviderKeyRow:
    """Masked row for a runtime-managed upstream provider API key."""

    id: str
    provider: str
    key_prefix: str
    label: str | None
    status: str
    created_at: datetime


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
    "max_concurrent_requests": "max_concurrent_requests",
    "admin_note": "admin_note",
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
        signup_reason: str | None = None,
    ) -> None:
        """Insert a new user row.

        ``signup_reason`` captures the free-text use case the user submitted at
        registration; surfaced in the admin user list to aid manual approval.
        """

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
    async def resume_user(
        self,
        user_id: str,
        *,
        admin_ip: str,
        admin_id: str,
        reason: str | None = None,
        email: str | None = None,
    ) -> None:
        """Resume a soft-deleted user: set status='active' and audit.

        API keys remain ``revoked`` — the user must re-create one through
        the normal flow.  All mutations and the audit-log insert are atomic.
        """

    @abstractmethod
    async def hard_delete_user(
        self,
        user_id: str,
        *,
        admin_ip: str,
        admin_id: str,
        reason: str | None = None,
        email: str | None = None,
    ) -> dict[str, int]:
        """Permanently delete a user row and all operationally-linked rows.

        Wipes (in one transaction):

        - ``api_keys`` (by ``account_id`` or ``user_id``)
        - ``auth_sessions``
        - ``email_verification_tokens``
        - ``password_reset_tokens``
        - ``user_daily_cost``
        - ``admin_audit_log`` rows referencing this user (compliance loss
          accepted at the caller level)
        - ``users`` row itself
        - Inserts a NEW ``admin_audit_log`` row for the hard-delete itself.

        ``api_logs`` and ``email_broadcast_recipients`` live in the LogStore,
        not this contract — the caller must purge them separately via
        ``LogStore.hard_delete_user_data``.

        Returns a ``{table_name: row_count}`` mapping for inclusion in the
        audit details.  Implementations that cannot determine row counts may
        return ``{}``.
        """

    @abstractmethod
    async def list_users(
        self,
        *,
        status: str | None = None,
        search: str | None = None,
        sort_by: Literal[
            "created",
            "cost_today",
            "cost_month",
            "cost_alltime",
            "last_login",
            "requests",
            "tokens",
        ] = "created",
        limit: int = 100,
        offset: int = 0,
        # Phase 1 admin Users redesign — additional filters
        min_cost_today: Decimal | None = None,
        min_cost_month: Decimal | None = None,
        quota_state: Literal["near", "over", "custom", "default"] | None = None,
        provider: str | None = None,
        active_within_hours: int | None = None,
        anomaly: bool | None = None,
    ) -> tuple[int, list[Row], Row]:
        """Return ``(total_count, user_rows, status_counts_row)``.

        *sort_by* values that reference cost (``cost_today``, ``cost_month``,
        ``cost_alltime``) or usage (``tokens``) require joining against
        ``api_logs`` which lives in the **LogStore**.  ``requests`` reads the
        ``user_daily_cost`` rollup.  Implementations that cannot access
        ``api_logs`` directly must accept an optional *usage_provider* callback
        or return those columns as zero and let the caller enrich them.

        ``status_counts_row`` is a dict with keys ``all``, ``pending_approval``,
        ``active``, ``suspended``, ``rejected``, ``deleted``.

        Additional kw-only filters (all default to ``None`` — no-op):
        - ``min_cost_today`` / ``min_cost_month``: filter to users whose
          today/month spend meets the threshold (USD).
        - ``quota_state``: ``"default"``/``"custom"`` filter on the user's
          active key quota; ``"near"``/``"over"`` compare today's spend
          against quota.
        - ``provider``: keep only users who hit ``provider`` in api_logs in
          the last 30 days.
        - ``active_within_hours``: ``last_login_at`` within the window.
        - ``anomaly``: when ``True``, keep only users whose today's spend is
          anomalously high vs. their prior 7-day average. Uses the same rule
          as ``get_users_summary``: today >= $1, days_with_history >= 3,
          today >= 5x prior_7d_avg. Reads ``user_daily_cost``.
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

    async def invalidate_auth_caches(self) -> None:  # noqa: B027
        """Evict all cached auth-context entries.

        The base implementation is a no-op — uncached stores have nothing
        to evict.  ``CachedOperationalStore`` overrides this to clear the
        in-memory (or Redis) auth cache so that key revocations take effect
        immediately without waiting for TTL expiry.
        """

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
        """Fetch the active key by account_id OR user_id.

        Matches the real user_id uniqueness constraint, so it also finds legacy
        keys whose account_id is NULL. Preferred for create/regenerate
        pre-checks.
        """

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

    # -- login events (audit) ------------------------------------------------

    @abstractmethod
    async def record_login_event(
        self,
        *,
        email: str,
        outcome: str,
        failure_reason: str | None,
        user_id: str | None,
        ip: str | None,
        user_agent: str | None,
    ) -> None:
        """Insert one row into ``login_events``.

        Best-effort for callers — callers may catch + log on exception so
        audit failures don't break login. ``outcome`` must be one of
        ``'success'`` | ``'failure'``.
        """

    @abstractmethod
    async def purge_login_events_older_than(self, days: int) -> int:
        """Delete ``login_events`` rows older than ``days``.

        Returns the deleted row count.
        """

    @abstractmethod
    async def purge_login_events_for_user(self, user_id: str) -> int:
        """Delete all ``login_events`` rows for ``user_id``.

        Returns the deleted row count.
        """

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
    async def query_users_over_daily_threshold(
        self,
        thresholds: dict[str, float],
    ) -> list[tuple[str, str, float]]:
        """Return users whose UTC daily cost is above their role threshold.

        Returns ``(user_id, role, daily_cost)`` tuples ordered by highest
        daily cost first. Reads from the ``user_daily_cost`` counter table.
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

    # -- signup domain allowlist --------------------------------------------

    @abstractmethod
    async def list_signup_allowed_domains(self) -> list[Row]:
        """Return all rows in ``signup_allowed_domains`` ordered by created_at DESC.

        Each row contains: ``domain``, ``is_wildcard``, ``created_at``,
        ``created_by`` (user id), and ``created_by_email`` (joined from users
        table; may be None when the creator was deleted or unknown).
        """

    @abstractmethod
    async def add_signup_allowed_domain(
        self,
        *,
        domain: str,
        is_wildcard: bool,
        created_by: str | None,
    ) -> Row:
        """Insert a new allowlist entry. Returns the inserted row.

        Implementations must raise an exception on duplicate composite key
        ``(domain, is_wildcard)``.
        """

    @abstractmethod
    async def remove_signup_allowed_domain(
        self,
        *,
        domain: str,
        is_wildcard: bool,
    ) -> bool:
        """Delete an allowlist entry. Returns True if a row was removed."""

    @abstractmethod
    async def signup_allowlist_is_empty(self) -> bool:
        """Return True when ``signup_allowed_domains`` has no rows.

        Hot-path read used during signup; callers should layer their own
        TTL cache for performance.
        """

    @abstractmethod
    async def is_signup_domain_allowed(self, email: str) -> bool:
        """Return True if *email*'s domain is on the signup allowlist.

        Match rules:
        - Exact match against rows where ``is_wildcard=FALSE``.
        - Suffix match against rows where ``is_wildcard=TRUE``: walks parent
          labels of the email's domain. ``*.example.com`` matches
          ``a.example.com`` and ``a.b.example.com`` but **not** the bare
          suffix ``example.com``.

        Returns False on malformed email (no ``@`` or empty domain) and on
        an empty allowlist (callers handle "empty allowlist = allow").
        """

    # -- site settings (runtime feature flags) --------------------------------

    @abstractmethod
    async def get_setting(self, key: str) -> Row | None:
        """Fetch a single site_settings row by key.

        Returns columns: key, value, value_type, updated_at, updated_by.
        """

    @abstractmethod
    async def set_setting(
        self, key: str, value: str, value_type: str, updated_by: str | None
    ) -> None:
        """Upsert a site_settings row."""

    @abstractmethod
    async def delete_setting(self, key: str) -> bool:
        """Delete a site_settings row by key. Returns True when removed."""

    @abstractmethod
    async def list_settings(self) -> list[Row]:
        """Return all site_settings rows."""

    @abstractmethod
    async def get_model_visibility_override(self, model_id: str) -> Row | None:
        """Fetch a single model visibility override row by model_id."""

    @abstractmethod
    async def set_model_visibility_override(
        self,
        model_id: str,
        required_role: str,
        updated_by: str | None,
    ) -> None:
        """Upsert a model visibility override row."""

    @abstractmethod
    async def delete_model_visibility_override(self, model_id: str) -> bool:
        """Delete a model visibility override row. Returns True if removed."""

    @abstractmethod
    async def list_model_visibility_overrides(self) -> list[Row]:
        """Return all model visibility override rows ordered by model_id."""

    @abstractmethod
    async def get_model_concurrency_exemption(self, model_id: str) -> Row | None:
        """Fetch a single model concurrency exemption row by model_id."""

    @abstractmethod
    async def set_model_concurrency_exemption(
        self,
        model_id: str,
        updated_by: str | None,
    ) -> None:
        """Upsert a model concurrency exemption row (presence of row = exempt)."""

    @abstractmethod
    async def delete_model_concurrency_exemption(self, model_id: str) -> bool:
        """Delete a model concurrency exemption row. Returns True if removed."""

    @abstractmethod
    async def list_model_concurrency_exemptions(self) -> list[Row]:
        """Return all model concurrency exemption rows ordered by model_id."""

    @abstractmethod
    async def list_weight_overrides_for_model(self, model_id: str) -> list[Row]:
        """Return provider weight overrides for a model ordered by endpoint_id."""

    @abstractmethod
    async def list_all_weight_overrides(self) -> list[Row]:
        """Return all provider weight override rows ordered by model_id and endpoint_id."""

    @abstractmethod
    async def upsert_weight_override(
        self,
        model_id: str,
        endpoint_id: str,
        weight: float,
        updated_by: str | None,
    ) -> None:
        """Upsert a provider weight override row."""

    @abstractmethod
    async def delete_weight_override(self, model_id: str, endpoint_id: str) -> bool:
        """Delete a provider weight override row. Returns True if removed."""

    @abstractmethod
    async def list_provider_route_configs_for_model(self, model_id: str) -> list[Row]:
        """Return provider route override rows for one model ordered by route_id."""

    @abstractmethod
    async def list_all_provider_route_configs(self) -> list[Row]:
        """Return all provider route override rows ordered by model_id and route_id."""

    @abstractmethod
    async def upsert_provider_route_config(
        self,
        model_id: str,
        route_id: str,
        provider: str,
        openrouter_sort: str | None,
        base_url: str,
        api_key_id: str | None,
        provider_model_id: str,
        quota_limit: int | None,
        updated_by: str | None,
    ) -> None:
        """Upsert a runtime provider route override row."""

    @abstractmethod
    async def delete_provider_route_config(self, model_id: str, route_id: str) -> bool:
        """Delete a provider route override row. Returns True if removed."""

    @abstractmethod
    async def list_provider_route_candidates_for_model(self, model_id: str) -> list[Row]:
        """Return DB-backed runtime provider route candidates for one model."""

    @abstractmethod
    async def list_all_provider_route_candidates(self) -> list[Row]:
        """Return all DB-backed runtime provider route candidates."""

    @abstractmethod
    async def upsert_provider_route_candidate(
        self,
        model_id: str,
        route_id: str,
        route_type: str,
        provider: str,
        openrouter_sort: str | None,
        base_url: str,
        api_key_id: str | None,
        provider_model_id: str,
        quota_limit: int | None,
        concurrency_limit: int | None,
        weight: float,
        pricing: dict[str, str] | None,
        updated_by: str | None,
    ) -> None:
        """Upsert a DB-backed runtime provider route candidate."""

    @abstractmethod
    async def delete_provider_route_candidate(self, model_id: str, route_id: str) -> bool:
        """Delete a runtime provider route candidate row. Returns True if removed."""

    @abstractmethod
    async def delete_provider_route_candidate_with_config(
        self,
        model_id: str,
        route_id: str,
    ) -> bool:
        """Atomically delete a route candidate and any matching override row."""

    # -- routewise probes ----------------------------------------------------

    @abstractmethod
    async def insert_routewise_probe_sample(
        self,
        *,
        model_id: str,
        endpoint_id: str,
        ttft_ms: float | None,
        ok: bool,
        error: str | None,
        cost_usd: float | None,
        checked_at: datetime | None = None,
    ) -> int | None:
        """Persist one active RouteWise latency probe outcome and return its row id."""

    @abstractmethod
    async def list_routewise_probe_samples(
        self,
        *,
        model_id: str | None = None,
        endpoint_id: str | None = None,
        since: datetime | None = None,
        after_id: int | None = None,
        newest_first: bool = False,
        limit: int = 1000,
    ) -> list[Row]:
        """Return RouteWise probe samples, oldest-first by id unless newest_first is set."""

    @abstractmethod
    async def try_acquire_routewise_probe_lease(
        self,
        *,
        lease_key: str,
        holder_id: str,
        ttl_sec: float,
    ) -> bool:
        """Acquire or renew the active RouteWise probe lease for a router scope."""

    # -- role quota ----------------------------------------------------------

    @abstractmethod
    async def count_active_keys_for_role(self, role: str) -> tuple[int, int]:
        """Return ``(key_count, user_count)`` of active api_keys whose owner has this role.

        Used by the admin "apply role quota" preview.
        """

    @abstractmethod
    async def apply_role_quota(self, role: str, quota: Decimal) -> int:
        """Set ``quota_daily_cost_usd`` on every active api_key for this role.

        Returns the number of rows updated. Atomic: a failure rolls back.
        Overwrites any per-key custom override.
        """

    # -- provider api keys ---------------------------------------------------

    @abstractmethod
    async def add_provider_key(
        self,
        *,
        provider: str,
        api_key: str,
        label: str | None,
        created_by: str | None,
        key_id: str | None = None,
    ) -> str:
        """Insert a new upstream provider API key row.

        When ``key_id`` is supplied the caller-provided UUID is used instead
        of generating a new one. Returns the row id.
        """

    @abstractmethod
    async def get_provider_key_full(self, key_id: str) -> tuple[str, str] | None:
        """Return ``(provider, raw_key)`` for the row, or None if absent.

        Used by the admin delete endpoint to identify the raw key that was
        just removed without scanning every key for the provider.
        """

    @abstractmethod
    async def list_provider_keys(self, provider: str | None = None) -> list[ProviderKeyRow]:
        """Return masked rows for provider keys (both active and disabled).

        Filters by ``provider`` when supplied. The row ``status`` distinguishes
        active from disabled keys so the admin UI can render an enable/disable
        toggle. Raw secret material is never returned — see
        ``list_provider_keys_full`` for the boot-time loader.
        """

    @abstractmethod
    async def list_provider_keys_full(
        self,
        provider: str,
        *,
        exclude_ids: set[str] | None = None,
    ) -> list[str]:
        """Return raw active API keys for ``provider`` (boot-time only).

        Disabled keys are excluded so a disabled key is never re-seeded into a
        live pool at boot. ``exclude_ids`` omits DB rows that are bound to
        explicit provider-route configs/candidates, so route-scoped keys are not
        injected into a provider's global pool during boot.
        """

    @abstractmethod
    async def set_provider_key_status(self, key_id: str, status: str) -> bool:
        """Set a provider key row's ``status`` (``"active"``/``"disabled"``).

        Returns True when a row was updated. Used by the admin enable/disable
        toggle; the caller syncs the live key pools separately.
        """

    @abstractmethod
    async def delete_provider_key(self, key_id: str) -> bool:
        """Hard-delete the provider key row. Returns True if a row was removed."""

    @abstractmethod
    async def disable_provider_env_key(
        self,
        *,
        provider: str,
        key_hash: str,
        key_prefix: str,
        disabled_by: str | None,
    ) -> None:
        """Persist a tombstone for an env-sourced provider API key."""

    @abstractmethod
    async def list_disabled_provider_env_key_hashes(self, provider: str) -> set[str]:
        """Return disabled env-sourced provider key hashes for ``provider``."""

    @abstractmethod
    async def list_disabled_provider_env_keys(self, provider: str) -> list[tuple[str, str]]:
        """Return ``(key_hash, key_prefix)`` for disabled env keys of ``provider``.

        Unlike ``list_disabled_provider_env_key_hashes`` (boot-time hash set),
        this carries the masked prefix so the admin UI can display a disabled
        env key and offer to re-enable it.
        """

    @abstractmethod
    async def enable_provider_env_key(self, provider: str, key_hash: str) -> bool:
        """Remove an env-key tombstone so the key is used again.

        Returns True when a tombstone row was deleted.
        """


# ---------------------------------------------------------------------------
# LogStore — api_logs, api_stats_hourly
# ---------------------------------------------------------------------------


class LogStore(ABC):
    """Abstract interface for request logging and usage analytics.

    Implementations:
    - ``PostgresLogStore``: full rows in PostgreSQL (prompt/response content).
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
        usage: dict[str, Any] | None,
        latency_ms: int,
        status_code: int,
        error: str | None = None,
        params: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        ttft_ms: int | None = None,
        store_full_content: bool | None = None,
        pricing: dict[str, str] | None = None,
        upstream_cost_usd: float | None = None,
        request_payload: dict[str, Any] | None = None,
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
        ``last_request_at``, ``avg_turns``, ``avg_user_turns``. The two
        averages are the all-time mean message / user-message count across the
        user's chat-style requests (``None`` when they have none).
        """

    @abstractmethod
    async def get_bulk_user_turn_averages(
        self, user_ids: list[str]
    ) -> dict[str, dict[str, float | None]]:
        """Return per-user all-time average turn counts for a batch of users.

        Maps ``user_id`` → ``{"avg_turns", "avg_user_turns"}`` (each float or
        None). Users with no chat-style requests are omitted. Used by the admin
        list endpoint to avoid N+1 queries.
        """

    @abstractmethod
    async def get_user_automation_score(
        self, user_id: str, *, days: int = 30
    ) -> dict[str, Any] | None:
        """Return one user's human-vs-script automation score, or None with no traffic.

        See :mod:`serving.analytics.automation_score`: the record's ``score`` in
        ``[0, 1]`` is HIGH (→1) for script/batch/cron-driven ``api_logs`` over the
        trailing ``days`` and LOW (→0) for interactive-human usage.
        """

    @abstractmethod
    async def get_bulk_user_automation_scores(
        self, user_ids: list[str], *, days: int = 30
    ) -> dict[str, dict[str, Any]]:
        """Return ``{user_id: automation-score record}`` for a batch of users.

        Scores every requested user over the trailing ``days`` window in one
        round-trip; users with no traffic in the window are omitted. Used by the
        admin Users tab to score a page on demand.
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
    async def get_routewise_bootstrap_rows(
        self,
        *,
        model_ids: list[str],
        since: datetime,
        limit: int | None = None,
    ) -> list[Row]:
        """Fetch recent request rows for RouteWise startup bootstrap.

        Rows must be returned in ascending timestamp order so RouteWise's
        in-memory rolling windows can replay them oldest-to-newest.  When
        ``limit`` is provided, implementations should return the most recent
        ``limit`` rows from the window, still ordered ascending for replay.
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

    # -- admin: bulk delete --------------------------------------------------

    @abstractmethod
    async def delete_recent_error_requests(self, *, hours: int = 1) -> int:
        """Hard-delete error requests logged within the last *hours* hours.

        An "error" row matches the same predicate the admin Recent Requests
        "errors only" filter uses: ``error IS NOT NULL`` OR a status code that
        is missing or outside the 2xx/3xx success range. Returns the number of
        deleted rows.
        """

    # -- admin: hard-delete user-owned rows ---------------------------------

    @abstractmethod
    async def hard_delete_user_data(self, user_id: str) -> dict[str, int]:
        """Permanently delete LogStore-owned rows for a user.

        Wipes ``api_logs`` and ``email_broadcast_recipients`` rows referencing
        *user_id*.  Returns ``{table_name: row_count}`` (implementations that
        cannot return per-statement counts may return ``{}``).

        Called by the admin hard-delete endpoint AFTER the OperationalStore
        wipe completes.  Cross-pool failure semantics are documented at the
        endpoint.
        """
