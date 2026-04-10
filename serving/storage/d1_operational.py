"""Cloudflare D1 implementation of OperationalStore.

SQLite-dialect translations of all queries from PostgresOperationalStore.
Uses the D1Client HTTP wrapper for all database access.

Key differences from PostgreSQL:
- BOOLEAN → INTEGER (0/1)
- TIMESTAMPTZ → TEXT (ISO 8601)
- JSONB → TEXT (json.dumps/loads)
- $1, $2 params → ?, ?
- NOW() → strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
- ILIKE → LIKE (D1 is case-insensitive for ASCII)
- No FILTER aggregate, no FOR UPDATE (partial indexes are supported)
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal

from serving.storage.base import OperationalStore, Row
from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from decimal import Decimal

    from serving.storage.d1_client import D1Client

logger = get_logger(__name__)


def _now_iso() -> str:
    """Return current UTC timestamp as ISO 8601 string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _dt_to_iso(dt: datetime | None) -> str | None:
    """Convert a datetime to ISO 8601 string, or None."""
    if dt is None:
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _iso_to_dt(val: str | None) -> datetime | None:
    """Parse an ISO 8601 string back to a timezone-aware datetime, or None."""
    if val is None:
        return None
    # Handle both 3-digit (SQLite strftime %f) and 6-digit (Python %f) fractional seconds
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(val, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


# Columns in returned rows that should be parsed from ISO strings to datetime
_TIMESTAMP_COLUMNS = frozenset(
    {
        "created_at",
        "last_login_at",
        "expires_at",
        "last_used_at",
        "reviewed_at",
        "used_at",
        "last_request_at",
    }
)


_BOOLEAN_COLUMNS = frozenset(
    {
        "email_verified",
        "revoked",
    }
)


def _parse_row_timestamps(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """Convert known timestamp and boolean columns in a D1 result row.

    Timestamps are parsed from ISO 8601 strings to datetime objects.
    Booleans are converted from SQLite integers (0/1) to Python bools.
    """
    if row is None:
        return None
    out = dict(row)
    for col in _TIMESTAMP_COLUMNS:
        if col in out and isinstance(out[col], str):
            out[col] = _iso_to_dt(out[col])
    for col in _BOOLEAN_COLUMNS:
        if col in out and isinstance(out[col], int):
            out[col] = bool(out[col])
    return out


class D1OperationalStore(OperationalStore):
    """OperationalStore backed by Cloudflare D1 via HTTP API."""

    def __init__(self, client: D1Client) -> None:
        """Initialize with a D1Client instance."""
        self._d1 = client

    # -- lifecycle -----------------------------------------------------------

    async def initialize(self) -> None:
        """Create tables/indexes by executing the D1 schema DDL.

        Statements are executed one at a time because CREATE INDEX
        requires the referenced table to already exist, and D1 batches
        are atomic (tables created in a batch aren't visible to later
        statements in the same batch).
        """
        from pathlib import Path

        schema_path = Path(__file__).parent / "d1_schema.sql"
        schema_sql = schema_path.read_text()

        statements: list[str] = []
        for segment in schema_sql.split(";"):
            # Strip comment-only lines from the segment
            lines = [ln for ln in segment.splitlines() if not ln.strip().startswith("--")]
            cleaned = "\n".join(lines).strip()
            if cleaned:
                statements.append(cleaned)

        for stmt in statements:
            await self._d1.execute(stmt)
        logger.info("D1 schema initialized (%d statements)", len(statements))

    async def cleanup(self) -> None:
        """Close the D1 HTTP client."""
        await self._d1.close()

    async def health_check(self) -> bool:
        """Return True if D1 is reachable."""
        return await self._d1.health_check()

    # -- users ---------------------------------------------------------------

    async def get_user_by_id(self, user_id: str) -> Row | None:
        """Fetch a single user row by primary key."""
        result = await self._d1.query(
            "SELECT id, email, user_name, role, status, email_verified, "
            "created_at, last_login_at, password_hash, preferences "
            "FROM users WHERE id = ?",
            [user_id],
        )
        return _parse_row_timestamps(result.rows[0]) if result.rows else None

    async def get_user_by_email(self, email: str) -> Row | None:
        """Fetch a single user row by lowercased email."""
        result = await self._d1.query(
            "SELECT id, email, user_name, role, status, email_verified, "
            "created_at, last_login_at, password_hash, preferences "
            "FROM users WHERE email = ?",
            [email.lower()],
        )
        return _parse_row_timestamps(result.rows[0]) if result.rows else None

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
        await self._d1.execute(
            "INSERT INTO users (id, email, password_hash, user_name, "
            "email_verified, status) VALUES (?, ?, ?, ?, ?, ?)",
            [user_id, email.lower(), password_hash, user_name, int(email_verified), status],
        )

    async def update_user_fields(self, user_id: str, **fields: Any) -> None:
        """Update one or more columns on the users table."""
        if not fields:
            return
        from decimal import Decimal as _Decimal

        from .base import USERS_MUTABLE_COLUMNS

        invalid = set(fields) - USERS_MUTABLE_COLUMNS.keys()
        if invalid:
            raise ValueError(f"Invalid column(s) for users: {invalid}")
        set_parts = []
        params: list[Any] = []
        for key, val in fields.items():
            col = USERS_MUTABLE_COLUMNS[key]
            set_parts.append(f"{col} = ?")
            if isinstance(val, bool):
                params.append(int(val))
            elif isinstance(val, datetime):
                params.append(_dt_to_iso(val))
            elif isinstance(val, _Decimal):
                params.append(float(val))
            else:
                params.append(val)
        params.append(user_id)
        sql = f"UPDATE users SET {', '.join(set_parts)} WHERE id = ?"
        await self._d1.execute(sql, params)

    async def update_user_last_login(self, user_id: str) -> None:
        """Set last_login_at to the current timestamp."""
        await self._d1.execute(
            "UPDATE users SET last_login_at = ? WHERE id = ?",
            [_now_iso(), user_id],
        )

    async def delete_user(
        self,
        user_id: str,
        *,
        admin_ip: str,
        admin_id: str,
        reason: str | None = None,
        email: str | None = None,
    ) -> None:
        """Soft-delete user atomically via D1 batch.

        All mutations (status update, key revocation, session/token purge,
        audit log) execute in a single batch request which D1 treats as an
        atomic transaction.
        """
        now = _now_iso()
        details = json.dumps({"admin_id": admin_id, "reason": reason, "email": email})

        await self._d1.batch(
            [
                ("UPDATE users SET status = 'deleted' WHERE id = ?", [user_id]),
                ("UPDATE api_keys SET status = 'revoked' WHERE user_id = ?", [user_id]),
                ("DELETE FROM auth_sessions WHERE user_id = ?", [user_id]),
                ("DELETE FROM email_verification_tokens WHERE user_id = ?", [user_id]),
                ("DELETE FROM password_reset_tokens WHERE user_id = ?", [user_id]),
                (
                    "INSERT INTO admin_audit_log (timestamp, admin_ip, action, "
                    "target_user_id, details, success) VALUES (?, ?, ?, ?, ?, ?)",
                    [now, admin_ip, "delete_user", user_id, details, 1],
                ),
            ]
        )

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
        """Return (total_count, user_rows, status_counts_row).

        Cost-based sorts are not supported in D1 (api_logs is in Postgres).
        They fall back to created_at ordering; the caller enriches costs.
        """
        where_clauses: list[str] = []
        params: list[Any] = []

        if status:
            where_clauses.append("u.status = ?")
            params.append(status)
        if search:
            # D1 LIKE patterns limited to 50 bytes; truncate search term
            truncated = search[:46]  # 46 + len("%%") = 48, safely under 50
            where_clauses.append("(u.email LIKE ? OR u.user_name LIKE ?)")
            params.extend([f"%{truncated}%", f"%{truncated}%"])

        where_sql = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""

        # D1 cannot join api_logs — cost sorts fall back to created
        _sort_map = {
            "created": "u.created_at DESC, u.id",
            "cost_today": "u.created_at DESC, u.id",
            "cost_month": "u.created_at DESC, u.id",
            "cost_alltime": "u.created_at DESC, u.id",
            "last_login": "u.last_login_at DESC, u.created_at DESC, u.id",
        }
        order_clause = _sort_map[sort_by]

        # Total count
        count_result = await self._d1.query(
            f"SELECT COUNT(*) as total FROM users u {where_sql}",
            params,
        )
        total = count_result.rows[0]["total"] if count_result.rows else 0

        # Status counts (unfiltered)
        sc_result = await self._d1.query(
            "SELECT status, COUNT(*) as cnt FROM users GROUP BY status"
        )
        sc = {r["status"]: r["cnt"] for r in sc_result.rows}
        status_counts: Row = {
            "all": sum(sc.values()),
            "pending_approval": sc.get("pending_approval", 0),
            "active": sc.get("active", 0),
            "suspended": sc.get("suspended", 0),
            "rejected": sc.get("rejected", 0),
            "deleted": sc.get("deleted", 0),
        }

        # Main query
        query_params = [*params, limit, offset]
        rows_result = await self._d1.query(
            f"SELECT u.id, u.email, u.user_name, u.role, u.status, "
            f"u.email_verified, u.approval_note, u.reviewed_at, u.reviewed_by, "
            f"u.created_at, u.last_login_at, "
            f"k.key_prefix, k.status AS key_status, k.tier AS key_tier "
            f"FROM users u "
            f"LEFT JOIN api_keys k ON k.account_id = u.id AND k.status = 'active' "
            f"{where_sql} "
            f"ORDER BY {order_clause} "
            f"LIMIT ? OFFSET ?",
            query_params,
        )

        return total, rows_result.rows, status_counts

    async def approve_user(
        self,
        user_id: str,
        *,
        admin_id: str,
        note: str | None = None,
    ) -> None:
        """Set status='active', record reviewer and note."""
        now = _now_iso()
        await self._d1.execute(
            "UPDATE users SET status = 'active', reviewed_at = ?, reviewed_by = ?, "
            "approval_note = ? WHERE id = ?",
            [now, admin_id, note, user_id],
        )

    async def reject_user(
        self,
        user_id: str,
        *,
        admin_id: str,
        reason: str,
    ) -> None:
        """Set status='rejected', record reviewer and reason."""
        now = _now_iso()
        await self._d1.execute(
            "UPDATE users SET status = 'rejected', reviewed_at = ?, reviewed_by = ?, "
            "approval_note = ? WHERE id = ?",
            [now, admin_id, reason, user_id],
        )

    async def get_user_counts_by_status(self) -> dict[str, int]:
        """Return {status_value: count} for all statuses."""
        result = await self._d1.query("SELECT status, COUNT(*) as cnt FROM users GROUP BY status")
        return {r["status"]: r["cnt"] for r in result.rows}

    async def get_active_user_counts(self) -> dict[str, int]:
        """Return {"total": N, "dau": N, "mau": N}."""
        total_r = await self._d1.query(
            "SELECT COUNT(*) as count FROM users WHERE status = 'active'"
        )
        dau_r = await self._d1.query(
            "SELECT COUNT(DISTINCT id) as count FROM users "
            "WHERE status = 'active' AND last_login_at >= datetime('now', '-24 hours')"
        )
        mau_r = await self._d1.query(
            "SELECT COUNT(DISTINCT id) as count FROM users "
            "WHERE status = 'active' AND last_login_at >= datetime('now', '-30 days')"
        )
        return {
            "total": total_r.rows[0]["count"] if total_r.rows else 0,
            "dau": dau_r.rows[0]["count"] if dau_r.rows else 0,
            "mau": mau_r.rows[0]["count"] if mau_r.rows else 0,
        }

    # -- api keys ------------------------------------------------------------

    async def get_auth_context_by_key_hash(self, key_hash: str) -> Row | None:
        """Materialized auth lookup for verify_api_key.

        Returns full projection in a single round-trip.
        """
        result = await self._d1.query(
            "SELECT k.id, k.user_id, k.user_name, k.quota_daily_cost_usd, k.tier, "
            "u.email, u.role "
            "FROM api_keys k "
            "LEFT JOIN users u ON u.id = k.user_id "
            "WHERE k.key_hash = ? "
            "  AND k.status = 'active' "
            "  AND (k.expires_at IS NULL OR k.expires_at > datetime('now')) "
            "  AND (u.id IS NULL OR u.status = 'active')",
            [key_hash],
        )
        return _parse_row_timestamps(result.rows[0]) if result.rows else None

    async def get_auth_context_lightweight(self, key_hash: str) -> Row | None:
        """Lightweight identity lookup."""
        result = await self._d1.query(
            "SELECT k.user_id, u.email, u.role "
            "FROM api_keys k "
            "LEFT JOIN users u ON u.id = k.user_id "
            "WHERE k.key_hash = ? "
            "  AND k.status = 'active' "
            "  AND (k.expires_at IS NULL OR k.expires_at > datetime('now')) "
            "  AND (u.id IS NULL OR u.status = 'active')",
            [key_hash],
        )
        return _parse_row_timestamps(result.rows[0]) if result.rows else None

    async def update_key_last_used(self, key_id: int) -> None:
        """Set last_used_at for the given key id."""
        await self._d1.execute(
            "UPDATE api_keys SET last_used_at = ? WHERE id = ?",
            [_now_iso(), key_id],
        )

    async def create_key(
        self,
        *,
        key_hash: str,
        key_prefix: str,
        user_id: str,
        user_name: str | None = None,
        tier: str = "free",
        quota_daily_cost_usd: Decimal | float = 1000.0,
        quota_monthly_cost_usd: Decimal | float | None = None,
        expires_at: datetime | None = None,
        notes: str | None = None,
        metadata: str | None = None,
        account_id: str | None = None,
    ) -> Row:
        """Insert a new API key. Returns the inserted row."""
        now = _now_iso()
        result = await self._d1.query(
            "INSERT INTO api_keys "
            "(key_hash, key_prefix, user_id, user_name, tier, "
            "quota_daily_cost_usd, quota_monthly_cost_usd, "
            "expires_at, notes, metadata, account_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "RETURNING id, created_at",
            [
                key_hash,
                key_prefix,
                user_id,
                user_name,
                tier,
                float(quota_daily_cost_usd),
                float(quota_monthly_cost_usd) if quota_monthly_cost_usd is not None else None,
                _dt_to_iso(expires_at),
                notes,
                metadata,
                account_id,
                now,
            ],
        )
        if result.rows:
            return _parse_row_timestamps(result.rows[0])
        # Fallback if RETURNING not supported in D1 version
        return {"id": result.last_row_id, "created_at": now}

    async def check_active_key_exists(self, user_id: str) -> bool:
        """Return True if user_id already has an active key."""
        result = await self._d1.query(
            "SELECT id FROM api_keys WHERE user_id = ? AND status = 'active'",
            [user_id],
        )
        return len(result.rows) > 0

    async def list_keys(
        self,
        *,
        status: str | None = None,
        tier: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[int, list[Row]]:
        """Return (total_count, key_rows) with optional filters."""
        where_clauses: list[str] = []
        params: list[Any] = []

        if status:
            where_clauses.append("status = ?")
            params.append(status)
        if tier:
            where_clauses.append("tier = ?")
            params.append(tier)

        where_sql = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""

        count_result = await self._d1.query(
            f"SELECT COUNT(*) as total FROM api_keys {where_sql}",
            params,
        )
        total = count_result.rows[0]["total"] if count_result.rows else 0

        query_params = [*params, limit, offset]
        rows_result = await self._d1.query(
            f"SELECT user_id, user_name, key_prefix, tier, status, "
            f"quota_daily_cost_usd, quota_monthly_cost_usd, "
            f"created_at, last_used_at, expires_at, notes "
            f"FROM api_keys {where_sql} "
            f"ORDER BY created_at DESC "
            f"LIMIT ? OFFSET ?",
            query_params,
        )

        return total, rows_result.rows

    async def get_key_detail(self, user_id: str) -> Row | None:
        """Fetch full key row for a given user_id."""
        result = await self._d1.query(
            "SELECT user_id, user_name, key_prefix, tier, status, "
            "quota_daily_cost_usd, quota_monthly_cost_usd, "
            "created_at, last_used_at, expires_at, notes, metadata "
            "FROM api_keys WHERE user_id = ?",
            [user_id],
        )
        return _parse_row_timestamps(result.rows[0]) if result.rows else None

    async def update_key(self, user_id: str, **fields: Any) -> None:
        """Dynamically update key columns for user_id."""
        if not fields:
            return
        from decimal import Decimal as _Decimal

        from .base import API_KEYS_MUTABLE_COLUMNS

        invalid = set(fields) - API_KEYS_MUTABLE_COLUMNS.keys()
        if invalid:
            raise ValueError(f"Invalid column(s) for api_keys: {invalid}")
        set_parts = []
        params: list[Any] = []
        for key, val in fields.items():
            col = API_KEYS_MUTABLE_COLUMNS[key]
            set_parts.append(f"{col} = ?")
            if isinstance(val, bool):
                params.append(int(val))
            elif isinstance(val, datetime):
                params.append(_dt_to_iso(val))
            elif isinstance(val, _Decimal):
                params.append(float(val))
            else:
                params.append(val)
        params.append(user_id)
        sql = f"UPDATE api_keys SET {', '.join(set_parts)} WHERE user_id = ?"
        await self._d1.execute(sql, params)

    async def revoke_key(self, user_id: str, *, hard_delete: bool = False) -> None:
        """Soft-revoke or hard-delete the key."""
        if hard_delete:
            await self._d1.execute("DELETE FROM api_keys WHERE user_id = ?", [user_id])
        else:
            await self._d1.execute(
                "UPDATE api_keys SET status = 'revoked' WHERE user_id = ?",
                [user_id],
            )

    async def regenerate_key(
        self,
        user_id: str,
        *,
        new_key_hash: str,
        new_key_prefix: str,
    ) -> str:
        """Atomically replace the key hash/prefix. Returns old key_prefix.

        Uses a read-then-batch to minimize the race window. The read fetches
        the old prefix, then the batch atomically updates the key.
        """
        old_result = await self._d1.query(
            "SELECT key_prefix FROM api_keys WHERE user_id = ?", [user_id]
        )
        if not old_result.rows:
            raise ValueError(f"No key found for user_id={user_id}")

        await self._d1.batch(
            [
                (
                    "UPDATE api_keys SET key_hash = ?, key_prefix = ? WHERE user_id = ?",
                    [new_key_hash, new_key_prefix, user_id],
                ),
            ]
        )
        return old_result.rows[0]["key_prefix"]

    async def get_key_by_account_or_user(self, account_id: str) -> Row | None:
        """Fetch key row by account_id or user_id."""
        result = await self._d1.query(
            "SELECT tier FROM api_keys "
            "WHERE (account_id = ? OR user_id = ?) AND status = 'active' "
            "LIMIT 1",
            [account_id, account_id],
        )
        return _parse_row_timestamps(result.rows[0]) if result.rows else None

    async def get_active_key_by_account(self, account_id: str) -> Row | None:
        """Fetch the active key for self-registered user by account_id."""
        result = await self._d1.query(
            "SELECT id, key_prefix, created_at, last_used_at, status, "
            "quota_daily_cost_usd "
            "FROM api_keys WHERE account_id = ? AND status = 'active'",
            [account_id],
        )
        return _parse_row_timestamps(result.rows[0]) if result.rows else None

    # -- auth sessions -------------------------------------------------------

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
        await self._d1.execute(
            "INSERT INTO auth_sessions "
            "(id, user_id, refresh_token_hash, jti, sid, expires_at, revoked) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [session_id, user_id, refresh_token_hash, jti, sid, _dt_to_iso(expires_at), 0],
        )

    async def get_session_by_token_hash(self, token_hash: str) -> Row | None:
        """Fetch session row by refresh_token_hash."""
        result = await self._d1.query(
            "SELECT id, user_id, sid, expires_at, revoked "
            "FROM auth_sessions WHERE refresh_token_hash = ?",
            [token_hash],
        )
        return _parse_row_timestamps(result.rows[0]) if result.rows else None

    async def rotate_session(
        self,
        session_id: str,
        *,
        new_refresh_token_hash: str,
        new_jti: str,
    ) -> None:
        """Update the session with a new refresh token hash and JTI."""
        await self._d1.execute(
            "UPDATE auth_sessions "
            "SET last_used_at = ?, jti = ?, refresh_token_hash = ? "
            "WHERE id = ?",
            [_now_iso(), new_jti, new_refresh_token_hash, session_id],
        )

    async def revoke_session(self, session_id: str) -> None:
        """Set revoked = 1 on the session."""
        await self._d1.execute(
            "UPDATE auth_sessions SET revoked = 1 WHERE id = ?",
            [session_id],
        )

    async def delete_user_sessions(self, user_id: str) -> None:
        """Delete all sessions for a user."""
        await self._d1.execute("DELETE FROM auth_sessions WHERE user_id = ?", [user_id])

    # -- email verification tokens -------------------------------------------

    async def create_verification_token(
        self,
        *,
        token: str,
        user_id: str,
        expires_at: datetime,
    ) -> None:
        """Insert a new email verification token."""
        await self._d1.execute(
            "INSERT INTO email_verification_tokens (token, user_id, expires_at) VALUES (?, ?, ?)",
            [token, user_id, _dt_to_iso(expires_at)],
        )

    async def get_verification_token(self, token: str) -> Row | None:
        """Fetch verification token row."""
        result = await self._d1.query(
            "SELECT token, user_id, created_at, expires_at, used_at "
            "FROM email_verification_tokens WHERE token = ?",
            [token],
        )
        return _parse_row_timestamps(result.rows[0]) if result.rows else None

    async def mark_verification_used(self, token: str) -> None:
        """Set used_at on the token."""
        await self._d1.execute(
            "UPDATE email_verification_tokens SET used_at = ? WHERE token = ?",
            [_now_iso(), token],
        )

    async def mark_user_email_verified(self, user_id: str) -> None:
        """Set email_verified = 1 on the user."""
        await self._d1.execute("UPDATE users SET email_verified = 1 WHERE id = ?", [user_id])

    async def delete_user_verification_tokens(self, user_id: str) -> None:
        """Delete all verification tokens for a user."""
        await self._d1.execute("DELETE FROM email_verification_tokens WHERE user_id = ?", [user_id])

    # -- password reset tokens -----------------------------------------------

    async def create_reset_token(
        self,
        *,
        token: str,
        user_id: str,
        expires_at: datetime,
    ) -> None:
        """Insert a new password reset token."""
        await self._d1.execute(
            "INSERT INTO password_reset_tokens (token, user_id, expires_at) VALUES (?, ?, ?)",
            [token, user_id, _dt_to_iso(expires_at)],
        )

    async def get_reset_token(self, token: str) -> Row | None:
        """Fetch reset token row."""
        result = await self._d1.query(
            "SELECT token, user_id, created_at, expires_at, used_at "
            "FROM password_reset_tokens WHERE token = ?",
            [token],
        )
        return _parse_row_timestamps(result.rows[0]) if result.rows else None

    async def mark_reset_used(self, token: str) -> None:
        """Set used_at on the token."""
        await self._d1.execute(
            "UPDATE password_reset_tokens SET used_at = ? WHERE token = ?",
            [_now_iso(), token],
        )

    async def delete_user_reset_tokens(self, user_id: str) -> None:
        """Delete all reset tokens for a user."""
        await self._d1.execute("DELETE FROM password_reset_tokens WHERE user_id = ?", [user_id])

    # -- admin audit log -----------------------------------------------------

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
        await self._d1.execute(
            "INSERT INTO admin_audit_log "
            "(timestamp, admin_ip, action, target_user_id, details, success) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                _now_iso(),
                admin_ip,
                action,
                target_user_id,
                json.dumps(details) if details else None,
                int(success),
            ],
        )

    async def list_audit_log(
        self,
        *,
        action: str | None = None,
        target_user_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[int, list[Row]]:
        """Return (total_count, audit_rows) with optional filters."""
        where_clauses: list[str] = []
        params: list[Any] = []

        if action:
            where_clauses.append("action = ?")
            params.append(action)
        if target_user_id:
            where_clauses.append("target_user_id = ?")
            params.append(target_user_id)

        where_sql = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""

        count_result = await self._d1.query(
            f"SELECT COUNT(*) as total FROM admin_audit_log {where_sql}",
            params,
        )
        total = count_result.rows[0]["total"] if count_result.rows else 0

        query_params = [*params, limit, offset]
        rows_result = await self._d1.query(
            f"SELECT id, timestamp, admin_ip, action, target_user_id, "
            f"details, success FROM admin_audit_log "
            f"{where_sql} ORDER BY timestamp DESC "
            f"LIMIT ? OFFSET ?",
            query_params,
        )

        return total, rows_result.rows

    # -- user preferences ----------------------------------------------------

    async def get_user_preferences(self, user_id: str) -> dict[str, Any]:
        """Return the preferences column parsed as dict."""
        result = await self._d1.query("SELECT preferences FROM users WHERE id = ?", [user_id])
        if not result.rows:
            return {}
        val = result.rows[0].get("preferences")
        if isinstance(val, dict):
            return val
        if isinstance(val, str):
            try:
                parsed = json.loads(val)
                if isinstance(parsed, dict):
                    return parsed
            except (json.JSONDecodeError, TypeError):
                pass
        return {}

    async def update_user_preferences(
        self,
        user_id: str,
        preferences: dict[str, Any],
    ) -> None:
        """Replace the full preferences column."""
        await self._d1.execute(
            "UPDATE users SET preferences = ? WHERE id = ?",
            [json.dumps(preferences), user_id],
        )

    # -- cost counters -------------------------------------------------------

    async def increment_user_cost(
        self,
        user_id: str,
        cost_usd: float,
        *,
        day: str | None = None,
    ) -> None:
        """Atomically increment the daily cost counter via upsert."""
        if day is None:
            day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        now = _now_iso()
        await self._d1.execute(
            "INSERT INTO user_daily_cost (user_id, day, cost_usd, requests, last_request_at) "
            "VALUES (?, ?, ?, 1, ?) "
            "ON CONFLICT(user_id, day) DO UPDATE SET "
            "cost_usd = cost_usd + ?, requests = requests + 1, last_request_at = ?",
            [user_id, day, cost_usd, now, cost_usd, now],
        )

    async def get_user_cost_today(self, user_id: str) -> float:
        """Return today's cost from the counter table."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        result = await self._d1.query(
            "SELECT cost_usd FROM user_daily_cost WHERE user_id = ? AND day = ?",
            [user_id, today],
        )
        if result.rows:
            return float(result.rows[0]["cost_usd"])
        return 0.0

    async def get_user_cost_period(
        self,
        user_id: str,
        period: Literal["today", "month"],
    ) -> float:
        """Return cost for a period from the counter table."""
        if period == "today":
            return await self.get_user_cost_today(user_id)
        # month: sum all days in current UTC month
        month_prefix = datetime.now(timezone.utc).strftime("%Y-%m")
        result = await self._d1.query(
            "SELECT COALESCE(SUM(cost_usd), 0) as total "
            "FROM user_daily_cost WHERE user_id = ? AND day LIKE ?",
            [user_id, f"{month_prefix}%"],
        )
        if result.rows:
            return float(result.rows[0]["total"])
        return 0.0

    async def get_batch_usage(
        self,
        user_ids: list[str],
        period: Literal["today", "month"],
    ) -> dict[str, float]:
        """Return {user_id: cost_usd} for a batch of users."""
        if not user_ids:
            return {}
        if period == "today":
            day_filter = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            where = "day = ?"
            params: list[Any] = [day_filter]
        else:
            month_prefix = datetime.now(timezone.utc).strftime("%Y-%m")
            where = "day LIKE ?"
            params = [f"{month_prefix}%"]

        placeholders = ",".join(["?"] * len(user_ids))
        params.extend(user_ids)
        result = await self._d1.query(
            f"SELECT user_id, COALESCE(SUM(cost_usd), 0) as cost "
            f"FROM user_daily_cost WHERE {where} AND user_id IN ({placeholders}) "
            f"GROUP BY user_id",
            params,
        )
        return {r["user_id"]: float(r["cost"]) for r in result.rows}
