"""Dual-write wrappers for OperationalStore and LogStore.

When DB_BACKEND=d1 and DB_DUAL_WRITE=1, these wrappers send every write to
both a *primary* store (Cloudflare D1) and a *shadow* store (PostgreSQL).
Reads are served exclusively from the primary.

Shadow writes are best-effort: failures are logged with structured context
but never propagate to the caller.  This keeps PostgreSQL warm as a rolling
fallback — you can flip back to DB_BACKEND=postgres without data loss — but
it is **not** a guaranteed byte-for-byte mirror.  There are no distributed
transactions; if a multi-step shadow write partially fails, the shadow may
diverge from the primary until the next successful write.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from .base import LogStore, OperationalStore, Row

if TYPE_CHECKING:
    from datetime import datetime
    from decimal import Decimal
    from typing import Literal

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _shadow_ctx(**ids: Any) -> str:
    """Format stable identifiers for structured shadow-failure logs."""
    parts = [f"{k}={v}" for k, v in ids.items() if v is not None]
    return ", ".join(parts) if parts else ""


# ---------------------------------------------------------------------------
# DualWriteOperationalStore
# ---------------------------------------------------------------------------


class DualWriteOperationalStore(OperationalStore):
    """Dual-write proxy: D1 primary + PostgreSQL shadow for operational data.

    All reads go to the primary.  All writes go to the primary first; on
    success the same write is replayed against the shadow.  Shadow failures
    are swallowed and logged.
    """

    def __init__(self, primary: OperationalStore, shadow: OperationalStore) -> None:
        self._primary = primary
        self._shadow = shadow
        self._shadow_healthy: bool = True

    # -- shadow helper -------------------------------------------------------

    async def _do_shadow(self, method_name: str, coro, /, **ctx_ids: Any) -> None:
        """Await *coro* (a shadow method call).  On failure, log and swallow."""
        try:
            await coro
            if not self._shadow_healthy:
                self._shadow_healthy = True
                logger.info("Shadow operational store recovered: %s", method_name)
        except Exception:
            self._shadow_healthy = False
            ctx = _shadow_ctx(**ctx_ids)
            logger.warning(
                "Shadow operational write failed: method=%s %s",
                method_name,
                ctx,
                exc_info=True,
            )

    # -- public property -----------------------------------------------------

    @property
    def shadow_healthy(self) -> bool:
        """Whether the last shadow write succeeded.  Exposed for health endpoints."""
        return self._shadow_healthy

    # -- lifecycle -----------------------------------------------------------

    async def initialize(self) -> None:
        """Initialize both primary and shadow stores."""
        await self._primary.initialize()
        try:
            await self._shadow.initialize()
        except Exception:
            self._shadow_healthy = False
            logger.warning("Shadow operational store failed to initialize", exc_info=True)

    async def cleanup(self) -> None:
        """Clean up both primary and shadow stores."""
        await self._primary.cleanup()
        try:
            await self._shadow.cleanup()
        except Exception:
            logger.warning("Shadow operational store failed to cleanup", exc_info=True)

    async def health_check(self) -> bool:
        """Return primary health.  Shadow health tracked via shadow_healthy property."""
        primary_ok = await self._primary.health_check()
        try:
            shadow_ok = await self._shadow.health_check()
            if not shadow_ok and self._shadow_healthy:
                self._shadow_healthy = False
                logger.warning("Shadow operational store health check returned False")
            elif shadow_ok and not self._shadow_healthy:
                self._shadow_healthy = True
        except Exception:
            self._shadow_healthy = False
            logger.warning("Shadow operational store health check failed", exc_info=True)
        return primary_ok

    # -- users: reads --------------------------------------------------------

    async def get_user_by_id(self, user_id: str) -> Row | None:
        """Delegate to primary."""
        return await self._primary.get_user_by_id(user_id)

    async def get_user_by_email(self, email: str) -> Row | None:
        """Delegate to primary."""
        return await self._primary.get_user_by_email(email)

    async def get_user_counts_by_status(self) -> dict[str, int]:
        """Delegate to primary."""
        return await self._primary.get_user_counts_by_status()

    async def get_active_user_counts(self) -> dict[str, int]:
        """Delegate to primary."""
        return await self._primary.get_active_user_counts()

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
        """Delegate to primary."""
        return await self._primary.list_users(
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

    # -- users: writes -------------------------------------------------------

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
        """Write to primary, then shadow."""
        await self._primary.create_user(
            user_id=user_id,
            email=email,
            password_hash=password_hash,
            user_name=user_name,
            email_verified=email_verified,
            status=status,
        )
        await self._do_shadow(
            "create_user",
            self._shadow.create_user(
                user_id=user_id,
                email=email,
                password_hash=password_hash,
                user_name=user_name,
                email_verified=email_verified,
                status=status,
            ),
            user_id=user_id,
        )

    async def update_user_fields(self, user_id: str, **fields: Any) -> None:
        """Write to primary, then shadow."""
        await self._primary.update_user_fields(user_id, **fields)
        await self._do_shadow(
            "update_user_fields",
            self._shadow.update_user_fields(user_id, **fields),
            user_id=user_id,
        )

    async def update_user_last_login(self, user_id: str) -> None:
        """Write to primary, then shadow."""
        await self._primary.update_user_last_login(user_id)
        await self._do_shadow(
            "update_user_last_login",
            self._shadow.update_user_last_login(user_id),
            user_id=user_id,
        )

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
        """Write to primary, then shadow."""
        kwargs = {
            "email": email,
            "outcome": outcome,
            "failure_reason": failure_reason,
            "user_id": user_id,
            "ip": ip,
            "user_agent": user_agent,
        }
        await self._primary.record_login_event(**kwargs)
        await self._do_shadow(
            "record_login_event",
            self._shadow.record_login_event(**kwargs),
            user_id=user_id,
        )

    async def purge_login_events_older_than(self, days: int) -> int:
        """Run on primary; mirror on shadow. Returns primary's count."""
        deleted = await self._primary.purge_login_events_older_than(days)
        await self._do_shadow(
            "purge_login_events_older_than",
            self._shadow.purge_login_events_older_than(days),
        )
        return deleted

    async def purge_login_events_for_user(self, user_id: str) -> int:
        """Run on primary; mirror on shadow. Returns primary's count."""
        deleted = await self._primary.purge_login_events_for_user(user_id)
        await self._do_shadow(
            "purge_login_events_for_user",
            self._shadow.purge_login_events_for_user(user_id),
            user_id=user_id,
        )
        return deleted

    async def delete_user(
        self,
        user_id: str,
        *,
        admin_ip: str,
        admin_id: str,
        reason: str | None = None,
        email: str | None = None,
    ) -> None:
        """Write to primary, then shadow."""
        await self._primary.delete_user(
            user_id, admin_ip=admin_ip, admin_id=admin_id, reason=reason, email=email
        )
        await self._do_shadow(
            "delete_user",
            self._shadow.delete_user(
                user_id, admin_ip=admin_ip, admin_id=admin_id, reason=reason, email=email
            ),
            user_id=user_id,
        )

    async def resume_user(
        self,
        user_id: str,
        *,
        admin_ip: str,
        admin_id: str,
        reason: str | None = None,
        email: str | None = None,
    ) -> None:
        """Write to primary, then shadow."""
        await self._primary.resume_user(
            user_id, admin_ip=admin_ip, admin_id=admin_id, reason=reason, email=email
        )
        await self._do_shadow(
            "resume_user",
            self._shadow.resume_user(
                user_id, admin_ip=admin_ip, admin_id=admin_id, reason=reason, email=email
            ),
            user_id=user_id,
        )

    async def hard_delete_user(
        self,
        user_id: str,
        *,
        admin_ip: str,
        admin_id: str,
        reason: str | None = None,
        email: str | None = None,
    ) -> dict[str, int]:
        """Write to primary, then shadow.  Returns primary's row counts."""
        counts = await self._primary.hard_delete_user(
            user_id, admin_ip=admin_ip, admin_id=admin_id, reason=reason, email=email
        )
        await self._do_shadow(
            "hard_delete_user",
            self._shadow.hard_delete_user(
                user_id, admin_ip=admin_ip, admin_id=admin_id, reason=reason, email=email
            ),
            user_id=user_id,
        )
        return counts

    async def approve_user(self, user_id: str, *, admin_id: str, note: str | None = None) -> None:
        """Write to primary, then shadow."""
        await self._primary.approve_user(user_id, admin_id=admin_id, note=note)
        await self._do_shadow(
            "approve_user",
            self._shadow.approve_user(user_id, admin_id=admin_id, note=note),
            user_id=user_id,
        )

    async def reject_user(self, user_id: str, *, admin_id: str, reason: str) -> None:
        """Write to primary, then shadow."""
        await self._primary.reject_user(user_id, admin_id=admin_id, reason=reason)
        await self._do_shadow(
            "reject_user",
            self._shadow.reject_user(user_id, admin_id=admin_id, reason=reason),
            user_id=user_id,
        )

    # -- api keys: reads -----------------------------------------------------

    async def get_auth_context_by_key_hash(self, key_hash: str) -> Row | None:
        """Delegate to primary."""
        return await self._primary.get_auth_context_by_key_hash(key_hash)

    async def get_auth_context_lightweight(self, key_hash: str) -> Row | None:
        """Delegate to primary."""
        return await self._primary.get_auth_context_lightweight(key_hash)

    async def check_active_key_exists(self, user_id: str) -> bool:
        """Delegate to primary."""
        return await self._primary.check_active_key_exists(user_id)

    async def list_keys(
        self,
        *,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[int, list[Row]]:
        """Delegate to primary."""
        return await self._primary.list_keys(status=status, limit=limit, offset=offset)

    async def get_key_detail(self, user_id: str) -> Row | None:
        """Delegate to primary."""
        return await self._primary.get_key_detail(user_id)

    async def get_key_by_account_or_user(self, account_id: str) -> Row | None:
        """Delegate to primary."""
        return await self._primary.get_key_by_account_or_user(account_id)

    async def get_active_key_by_account(self, account_id: str) -> Row | None:
        """Delegate to primary."""
        return await self._primary.get_active_key_by_account(account_id)

    # -- api keys: writes ----------------------------------------------------

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
        """Write to primary, then shadow.  Returns primary result."""
        result = await self._primary.create_key(
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
        await self._do_shadow(
            "create_key",
            self._shadow.create_key(
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
            ),
            user_id=user_id,
            key_prefix=key_prefix,
        )
        return result

    async def update_key_last_used(self, key_id: int) -> None:
        """Write to primary, then shadow."""
        await self._primary.update_key_last_used(key_id)
        await self._do_shadow(
            "update_key_last_used",
            self._shadow.update_key_last_used(key_id),
            key_id=key_id,
        )

    async def update_key(self, user_id: str, **fields: Any) -> None:
        """Write to primary, then shadow."""
        await self._primary.update_key(user_id, **fields)
        await self._do_shadow(
            "update_key",
            self._shadow.update_key(user_id, **fields),
            user_id=user_id,
        )

    async def revoke_key(self, user_id: str, *, hard_delete: bool = False) -> None:
        """Write to primary, then shadow."""
        await self._primary.revoke_key(user_id, hard_delete=hard_delete)
        await self._do_shadow(
            "revoke_key",
            self._shadow.revoke_key(user_id, hard_delete=hard_delete),
            user_id=user_id,
        )

    async def regenerate_key(
        self,
        user_id: str,
        *,
        new_key_hash: str,
        new_key_prefix: str,
    ) -> str:
        """Write to primary, then shadow.  Returns old key prefix from primary."""
        old_prefix = await self._primary.regenerate_key(
            user_id, new_key_hash=new_key_hash, new_key_prefix=new_key_prefix
        )
        await self._do_shadow(
            "regenerate_key",
            self._shadow.regenerate_key(
                user_id, new_key_hash=new_key_hash, new_key_prefix=new_key_prefix
            ),
            user_id=user_id,
        )
        return old_prefix

    # -- sessions: reads -----------------------------------------------------

    async def get_session_by_token_hash(self, token_hash: str) -> Row | None:
        """Delegate to primary."""
        return await self._primary.get_session_by_token_hash(token_hash)

    # -- sessions: writes ----------------------------------------------------

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
        """Write to primary, then shadow."""
        await self._primary.create_session(
            session_id=session_id,
            user_id=user_id,
            refresh_token_hash=refresh_token_hash,
            jti=jti,
            sid=sid,
            expires_at=expires_at,
        )
        await self._do_shadow(
            "create_session",
            self._shadow.create_session(
                session_id=session_id,
                user_id=user_id,
                refresh_token_hash=refresh_token_hash,
                jti=jti,
                sid=sid,
                expires_at=expires_at,
            ),
            user_id=user_id,
            session_id=session_id,
        )

    async def rotate_session(
        self, session_id: str, *, new_refresh_token_hash: str, new_jti: str
    ) -> None:
        """Write to primary, then shadow."""
        await self._primary.rotate_session(
            session_id, new_refresh_token_hash=new_refresh_token_hash, new_jti=new_jti
        )
        await self._do_shadow(
            "rotate_session",
            self._shadow.rotate_session(
                session_id, new_refresh_token_hash=new_refresh_token_hash, new_jti=new_jti
            ),
            session_id=session_id,
        )

    async def revoke_session(self, session_id: str) -> None:
        """Write to primary, then shadow."""
        await self._primary.revoke_session(session_id)
        await self._do_shadow(
            "revoke_session",
            self._shadow.revoke_session(session_id),
            session_id=session_id,
        )

    async def delete_user_sessions(self, user_id: str) -> None:
        """Write to primary, then shadow."""
        await self._primary.delete_user_sessions(user_id)
        await self._do_shadow(
            "delete_user_sessions",
            self._shadow.delete_user_sessions(user_id),
            user_id=user_id,
        )

    # -- email verification tokens: reads ------------------------------------

    async def get_verification_token(self, token: str) -> Row | None:
        """Delegate to primary."""
        return await self._primary.get_verification_token(token)

    # -- email verification tokens: writes -----------------------------------

    async def create_verification_token(
        self, *, token: str, user_id: str, expires_at: datetime
    ) -> None:
        """Write to primary, then shadow."""
        await self._primary.create_verification_token(
            token=token, user_id=user_id, expires_at=expires_at
        )
        await self._do_shadow(
            "create_verification_token",
            self._shadow.create_verification_token(
                token=token, user_id=user_id, expires_at=expires_at
            ),
            user_id=user_id,
        )

    async def mark_verification_used(self, token: str) -> None:
        """Write to primary, then shadow."""
        await self._primary.mark_verification_used(token)
        await self._do_shadow(
            "mark_verification_used",
            self._shadow.mark_verification_used(token),
        )

    async def mark_user_email_verified(self, user_id: str) -> None:
        """Write to primary, then shadow."""
        await self._primary.mark_user_email_verified(user_id)
        await self._do_shadow(
            "mark_user_email_verified",
            self._shadow.mark_user_email_verified(user_id),
            user_id=user_id,
        )

    async def delete_user_verification_tokens(self, user_id: str) -> None:
        """Write to primary, then shadow."""
        await self._primary.delete_user_verification_tokens(user_id)
        await self._do_shadow(
            "delete_user_verification_tokens",
            self._shadow.delete_user_verification_tokens(user_id),
            user_id=user_id,
        )

    # -- password reset tokens: reads ----------------------------------------

    async def get_reset_token(self, token: str) -> Row | None:
        """Delegate to primary."""
        return await self._primary.get_reset_token(token)

    # -- password reset tokens: writes ---------------------------------------

    async def create_reset_token(self, *, token: str, user_id: str, expires_at: datetime) -> None:
        """Write to primary, then shadow."""
        await self._primary.create_reset_token(token=token, user_id=user_id, expires_at=expires_at)
        await self._do_shadow(
            "create_reset_token",
            self._shadow.create_reset_token(token=token, user_id=user_id, expires_at=expires_at),
            user_id=user_id,
        )

    async def mark_reset_used(self, token: str) -> None:
        """Write to primary, then shadow."""
        await self._primary.mark_reset_used(token)
        await self._do_shadow(
            "mark_reset_used",
            self._shadow.mark_reset_used(token),
        )

    async def delete_user_reset_tokens(self, user_id: str) -> None:
        """Write to primary, then shadow."""
        await self._primary.delete_user_reset_tokens(user_id)
        await self._do_shadow(
            "delete_user_reset_tokens",
            self._shadow.delete_user_reset_tokens(user_id),
            user_id=user_id,
        )

    # -- audit: write --------------------------------------------------------

    async def log_admin_action(
        self,
        *,
        admin_ip: str,
        action: str,
        target_user_id: str | None = None,
        details: dict[str, Any] | None = None,
        success: bool = True,
    ) -> None:
        """Write to primary, then shadow."""
        await self._primary.log_admin_action(
            admin_ip=admin_ip,
            action=action,
            target_user_id=target_user_id,
            details=details,
            success=success,
        )
        await self._do_shadow(
            "log_admin_action",
            self._shadow.log_admin_action(
                admin_ip=admin_ip,
                action=action,
                target_user_id=target_user_id,
                details=details,
                success=success,
            ),
            target_user_id=target_user_id,
            action=action,
        )

    # -- audit: read ---------------------------------------------------------

    async def list_audit_log(
        self,
        *,
        action: str | None = None,
        target_user_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[int, list[Row]]:
        """Delegate to primary."""
        return await self._primary.list_audit_log(
            action=action,
            target_user_id=target_user_id,
            limit=limit,
            offset=offset,
        )

    # -- cost counters -------------------------------------------------------

    async def increment_user_cost(
        self, user_id: str, cost_usd: float, *, day: str | None = None
    ) -> None:
        """Write to primary, then shadow."""
        await self._primary.increment_user_cost(user_id, cost_usd, day=day)
        await self._do_shadow(
            "increment_user_cost",
            self._shadow.increment_user_cost(user_id, cost_usd, day=day),
            user_id=user_id,
        )

    async def get_user_cost_today(self, user_id: str) -> float:
        """Delegate to primary."""
        return await self._primary.get_user_cost_today(user_id)

    async def get_user_cost_period(self, user_id: str, period: Literal["today", "month"]) -> float:
        """Delegate to primary."""
        return await self._primary.get_user_cost_period(user_id, period)

    async def get_batch_usage(
        self, user_ids: list[str], period: Literal["today", "month"]
    ) -> dict[str, float]:
        """Delegate to primary."""
        return await self._primary.get_batch_usage(user_ids, period)

    async def get_user_cost_history(self, user_id: str, days: int = 7) -> list[Row]:
        """Delegate to primary."""
        return await self._primary.get_user_cost_history(user_id, days=days)

    async def get_bulk_user_cost_history(
        self, user_ids: list[str], days: int = 7
    ) -> dict[str, list[Row]]:
        """Delegate to primary."""
        return await self._primary.get_bulk_user_cost_history(user_ids, days=days)

    async def get_users_summary(self, **kwargs: Any) -> Row:
        """Delegate to primary (read-only aggregation, no shadow needed)."""
        return await self._primary.get_users_summary(**kwargs)

    # -- preferences ---------------------------------------------------------

    async def get_user_preferences(self, user_id: str) -> dict[str, Any]:
        """Delegate to primary."""
        return await self._primary.get_user_preferences(user_id)

    async def update_user_preferences(self, user_id: str, preferences: dict[str, Any]) -> None:
        """Write to primary, then shadow."""
        await self._primary.update_user_preferences(user_id, preferences)
        await self._do_shadow(
            "update_user_preferences",
            self._shadow.update_user_preferences(user_id, preferences),
            user_id=user_id,
        )

    # -- signup domain allowlist --------------------------------------------

    async def list_signup_allowed_domains(self) -> list[Row]:
        """Delegate to primary."""
        return await self._primary.list_signup_allowed_domains()

    async def add_signup_allowed_domain(
        self,
        *,
        domain: str,
        is_wildcard: bool,
        created_by: str | None,
    ) -> Row:
        """Write to primary, then shadow."""
        result = await self._primary.add_signup_allowed_domain(
            domain=domain, is_wildcard=is_wildcard, created_by=created_by
        )
        await self._do_shadow(
            "add_signup_allowed_domain",
            self._shadow.add_signup_allowed_domain(
                domain=domain, is_wildcard=is_wildcard, created_by=created_by
            ),
            domain=domain,
        )
        return result

    async def remove_signup_allowed_domain(self, *, domain: str, is_wildcard: bool) -> bool:
        """Write to primary, then shadow."""
        result = await self._primary.remove_signup_allowed_domain(
            domain=domain, is_wildcard=is_wildcard
        )
        await self._do_shadow(
            "remove_signup_allowed_domain",
            self._shadow.remove_signup_allowed_domain(domain=domain, is_wildcard=is_wildcard),
            domain=domain,
        )
        return result

    async def signup_allowlist_is_empty(self) -> bool:
        """Delegate to primary."""
        return await self._primary.signup_allowlist_is_empty()

    async def is_signup_domain_allowed(self, email: str) -> bool:
        """Delegate to primary."""
        return await self._primary.is_signup_domain_allowed(email)

    # -- site settings --------------------------------------------------------

    async def get_setting(self, key: str) -> Row | None:
        """Delegate to primary."""
        return await self._primary.get_setting(key)

    async def set_setting(
        self, key: str, value: str, value_type: str, updated_by: str | None
    ) -> None:
        """Upsert a site_settings row on primary and shadow-write to secondary."""
        await self._primary.set_setting(key, value, value_type, updated_by)
        self._do_shadow(
            "set_setting",
            self._shadow.set_setting(key, value, value_type, updated_by),
            key=key,
        )

    async def list_settings(self) -> list[Row]:
        """Delegate to primary."""
        return await self._primary.list_settings()


# ---------------------------------------------------------------------------
# DualWriteLogStore
# ---------------------------------------------------------------------------


class DualWriteLogStore(LogStore):
    """Dual-write proxy: D1 primary + PostgreSQL shadow for request logs.

    All reads go to the primary.  ``log_request`` writes to both stores.
    Each store handles its own column mapping internally (D1 stores slim
    rows, PostgreSQL stores full content).
    """

    def __init__(self, primary: LogStore, shadow: LogStore) -> None:
        self._primary = primary
        self._shadow = shadow
        self._shadow_healthy: bool = True

    async def _do_shadow(self, method_name: str, coro, /, **ctx_ids: Any) -> None:
        """Await *coro* (a shadow method call).  On failure, log and swallow."""
        try:
            await coro
            if not self._shadow_healthy:
                self._shadow_healthy = True
                logger.info("Shadow log store recovered: %s", method_name)
        except Exception:
            self._shadow_healthy = False
            ctx = _shadow_ctx(**ctx_ids)
            logger.warning(
                "Shadow log write failed: method=%s %s",
                method_name,
                ctx,
                exc_info=True,
            )

    @property
    def shadow_healthy(self) -> bool:
        """Whether the last shadow write succeeded.  Exposed for health endpoints."""
        return self._shadow_healthy

    # -- lifecycle -----------------------------------------------------------

    async def initialize(self) -> None:
        """Initialize both primary and shadow stores."""
        await self._primary.initialize()
        try:
            await self._shadow.initialize()
        except Exception:
            self._shadow_healthy = False
            logger.warning("Shadow log store failed to initialize", exc_info=True)

    async def cleanup(self) -> None:
        """Clean up both primary and shadow stores."""
        await self._primary.cleanup()
        try:
            await self._shadow.cleanup()
        except Exception:
            logger.warning("Shadow log store failed to cleanup", exc_info=True)

    async def health_check(self) -> bool:
        """Return primary health.  Shadow health tracked via shadow_healthy property."""
        primary_ok = await self._primary.health_check()
        try:
            shadow_ok = await self._shadow.health_check()
            if not shadow_ok and self._shadow_healthy:
                self._shadow_healthy = False
                logger.warning("Shadow log store health check returned False")
            elif shadow_ok and not self._shadow_healthy:
                self._shadow_healthy = True
        except Exception:
            self._shadow_healthy = False
            logger.warning("Shadow log store health check failed", exc_info=True)
        return primary_ok

    # -- request logging (the sole write) ------------------------------------

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
        upstream_cost_usd: float | None = None,
    ) -> None:
        """Write to primary, then shadow."""
        kwargs: dict[str, Any] = {
            "request_id": request_id,
            "model_id": model_id,
            "provider": provider,
            "prompt": prompt,
            "response": response,
            "usage": usage,
            "latency_ms": latency_ms,
            "status_code": status_code,
            "error": error,
            "params": params,
            "metadata": metadata,
            "ttft_ms": ttft_ms,
            "prompt_hash": prompt_hash,
            "response_hash": response_hash,
            "store_full_content": store_full_content,
            "pricing": pricing,
            "upstream_cost_usd": upstream_cost_usd,
        }
        await self._primary.log_request(**kwargs)
        await self._do_shadow(
            "log_request",
            self._shadow.log_request(**kwargs),
            request_id=request_id,
        )

    # -- usage / cost reads --------------------------------------------------

    async def get_user_cost_today(self, user_id: str) -> float:
        """Delegate to primary."""
        return await self._primary.get_user_cost_today(user_id)

    async def get_user_cost_period(self, user_id: str, period: Literal["today", "month"]) -> float:
        """Delegate to primary."""
        return await self._primary.get_user_cost_period(user_id, period)

    async def get_user_usage_detail(self, user_id: str) -> dict[str, Any]:
        """Delegate to primary."""
        return await self._primary.get_user_usage_detail(user_id)

    async def get_batch_usage(
        self, user_ids: list[str], period: Literal["today", "month"]
    ) -> dict[str, float]:
        """Delegate to primary."""
        return await self._primary.get_batch_usage(user_ids, period)

    async def get_user_detail_usage(self, user_id: str) -> dict[str, Any]:
        """Delegate to primary."""
        return await self._primary.get_user_detail_usage(user_id)

    async def get_key_detail_usage(self, user_id: str) -> dict[str, Any]:
        """Delegate to primary."""
        return await self._primary.get_key_detail_usage(user_id)

    # -- analytics reads -----------------------------------------------------

    async def get_model_activity(self, window_minutes: int = 10) -> dict[str, Any]:
        """Delegate to primary."""
        return await self._primary.get_model_activity(window_minutes)

    async def get_stats(
        self,
        *,
        model_id: str | None = None,
        provider: str | None = None,
        hours: int = 24,
    ) -> list[Row]:
        """Delegate to primary."""
        return await self._primary.get_stats(model_id=model_id, provider=provider, hours=hours)

    # -- admin: hard-delete user-owned rows ---------------------------------

    async def hard_delete_user_data(self, user_id: str) -> dict[str, int]:
        """Wipe user-owned rows on primary, then shadow.  Returns primary counts."""
        counts = await self._primary.hard_delete_user_data(user_id)
        await self._do_shadow(
            "hard_delete_user_data",
            self._shadow.hard_delete_user_data(user_id),
            user_id=user_id,
        )
        return counts
