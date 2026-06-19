"""Pluggable caching layer for OperationalStore.

CachedOperationalStore wraps any OperationalStore and caches hot-path reads
behind a CacheBackend interface. Write operations invalidate affected cache
entries immediately (write-through) so revocations and status changes take
effect within one request rather than waiting for TTL expiry.

The default InMemoryCache is a simple dict-based TTL cache suitable for
single-instance deployments. Swap in a Redis backend later by implementing
CacheBackend.
"""

from __future__ import annotations

import fnmatch
import time
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from .base import OperationalStore, ProviderKeyRow, Row

if TYPE_CHECKING:
    from datetime import datetime
    from decimal import Decimal
    from typing import Literal


# ---------------------------------------------------------------------------
# Cache backend interface
# ---------------------------------------------------------------------------


class CacheBackend(ABC):
    """Minimal async cache interface. Implementations must be concurrency-safe."""

    @abstractmethod
    async def get(self, key: str) -> Any | None:
        """Return cached value or None on miss."""

    @abstractmethod
    async def set(self, key: str, value: Any, ttl: int) -> None:
        """Store *value* under *key* with a TTL in seconds."""

    @abstractmethod
    async def delete(self, key: str) -> None:
        """Remove a single key (no-op if missing)."""

    @abstractmethod
    async def delete_pattern(self, pattern: str) -> None:
        """Remove all keys matching a glob *pattern* (e.g. ``user:*``)."""


# ---------------------------------------------------------------------------
# In-memory TTL cache
# ---------------------------------------------------------------------------


class InMemoryCache(CacheBackend):
    """Dict-based TTL cache. Per-process, suitable for single-instance VPS."""

    def __init__(self) -> None:
        self._store: dict[str, tuple[Any, float]] = {}  # key → (value, expires_at)

    async def get(self, key: str) -> Any | None:
        """Return cached value or None on miss / expiry."""
        entry = self._store.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if time.monotonic() > expires_at:
            del self._store[key]
            return None
        return value

    async def set(self, key: str, value: Any, ttl: int) -> None:
        """Store *value* with monotonic-clock expiry."""
        self._store[key] = (value, time.monotonic() + ttl)

    async def delete(self, key: str) -> None:
        """Remove a single key."""
        self._store.pop(key, None)

    async def delete_pattern(self, pattern: str) -> None:
        """Remove all keys matching a glob *pattern*."""
        to_delete = [k for k in self._store if fnmatch.fnmatch(k, pattern)]
        for k in to_delete:
            del self._store[k]


# ---------------------------------------------------------------------------
# Cached wrapper
# ---------------------------------------------------------------------------

# TTLs (seconds)
_AUTH_CONTEXT_TTL = 30
_USER_TTL = 60
_HEALTH_TTL = 5


class CachedOperationalStore(OperationalStore):
    """Transparent caching proxy over any OperationalStore.

    Cached reads:
        - get_auth_context_by_key_hash  (30 s)
        - get_auth_context_lightweight  (30 s)
        - get_user_by_id                (60 s)
        - health_check                  ( 5 s)

    Write-through invalidation:
        - revoke_key / regenerate_key / update_key  → invalidate key caches
        - delete_user / update_user_fields          → invalidate user cache
        - approve_user / reject_user                → invalidate user cache

    Everything else passes straight through.
    """

    def __init__(self, store: OperationalStore, cache: CacheBackend) -> None:
        self._store = store
        self._cache = cache

    # -- cache key helpers ---------------------------------------------------

    @staticmethod
    def _auth_key(key_hash: str) -> str:
        return f"auth:{key_hash}"

    @staticmethod
    def _auth_light_key(key_hash: str) -> str:
        return f"auth_light:{key_hash}"

    @staticmethod
    def _user_key(user_id: str) -> str:
        return f"user:{user_id}"

    @staticmethod
    def _health_key() -> str:
        return "health"

    @property
    def pool(self) -> Any:
        """Expose underlying store's connection pool, if available (PostgreSQL only)."""
        return getattr(self._store, "_pool", None)

    # -- lifecycle (delegated, not cached) -----------------------------------

    async def initialize(self) -> None:
        """Delegate to wrapped store."""
        await self._store.initialize()

    async def cleanup(self) -> None:
        """Delegate to wrapped store."""
        await self._store.cleanup()

    # -- cached reads --------------------------------------------------------

    async def health_check(self) -> bool:
        """Return cached health status (5 s TTL)."""
        cached = await self._cache.get(self._health_key())
        if cached is not None:
            return cached
        result = await self._store.health_check()
        await self._cache.set(self._health_key(), result, _HEALTH_TTL)
        return result

    async def get_user_by_id(self, user_id: str) -> Row | None:
        """Return cached user row (60 s TTL)."""
        ck = self._user_key(user_id)
        cached = await self._cache.get(ck)
        if cached is not None:
            return cached
        result = await self._store.get_user_by_id(user_id)
        if result is not None:
            await self._cache.set(ck, result, _USER_TTL)
        return result

    async def get_auth_context_by_key_hash(self, key_hash: str) -> Row | None:
        """Return cached auth context (30 s TTL)."""
        ck = self._auth_key(key_hash)
        cached = await self._cache.get(ck)
        if cached is not None:
            return cached
        result = await self._store.get_auth_context_by_key_hash(key_hash)
        if result is not None:
            await self._cache.set(ck, result, _AUTH_CONTEXT_TTL)
        return result

    async def get_auth_context_lightweight(self, key_hash: str) -> Row | None:
        """Return cached lightweight auth context (30 s TTL)."""
        ck = self._auth_light_key(key_hash)
        cached = await self._cache.get(ck)
        if cached is not None:
            return cached
        result = await self._store.get_auth_context_lightweight(key_hash)
        if result is not None:
            await self._cache.set(ck, result, _AUTH_CONTEXT_TTL)
        return result

    # -- user writes (invalidate user cache) ---------------------------------

    async def update_user_fields(self, user_id: str, **fields: Any) -> None:
        """Delegate then invalidate user cache (and auth caches if role/status changed)."""
        await self._store.update_user_fields(user_id, **fields)
        await self._cache.delete(self._user_key(user_id))
        if fields.keys() & {"role", "status"}:
            await self._cache.delete_pattern("auth:*")
            await self._cache.delete_pattern("auth_light:*")

    async def update_user_last_login(self, user_id: str) -> None:
        """Delegate then invalidate user cache."""
        await self._store.update_user_last_login(user_id)
        await self._cache.delete(self._user_key(user_id))

    async def delete_user(
        self,
        user_id: str,
        *,
        admin_ip: str,
        admin_id: str,
        reason: str | None = None,
        email: str | None = None,
    ) -> None:
        """Delegate then invalidate user + auth caches."""
        await self._store.delete_user(
            user_id, admin_ip=admin_ip, admin_id=admin_id, reason=reason, email=email
        )
        await self._cache.delete(self._user_key(user_id))
        # Also purge any auth entries referencing this user's keys
        await self._cache.delete_pattern("auth:*")
        await self._cache.delete_pattern("auth_light:*")

    async def resume_user(
        self,
        user_id: str,
        *,
        admin_ip: str,
        admin_id: str,
        reason: str | None = None,
        email: str | None = None,
    ) -> None:
        """Delegate then invalidate user + auth caches."""
        await self._store.resume_user(
            user_id, admin_ip=admin_ip, admin_id=admin_id, reason=reason, email=email
        )
        await self._cache.delete(self._user_key(user_id))
        await self._cache.delete_pattern("auth:*")
        await self._cache.delete_pattern("auth_light:*")

    async def hard_delete_user(
        self,
        user_id: str,
        *,
        admin_ip: str,
        admin_id: str,
        reason: str | None = None,
        email: str | None = None,
    ) -> dict[str, int]:
        """Delegate then invalidate user + auth caches."""
        counts = await self._store.hard_delete_user(
            user_id, admin_ip=admin_ip, admin_id=admin_id, reason=reason, email=email
        )
        await self._cache.delete(self._user_key(user_id))
        await self._cache.delete_pattern("auth:*")
        await self._cache.delete_pattern("auth_light:*")
        return counts

    async def approve_user(self, user_id: str, *, admin_id: str, note: str | None = None) -> None:
        """Delegate then invalidate user cache."""
        await self._store.approve_user(user_id, admin_id=admin_id, note=note)
        await self._cache.delete(self._user_key(user_id))

    async def reject_user(self, user_id: str, *, admin_id: str, reason: str) -> None:
        """Delegate then invalidate user cache."""
        await self._store.reject_user(user_id, admin_id=admin_id, reason=reason)
        await self._cache.delete(self._user_key(user_id))

    async def invalidate_auth_caches(self) -> None:
        """Evict all cached auth-context entries (cache-only, no DB write)."""
        await self._cache.delete_pattern("auth:*")
        await self._cache.delete_pattern("auth_light:*")

    # -- key writes (invalidate auth caches) ---------------------------------

    async def update_key(self, user_id: str, **fields: Any) -> None:
        """Delegate then invalidate auth caches."""
        await self._store.update_key(user_id, **fields)
        # We don't know which key_hash maps to this user, so clear all auth entries.
        await self._cache.delete_pattern("auth:*")
        await self._cache.delete_pattern("auth_light:*")

    async def revoke_key(self, user_id: str, *, hard_delete: bool = False) -> None:
        """Delegate then invalidate auth caches."""
        await self._store.revoke_key(user_id, hard_delete=hard_delete)
        await self._cache.delete_pattern("auth:*")
        await self._cache.delete_pattern("auth_light:*")

    async def regenerate_key(
        self,
        user_id: str,
        *,
        new_key_hash: str,
        new_key_prefix: str,
    ) -> str:
        """Delegate then invalidate auth caches."""
        old_prefix = await self._store.regenerate_key(
            user_id, new_key_hash=new_key_hash, new_key_prefix=new_key_prefix
        )
        await self._cache.delete_pattern("auth:*")
        await self._cache.delete_pattern("auth_light:*")
        return old_prefix

    # -- role quotas (invalidate auth caches on bulk write) ------------------

    async def count_active_keys_for_role(self, role: str) -> tuple[int, int]:
        """Delegate to wrapped store."""
        return await self._store.count_active_keys_for_role(role)

    async def apply_role_quota(self, role: str, quota: Decimal) -> int:
        """Delegate then invalidate auth caches (quota_daily_cost_usd changed for all role rows)."""
        n = await self._store.apply_role_quota(role, quota)
        # Bulk write touches quota_daily_cost_usd on all keys for a role; clear
        # all per-key auth cache entries to prevent stale quota lookups.
        await self._cache.delete_pattern("auth:*")
        await self._cache.delete_pattern("auth_light:*")
        return n

    # -- pure pass-through (no caching, no invalidation) ---------------------

    async def get_user_by_email(self, email: str) -> Row | None:
        """Delegate to wrapped store."""
        return await self._store.get_user_by_email(email)

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
        """Delegate to wrapped store."""
        return await self._store.create_user(
            user_id=user_id,
            email=email,
            password_hash=password_hash,
            user_name=user_name,
            email_verified=email_verified,
            status=status,
            signup_reason=signup_reason,
        )

    async def get_user_counts_by_status(self) -> dict[str, int]:
        """Delegate to wrapped store."""
        return await self._store.get_user_counts_by_status()

    async def get_active_user_counts(self) -> dict[str, int]:
        """Delegate to wrapped store."""
        return await self._store.get_active_user_counts()

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
        min_cost_today: Decimal | None = None,
        min_cost_month: Decimal | None = None,
        quota_state: Literal["near", "over", "custom", "default"] | None = None,
        provider: str | None = None,
        active_within_hours: int | None = None,
        anomaly: bool | None = None,
    ) -> tuple[int, list[Row], Row]:
        """Delegate to wrapped store."""
        return await self._store.list_users(
            status=status,
            search=search,
            sort_by=sort_by,
            limit=limit,
            offset=offset,
            min_cost_today=min_cost_today,
            min_cost_month=min_cost_month,
            quota_state=quota_state,
            provider=provider,
            active_within_hours=active_within_hours,
            anomaly=anomaly,
        )

    async def update_key_last_used(self, key_id: int) -> None:
        """Delegate to wrapped store (fire-and-forget, not worth caching)."""
        await self._store.update_key_last_used(key_id)

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
        """Delegate to wrapped store."""
        return await self._store.create_key(
            key_hash=key_hash,
            key_prefix=key_prefix,
            user_id=user_id,
            user_name=user_name,
            quota_daily_cost_usd=quota_daily_cost_usd,
            quota_monthly_cost_usd=quota_monthly_cost_usd,
            expires_at=expires_at,
            notes=notes,
            metadata=metadata,
            account_id=account_id,
        )

    async def check_active_key_exists(self, user_id: str) -> bool:
        """Delegate to wrapped store."""
        return await self._store.check_active_key_exists(user_id)

    async def list_keys(
        self,
        *,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[int, list[Row]]:
        """Delegate to wrapped store."""
        return await self._store.list_keys(status=status, limit=limit, offset=offset)

    async def get_key_detail(self, user_id: str) -> Row | None:
        """Delegate to wrapped store."""
        return await self._store.get_key_detail(user_id)

    async def get_key_by_account_or_user(self, account_id: str) -> Row | None:
        """Delegate to wrapped store."""
        return await self._store.get_key_by_account_or_user(account_id)

    async def get_active_key_by_account(self, account_id: str) -> Row | None:
        """Delegate to wrapped store."""
        return await self._store.get_active_key_by_account(account_id)

    # -- sessions (pass-through) ---------------------------------------------

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
        """Delegate to wrapped store."""
        return await self._store.create_session(
            session_id=session_id,
            user_id=user_id,
            refresh_token_hash=refresh_token_hash,
            jti=jti,
            sid=sid,
            expires_at=expires_at,
        )

    async def get_session_by_token_hash(self, token_hash: str) -> Row | None:
        """Delegate to wrapped store."""
        return await self._store.get_session_by_token_hash(token_hash)

    async def rotate_session(
        self, session_id: str, *, new_refresh_token_hash: str, new_jti: str
    ) -> None:
        """Delegate to wrapped store."""
        return await self._store.rotate_session(
            session_id, new_refresh_token_hash=new_refresh_token_hash, new_jti=new_jti
        )

    async def revoke_session(self, session_id: str) -> None:
        """Delegate to wrapped store."""
        return await self._store.revoke_session(session_id)

    async def delete_user_sessions(self, user_id: str) -> None:
        """Delegate to wrapped store."""
        return await self._store.delete_user_sessions(user_id)

    # -- login events (pass-through) -----------------------------------------

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
        """Delegate to wrapped store."""
        await self._store.record_login_event(
            email=email,
            outcome=outcome,
            failure_reason=failure_reason,
            user_id=user_id,
            ip=ip,
            user_agent=user_agent,
        )

    async def purge_login_events_older_than(self, days: int) -> int:
        """Delegate to wrapped store."""
        return await self._store.purge_login_events_older_than(days)

    async def purge_login_events_for_user(self, user_id: str) -> int:
        """Delegate to wrapped store."""
        return await self._store.purge_login_events_for_user(user_id)

    # -- tokens (pass-through) -----------------------------------------------

    async def create_verification_token(
        self, *, token: str, user_id: str, expires_at: datetime
    ) -> None:
        """Delegate to wrapped store."""
        return await self._store.create_verification_token(
            token=token, user_id=user_id, expires_at=expires_at
        )

    async def get_verification_token(self, token: str) -> Row | None:
        """Delegate to wrapped store."""
        return await self._store.get_verification_token(token)

    async def mark_verification_used(self, token: str) -> None:
        """Delegate to wrapped store."""
        return await self._store.mark_verification_used(token)

    async def mark_user_email_verified(self, user_id: str) -> None:
        """Delegate then invalidate user + auth caches so verification is visible immediately."""
        await self._store.mark_user_email_verified(user_id)
        await self._cache.delete(self._user_key(user_id))
        # email_verified is carried in the cached auth contexts too, but we don't
        # know which key_hash maps to this user, so clear all auth entries.
        await self._cache.delete_pattern("auth:*")
        await self._cache.delete_pattern("auth_light:*")

    async def delete_user_verification_tokens(self, user_id: str) -> None:
        """Delegate to wrapped store."""
        return await self._store.delete_user_verification_tokens(user_id)

    async def create_reset_token(self, *, token: str, user_id: str, expires_at: datetime) -> None:
        """Delegate to wrapped store."""
        return await self._store.create_reset_token(
            token=token, user_id=user_id, expires_at=expires_at
        )

    async def get_reset_token(self, token: str) -> Row | None:
        """Delegate to wrapped store."""
        return await self._store.get_reset_token(token)

    async def mark_reset_used(self, token: str) -> None:
        """Delegate to wrapped store."""
        return await self._store.mark_reset_used(token)

    async def delete_user_reset_tokens(self, user_id: str) -> None:
        """Delegate to wrapped store."""
        return await self._store.delete_user_reset_tokens(user_id)

    # -- audit (pass-through) ------------------------------------------------

    async def log_admin_action(
        self,
        *,
        admin_ip: str,
        action: str,
        target_user_id: str | None = None,
        details: dict[str, Any] | None = None,
        success: bool = True,
    ) -> None:
        """Delegate to wrapped store."""
        return await self._store.log_admin_action(
            admin_ip=admin_ip,
            action=action,
            target_user_id=target_user_id,
            details=details,
            success=success,
        )

    async def list_audit_log(
        self,
        *,
        action: str | None = None,
        target_user_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[int, list[Row]]:
        """Delegate to wrapped store."""
        return await self._store.list_audit_log(
            action=action,
            target_user_id=target_user_id,
            limit=limit,
            offset=offset,
        )

    # -- preferences (pass-through) ------------------------------------------

    async def get_user_preferences(self, user_id: str) -> dict[str, Any]:
        """Delegate to wrapped store."""
        return await self._store.get_user_preferences(user_id)

    async def update_user_preferences(self, user_id: str, preferences: dict[str, Any]) -> None:
        """Delegate then invalidate user and auth caches."""
        await self._store.update_user_preferences(user_id, preferences)
        await self._cache.delete(self._user_key(user_id))
        await self._cache.delete_pattern("auth:*")
        await self._cache.delete_pattern("auth_light:*")

    # -- signup domain allowlist (pass-through) ------------------------------

    async def list_signup_allowed_domains(self) -> list[Row]:
        """Delegate to wrapped store."""
        return await self._store.list_signup_allowed_domains()

    async def add_signup_allowed_domain(
        self,
        *,
        domain: str,
        is_wildcard: bool,
        created_by: str | None,
    ) -> Row:
        """Delegate to wrapped store."""
        return await self._store.add_signup_allowed_domain(
            domain=domain, is_wildcard=is_wildcard, created_by=created_by
        )

    async def remove_signup_allowed_domain(self, *, domain: str, is_wildcard: bool) -> bool:
        """Delegate to wrapped store."""
        return await self._store.remove_signup_allowed_domain(
            domain=domain, is_wildcard=is_wildcard
        )

    async def signup_allowlist_is_empty(self) -> bool:
        """Delegate to wrapped store."""
        return await self._store.signup_allowlist_is_empty()

    async def is_signup_domain_allowed(self, email: str) -> bool:
        """Delegate to wrapped store."""
        return await self._store.is_signup_domain_allowed(email)

    # -- site settings (pass-through) -----------------------------------------

    async def get_setting(self, key: str) -> Row | None:
        """Delegate to wrapped store."""
        return await self._store.get_setting(key)

    async def set_setting(
        self, key: str, value: str, value_type: str, updated_by: str | None
    ) -> None:
        """Delegate to wrapped store."""
        await self._store.set_setting(key, value, value_type, updated_by)

    async def list_settings(self) -> list[Row]:
        """Delegate to wrapped store."""
        return await self._store.list_settings()

    async def get_model_visibility_override(self, model_id: str) -> Row | None:
        """Delegate to wrapped store."""
        return await self._store.get_model_visibility_override(model_id)

    async def set_model_visibility_override(
        self,
        model_id: str,
        required_role: str,
        updated_by: str | None,
    ) -> None:
        """Delegate to wrapped store."""
        await self._store.set_model_visibility_override(model_id, required_role, updated_by)

    async def delete_model_visibility_override(self, model_id: str) -> bool:
        """Delegate to wrapped store."""
        return await self._store.delete_model_visibility_override(model_id)

    async def list_model_visibility_overrides(self) -> list[Row]:
        """Delegate to wrapped store."""
        return await self._store.list_model_visibility_overrides()

    async def get_model_concurrency_exemption(self, model_id: str) -> Row | None:
        """Delegate to wrapped store."""
        return await self._store.get_model_concurrency_exemption(model_id)

    async def set_model_concurrency_exemption(
        self,
        model_id: str,
        updated_by: str | None,
    ) -> None:
        """Delegate to wrapped store."""
        await self._store.set_model_concurrency_exemption(model_id, updated_by)

    async def delete_model_concurrency_exemption(self, model_id: str) -> bool:
        """Delegate to wrapped store."""
        return await self._store.delete_model_concurrency_exemption(model_id)

    async def list_model_concurrency_exemptions(self) -> list[Row]:
        """Delegate to wrapped store."""
        return await self._store.list_model_concurrency_exemptions()

    async def list_weight_overrides_for_model(self, model_id: str) -> list[Row]:
        """Delegate to wrapped store."""
        return await self._store.list_weight_overrides_for_model(model_id)

    async def list_all_weight_overrides(self) -> list[Row]:
        """Delegate to wrapped store."""
        return await self._store.list_all_weight_overrides()

    async def upsert_weight_override(
        self,
        model_id: str,
        endpoint_id: str,
        weight: float,
        updated_by: str | None,
    ) -> None:
        """Delegate to wrapped store."""
        await self._store.upsert_weight_override(model_id, endpoint_id, weight, updated_by)

    async def delete_weight_override(self, model_id: str, endpoint_id: str) -> bool:
        """Delegate to wrapped store."""
        return await self._store.delete_weight_override(model_id, endpoint_id)

    # -- cost counters (pass-through) ----------------------------------------

    async def increment_user_cost(
        self, user_id: str, cost_usd: float, *, day: str | None = None
    ) -> None:
        """Delegate to wrapped store."""
        return await self._store.increment_user_cost(user_id, cost_usd, day=day)

    async def get_user_cost_today(self, user_id: str) -> float:
        """Delegate to wrapped store."""
        return await self._store.get_user_cost_today(user_id)

    async def get_user_cost_period(self, user_id: str, period: Literal["today", "month"]) -> float:
        """Delegate to wrapped store."""
        return await self._store.get_user_cost_period(user_id, period)

    async def query_users_over_daily_threshold(
        self,
        thresholds: dict[str, float],
    ) -> list[tuple[str, str, float]]:
        """Delegate to wrapped store."""
        return await self._store.query_users_over_daily_threshold(thresholds)

    async def get_batch_usage(
        self, user_ids: list[str], period: Literal["today", "month"]
    ) -> dict[str, float]:
        """Delegate to wrapped store."""
        return await self._store.get_batch_usage(user_ids, period)

    async def get_user_cost_history(self, user_id: str, days: int = 7) -> list[Row]:
        """Delegate to wrapped store."""
        return await self._store.get_user_cost_history(user_id, days=days)

    async def get_bulk_user_cost_history(
        self, user_ids: list[str], days: int = 7
    ) -> dict[str, list[Row]]:
        """Delegate to wrapped store."""
        return await self._store.get_bulk_user_cost_history(user_ids, days=days)

    async def get_users_summary(self, **kwargs: Any) -> Row:
        """Delegate to wrapped store (no caching — admin dashboard endpoint)."""
        return await self._store.get_users_summary(**kwargs)

    # -- provider api keys (pass-through) ------------------------------------

    async def add_provider_key(
        self,
        *,
        provider: str,
        api_key: str,
        label: str | None,
        created_by: str | None,
        key_id: str | None = None,
    ) -> str:
        """Delegate to wrapped store."""
        return await self._store.add_provider_key(
            provider=provider,
            api_key=api_key,
            label=label,
            created_by=created_by,
            key_id=key_id,
        )

    async def list_provider_keys(self, provider: str | None = None) -> list[ProviderKeyRow]:
        """Delegate to wrapped store."""
        return await self._store.list_provider_keys(provider)

    async def list_provider_keys_full(self, provider: str) -> list[str]:
        """Delegate to wrapped store."""
        return await self._store.list_provider_keys_full(provider)

    async def get_provider_key_full(self, key_id: str) -> tuple[str, str] | None:
        """Delegate to wrapped store."""
        return await self._store.get_provider_key_full(key_id)

    async def delete_provider_key(self, key_id: str) -> bool:
        """Delegate to wrapped store."""
        return await self._store.delete_provider_key(key_id)

    async def disable_provider_env_key(
        self,
        *,
        provider: str,
        key_hash: str,
        key_prefix: str,
        disabled_by: str | None,
    ) -> None:
        """Delegate to wrapped store."""
        await self._store.disable_provider_env_key(
            provider=provider,
            key_hash=key_hash,
            key_prefix=key_prefix,
            disabled_by=disabled_by,
        )

    async def list_disabled_provider_env_key_hashes(self, provider: str) -> set[str]:
        """Delegate to wrapped store."""
        return await self._store.list_disabled_provider_env_key_hashes(provider)
