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
from datetime import datetime, timedelta, timezone
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

        # --- Idempotent migrations (mirror postgres_operational.initialize) ---
        await self._migrate_users_role_check()
        await self._migrate_drop_api_keys_tier()

    async def _migrate_users_role_check(self) -> None:
        """Rebuild users table when CHECK constraint is missing the 'pro' role.

        SQLite cannot ALTER a CHECK constraint, so we rebuild the table when
        the existing DDL doesn't contain the new role. Idempotent: re-running
        on a migrated DB is a no-op since the rebuilt CHECK already lists 'pro'.
        D1 batches aren't atomic across DDL/DML, so statements run individually
        and any failure surfaces naturally; rerun on next boot will retry since
        the substring check still fails.
        """
        result = await self._d1.query(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='users'"
        )
        if not result.rows:
            return  # users table not present (e.g. fresh boot before schema load)
        users_sql = result.rows[0].get("sql") or ""
        # Match the exact quoted role literal from the new CHECK constraint to
        # avoid false-positives on any other token containing 'pro'.
        if "'pro'" in users_sql:
            return  # already migrated

        # Migrate legacy roles before copy (mirror postgres lines 142-144).
        await self._d1.execute(
            "UPDATE users SET role = 'internal' WHERE role IN ('internal_group', 'developer')"
        )

        # Build the new table with the same schema as d1_schema.sql.
        await self._d1.execute(
            "CREATE TABLE users_new ("
            "    id                TEXT PRIMARY KEY,"
            "    email             TEXT NOT NULL UNIQUE,"
            "    password_hash     TEXT NOT NULL,"
            "    user_name         TEXT,"
            "    preferences       TEXT NOT NULL DEFAULT '{}',"
            "    role              TEXT NOT NULL DEFAULT 'free'"
            "                      CHECK (role IN ('free', 'pro', 'internal', 'admin')),"
            "    email_verified    INTEGER DEFAULT 0,"
            "    status            TEXT DEFAULT 'active'"
            "                      CHECK (status IN ('active', 'suspended', 'deleted',"
            "                                        'pending_approval', 'rejected')),"
            "    approval_note     TEXT,"
            "    reviewed_at       TEXT,"
            "    reviewed_by       TEXT,"
            "    created_at        TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),"
            "    last_login_at     TEXT"
            ")"
        )

        # Copy with explicit columns (avoid silent breakage if column order drifts).
        await self._d1.execute(
            "INSERT INTO users_new ("
            "id, email, password_hash, user_name, preferences, role, email_verified, "
            "status, approval_note, reviewed_at, reviewed_by, created_at, last_login_at"
            ") SELECT "
            "id, email, password_hash, user_name, preferences, role, email_verified, "
            "status, approval_note, reviewed_at, reviewed_by, created_at, last_login_at "
            "FROM users"
        )

        # Count migrated rows for logging.
        count_result = await self._d1.query("SELECT COUNT(*) AS n FROM users_new")
        migrated = count_result.rows[0]["n"] if count_result.rows else 0

        await self._d1.execute("DROP TABLE users")
        await self._d1.execute("ALTER TABLE users_new RENAME TO users")

        # Recreate indexes (DROP TABLE removes them with the old table).
        await self._d1.execute("CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)")
        await self._d1.execute("CREATE INDEX IF NOT EXISTS idx_users_status ON users(status)")
        await self._d1.execute(
            "CREATE INDEX IF NOT EXISTS idx_users_created_at ON users(created_at DESC)"
        )
        await self._d1.execute(
            "CREATE INDEX IF NOT EXISTS idx_users_pending_approval "
            "ON users(created_at DESC) WHERE status = 'pending_approval'"
        )
        await self._d1.execute(
            "CREATE INDEX IF NOT EXISTS idx_users_last_login_at ON users(last_login_at DESC)"
        )

        logger.info("Migrated D1 users table to add 'pro' role (%d rows)", migrated)

    async def _migrate_drop_api_keys_tier(self) -> None:
        """Drop the legacy ``api_keys.tier`` column when present.

        Mirrors ``ALTER TABLE api_keys DROP COLUMN IF EXISTS tier`` from
        postgres. SQLite 3.35+ supports ``ALTER TABLE ... DROP COLUMN`` and
        D1 runs SQLite 3.42+. Idempotent: skipped when column is already gone.
        """
        result = await self._d1.query("PRAGMA table_info(api_keys)")
        has_tier = any(r.get("name") == "tier" for r in result.rows)
        if not has_tier:
            return
        await self._d1.execute("ALTER TABLE api_keys DROP COLUMN tier")
        logger.info("Dropped legacy api_keys.tier column from D1")

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

    async def resume_user(
        self,
        user_id: str,
        *,
        admin_ip: str,
        admin_id: str,
        reason: str | None = None,
        email: str | None = None,
    ) -> None:
        """Resume a soft-deleted user atomically via D1 batch.

        Sets status='active' and writes the audit row.  Keys remain revoked.
        """
        now = _now_iso()
        details = json.dumps({"admin_id": admin_id, "email": email, "reason": reason})
        await self._d1.batch(
            [
                ("UPDATE users SET status = 'active' WHERE id = ?", [user_id]),
                (
                    "INSERT INTO admin_audit_log (timestamp, admin_ip, action, "
                    "target_user_id, details, success) VALUES (?, ?, ?, ?, ?, ?)",
                    [now, admin_ip, "resume_user", user_id, details, 1],
                ),
            ]
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
        """Permanently delete user row and operationally-linked rows.

        Executed atomically as a D1 batch.  D1 does not return per-statement
        row counts in the batch response — we return an empty dict and let
        the audit row record only ``admin_id``, ``email`` and ``reason``.

        ``api_logs`` lives in the configured LogStore; the caller must purge
        it (and ``email_broadcast_recipients``, when present in that store)
        separately via ``LogStore.hard_delete_user_data``.
        """
        now = _now_iso()
        details = json.dumps({"admin_id": admin_id, "email": email, "reason": reason})

        await self._d1.batch(
            [
                (
                    "DELETE FROM api_keys WHERE account_id = ? OR user_id = ?",
                    [user_id, user_id],
                ),
                ("DELETE FROM auth_sessions WHERE user_id = ?", [user_id]),
                ("DELETE FROM email_verification_tokens WHERE user_id = ?", [user_id]),
                ("DELETE FROM password_reset_tokens WHERE user_id = ?", [user_id]),
                ("DELETE FROM user_daily_cost WHERE user_id = ?", [user_id]),
                ("DELETE FROM admin_audit_log WHERE target_user_id = ?", [user_id]),
                ("DELETE FROM users WHERE id = ?", [user_id]),
                (
                    "INSERT INTO admin_audit_log (timestamp, admin_ip, action, "
                    "target_user_id, details, success) VALUES (?, ?, ?, ?, ?, ?)",
                    [now, admin_ip, "hard_delete_user", user_id, details, 1],
                ),
            ]
        )

        return {}

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
        # NEW filters (Phase 1 admin Users redesign)
        min_cost_today: Any = None,
        min_cost_month: Any = None,
        quota_state: Literal["near", "over", "custom", "default"] | None = None,
        provider: str | None = None,
        active_within_hours: int | None = None,
        anomaly: bool | None = None,
    ) -> tuple[int, list[Row], Row]:
        """Return (total_count, user_rows, status_counts_row).

        Cost-based sorts are not supported in D1 (api_logs is in Postgres).
        They fall back to created_at ordering; the caller enriches costs.

        New filters mirror the postgres variant; ``provider`` is a no-op in
        D1 (api_logs lives in Postgres) — callers running on D1 should not
        pass ``provider``. ``anomaly`` filters using ``user_daily_cost`` which
        D1 keeps locally (same rule as ``get_users_summary``).
        """
        where_clauses: list[str] = []
        params: list[Any] = []

        if status:
            where_clauses.append("u.status = ?")
            params.append(status)
        if search:
            # D1 LIKE patterns limited to 50 bytes; truncate search term
            truncated = search[:46]  # 46 + len("%%") = 48, safely under 50
            where_clauses.append(
                "(u.email LIKE ? OR u.user_name LIKE ? "
                "OR u.id LIKE ? "
                "OR EXISTS (SELECT 1 FROM api_keys k2 "
                "           WHERE k2.account_id = u.id "
                "             AND k2.status = 'active' "
                "             AND k2.key_prefix LIKE ?))"
            )
            params.extend([f"%{truncated}%", f"%{truncated}%", f"{truncated}%", f"{truncated}%"])
        if active_within_hours is not None:
            # SQLite stores last_login_at as TEXT (ISO 8601). Compare against
            # a strftime cutoff.
            where_clauses.append("u.last_login_at >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?)")
            params.append(f"-{int(active_within_hours)} hours")
        if quota_state == "default":
            where_clauses.append(
                "EXISTS (SELECT 1 FROM api_keys k3 WHERE k3.account_id = u.id "
                "AND k3.status = 'active' AND k3.quota_daily_cost_usd IS NULL)"
            )
        elif quota_state == "custom":
            where_clauses.append(
                "EXISTS (SELECT 1 FROM api_keys k3 WHERE k3.account_id = u.id "
                "AND k3.status = 'active' AND k3.quota_daily_cost_usd IS NOT NULL)"
            )

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
            f"k.key_prefix, k.status AS key_status "
            f"FROM users u "
            f"LEFT JOIN api_keys k ON k.account_id = u.id AND k.status = 'active' "
            f"{where_sql} "
            f"ORDER BY {order_clause} "
            f"LIMIT ? OFFSET ?",
            query_params,
        )

        result_rows: list[Row] = list(rows_result.rows)

        # Post-query quota_state near/over: needs today's cost vs quota.
        # D1 keeps user_daily_cost so today's cost is available locally.
        if quota_state in ("near", "over") and result_rows:
            user_ids2 = [r["id"] for r in result_rows]
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            placeholders2 = ",".join(["?"] * len(user_ids2))
            cost_result = await self._d1.query(
                f"SELECT user_id, COALESCE(SUM(cost_usd), 0) AS cost "
                f"FROM user_daily_cost "
                f"WHERE day = ? AND user_id IN ({placeholders2}) "
                f"GROUP BY user_id",
                [today, *user_ids2],
            )
            today_costs = {r["user_id"]: float(r["cost"]) for r in cost_result.rows}

            quota_result = await self._d1.query(
                f"SELECT account_id, quota_daily_cost_usd FROM api_keys "
                f"WHERE account_id IN ({placeholders2}) AND status = 'active'",
                user_ids2,
            )
            quotas = {q["account_id"]: q["quota_daily_cost_usd"] for q in quota_result.rows}

            filtered: list[Row] = []
            for r in result_rows:
                quota = quotas.get(r["id"])
                if not quota or float(quota) <= 0:
                    continue
                pct = today_costs.get(r["id"], 0.0) / float(quota)
                if (quota_state == "near" and pct >= 0.80) or (
                    quota_state == "over" and pct >= 1.0
                ):
                    filtered.append(r)
            result_rows = filtered

        # min_cost_today / min_cost_month: enrich from user_daily_cost,
        # then apply the threshold.
        if (min_cost_today is not None or min_cost_month is not None) and result_rows:
            from decimal import Decimal as _Decimal

            user_ids3 = [r["id"] for r in result_rows]
            placeholders3 = ",".join(["?"] * len(user_ids3))
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            month_prefix = datetime.now(timezone.utc).strftime("%Y-%m")

            today_map: dict[str, _Decimal] = {}
            month_map: dict[str, _Decimal] = {}

            if min_cost_today is not None:
                today_result = await self._d1.query(
                    f"SELECT user_id, COALESCE(SUM(cost_usd), 0) AS cost "
                    f"FROM user_daily_cost WHERE day = ? AND user_id IN ({placeholders3}) "
                    f"GROUP BY user_id",
                    [today, *user_ids3],
                )
                today_map = {r["user_id"]: _Decimal(str(r["cost"])) for r in today_result.rows}
                result_rows = [
                    r
                    for r in result_rows
                    if today_map.get(r["id"], _Decimal("0")) >= _Decimal(str(min_cost_today))
                ]

            if min_cost_month is not None and result_rows:
                user_ids4 = [r["id"] for r in result_rows]
                placeholders4 = ",".join(["?"] * len(user_ids4))
                month_result = await self._d1.query(
                    f"SELECT user_id, COALESCE(SUM(cost_usd), 0) AS cost "
                    f"FROM user_daily_cost WHERE day LIKE ? AND user_id IN ({placeholders4}) "
                    f"GROUP BY user_id",
                    [f"{month_prefix}%", *user_ids4],
                )
                month_map = {r["user_id"]: _Decimal(str(r["cost"])) for r in month_result.rows}
                result_rows = [
                    r
                    for r in result_rows
                    if month_map.get(r["id"], _Decimal("0")) >= _Decimal(str(min_cost_month))
                ]

        # provider filter is a no-op in D1: api_logs lives in Postgres in this
        # hybrid deployment. Callers running on the D1 stack should not pass
        # ``provider`` — log a warning if they do but don't error.
        if provider:
            logger.warning(
                "list_users(provider=%r) requested on D1 store — "
                "api_logs is in Postgres; ignoring provider filter.",
                provider,
            )

        # Anomaly filter — same rule as get_users_summary:
        #   today >= $1 AND days_with_history >= 3 AND today >= 5x avg_prior_7d.
        # ``user_daily_cost`` lives in D1 so we can compute this locally.
        if anomaly and result_rows:
            from decimal import Decimal as _Decimal

            anomaly_multiplier = _Decimal("5.0")
            anomaly_min_today = _Decimal("1.00")
            anomaly_min_history_days = 3

            today = datetime.now(timezone.utc).date().isoformat()
            prior_7d_start = (datetime.now(timezone.utc).date() - timedelta(days=7)).isoformat()
            prior_7d_end = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()

            user_ids_a = [r["id"] for r in result_rows]
            placeholders_a = ",".join(["?"] * len(user_ids_a))

            today_result = await self._d1.query(
                f"SELECT user_id, COALESCE(SUM(cost_usd), 0) AS today_cost "
                f"FROM user_daily_cost WHERE day = ? AND user_id IN ({placeholders_a}) "
                f"GROUP BY user_id",
                [today, *user_ids_a],
            )
            today_costs = {
                r["user_id"]: _Decimal(str(r["today_cost"] or 0)) for r in today_result.rows
            }

            prior_result = await self._d1.query(
                f"SELECT user_id, "
                f"       COALESCE(SUM(cost_usd), 0) AS total, "
                f"       COUNT(DISTINCT day) AS days_with_history "
                f"FROM user_daily_cost "
                f"WHERE day BETWEEN ? AND ? AND user_id IN ({placeholders_a}) "
                f"GROUP BY user_id",
                [prior_7d_start, prior_7d_end, *user_ids_a],
            )
            prior_stats = {
                r["user_id"]: (
                    _Decimal(str(r["total"] or 0)),
                    int(r["days_with_history"] or 0),
                )
                for r in prior_result.rows
            }

            anomalous: list[Row] = []
            for r in result_rows:
                t = today_costs.get(r["id"], _Decimal("0"))
                prior_total, days = prior_stats.get(r["id"], (_Decimal("0"), 0))
                avg_7d = (prior_total / days) if days > 0 else _Decimal("0")
                if (
                    days >= anomaly_min_history_days
                    and t >= anomaly_min_today
                    and avg_7d > 0
                    and t >= anomaly_multiplier * avg_7d
                ):
                    anomalous.append(r)
            result_rows = anomalous

        if (
            min_cost_today is not None
            or min_cost_month is not None
            or quota_state in ("near", "over")
            or anomaly
        ):
            total = len(result_rows)

        return total, result_rows, status_counts

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
            "WHERE status = 'active' AND last_login_at >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-24 hours')"
        )
        mau_r = await self._d1.query(
            "SELECT COUNT(DISTINCT id) as count FROM users "
            "WHERE status = 'active' AND last_login_at >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-30 days')"
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
            "SELECT k.id, k.user_id, k.user_name, k.quota_daily_cost_usd, "
            "u.email, u.role, u.email_verified "
            "FROM api_keys k "
            "LEFT JOIN users u ON u.id = k.user_id "
            "WHERE k.key_hash = ? "
            "  AND k.status = 'active' "
            "  AND (k.expires_at IS NULL OR k.expires_at > strftime('%Y-%m-%dT%H:%M:%SZ', 'now')) "
            "  AND u.id IS NOT NULL AND u.status = 'active'",
            [key_hash],
        )
        return _parse_row_timestamps(result.rows[0]) if result.rows else None

    async def get_auth_context_lightweight(self, key_hash: str) -> Row | None:
        """Lightweight identity lookup."""
        result = await self._d1.query(
            "SELECT k.user_id, u.email, u.role, u.email_verified "
            "FROM api_keys k "
            "LEFT JOIN users u ON u.id = k.user_id "
            "WHERE k.key_hash = ? "
            "  AND k.status = 'active' "
            "  AND (k.expires_at IS NULL OR k.expires_at > strftime('%Y-%m-%dT%H:%M:%SZ', 'now')) "
            "  AND u.id IS NOT NULL AND u.status = 'active'",
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
            "(key_hash, key_prefix, user_id, user_name, "
            "quota_daily_cost_usd, quota_monthly_cost_usd, "
            "expires_at, notes, metadata, account_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "RETURNING id, created_at",
            [
                key_hash,
                key_prefix,
                user_id,
                user_name,
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
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[int, list[Row]]:
        """Return (total_count, key_rows) with optional filters."""
        where_clauses: list[str] = []
        params: list[Any] = []

        if status:
            where_clauses.append("status = ?")
            params.append(status)

        where_sql = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""

        count_result = await self._d1.query(
            f"SELECT COUNT(*) as total FROM api_keys {where_sql}",
            params,
        )
        total = count_result.rows[0]["total"] if count_result.rows else 0

        query_params = [*params, limit, offset]
        rows_result = await self._d1.query(
            f"SELECT user_id, user_name, key_prefix, status, "
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
            "SELECT user_id, user_name, key_prefix, status, "
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
            "SELECT id FROM api_keys "
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
                logger.debug("Malformed preferences payload for user %s", user_id)
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
            day_param: Any = day_filter
        else:
            month_prefix = datetime.now(timezone.utc).strftime("%Y-%m")
            where = "day LIKE ?"
            day_param = f"{month_prefix}%"

        # D1 limits to 100 bound parameters; 1 slot is used by the day/month filter.
        _CHUNK = 99
        result_map: dict[str, float] = {}
        for i in range(0, len(user_ids), _CHUNK):
            chunk = user_ids[i : i + _CHUNK]
            placeholders = ",".join(["?"] * len(chunk))
            params: list[Any] = [day_param, *chunk]
            result = await self._d1.query(
                f"SELECT user_id, COALESCE(SUM(cost_usd), 0) as cost "
                f"FROM user_daily_cost WHERE {where} AND user_id IN ({placeholders}) "
                f"GROUP BY user_id",
                params,
            )
            for r in result.rows:
                result_map[r["user_id"]] = float(r["cost"])
        return result_map

    async def get_user_cost_history(
        self,
        user_id: str,
        days: int = 7,
    ) -> list[Row]:
        """Return up to ``days`` of daily cost rows for ``user_id`` (D1 variant).

        Output mirrors the postgres variant: list of {"day", "cost_usd",
        "requests"} ordered ascending by day.
        """
        if days <= 0:
            return []
        from decimal import Decimal as _Decimal

        result = await self._d1.query(
            "SELECT day, cost_usd, requests "
            "FROM user_daily_cost "
            "WHERE user_id = ? "
            "  AND day >= date('now', ?) "
            "ORDER BY day ASC",
            [user_id, f"-{days - 1} days"],
        )
        return [
            {
                "day": r["day"],
                "cost_usd": _Decimal(str(r["cost_usd"])),
                "requests": r["requests"],
            }
            for r in result.rows
        ]

    async def get_bulk_user_cost_history(
        self,
        user_ids: list[str],
        days: int = 7,
    ) -> dict[str, list[Row]]:
        """Bulk variant — chunked to respect the 100-bound-param D1 limit."""
        if not user_ids or days <= 0:
            return {}
        from decimal import Decimal as _Decimal

        # 1 slot for the date offset; chunk users to fit under 100 binds.
        _CHUNK = 99
        out: dict[str, list[Row]] = {uid: [] for uid in user_ids}
        date_offset = f"-{days - 1} days"
        for i in range(0, len(user_ids), _CHUNK):
            chunk = user_ids[i : i + _CHUNK]
            placeholders = ",".join(["?"] * len(chunk))
            result = await self._d1.query(
                f"SELECT user_id, day, cost_usd, requests "
                f"FROM user_daily_cost "
                f"WHERE user_id IN ({placeholders}) "
                f"  AND day >= date('now', ?) "
                f"ORDER BY user_id, day ASC",
                [*chunk, date_offset],
            )
            for r in result.rows:
                out[r["user_id"]].append(
                    {
                        "day": r["day"],
                        "cost_usd": _Decimal(str(r["cost_usd"])),
                        "requests": r["requests"],
                    }
                )
        return out

    async def get_users_summary(
        self,
        *,
        top_n: int = 5,
        anomaly_multiplier: float = 5.0,
        anomaly_min_today: Any = None,
        anomaly_min_history_days: int = 3,
        near_quota_pct: float = 0.80,
    ) -> Row:
        """D1/SQLite variant of get_users_summary.

        Mirrors the postgres method but uses ``date('now', ...)`` and
        SQLite-flavoured boolean comparisons.
        """
        from decimal import Decimal as _Decimal

        if anomaly_min_today is None:
            anomaly_min_today = _Decimal("1.00")

        # Pending count + top
        pending_count_result = await self._d1.query(
            "SELECT COUNT(*) AS c FROM users WHERE status = 'pending_approval'"
        )
        pending_count = pending_count_result.rows[0]["c"] if pending_count_result.rows else 0
        pending_result = await self._d1.query(
            "SELECT id, email, user_name, role, created_at "
            "FROM users WHERE status = 'pending_approval' "
            "ORDER BY created_at DESC LIMIT ?",
            [top_n],
        )

        # Today / 7d-avg per active user.
        usage_result = await self._d1.query(
            """
            WITH today_costs AS (
                SELECT user_id, COALESCE(SUM(cost_usd), 0) AS today_cost
                FROM user_daily_cost
                WHERE day = date('now')
                GROUP BY user_id
            ),
            prior_7d AS (
                SELECT user_id,
                       COALESCE(SUM(cost_usd), 0) AS total,
                       COUNT(DISTINCT day) AS days_with_history
                FROM user_daily_cost
                WHERE day BETWEEN date('now', '-7 days') AND date('now', '-1 days')
                GROUP BY user_id
            )
            SELECT u.id, u.email, u.user_name, u.role,
                   COALESCE(t.today_cost, 0) AS today_cost,
                   COALESCE(p.total, 0) AS prior_7d_total,
                   COALESCE(p.days_with_history, 0) AS days_with_history,
                   k.quota_daily_cost_usd
            FROM users u
            LEFT JOIN today_costs t ON t.user_id = u.id
            LEFT JOIN prior_7d p ON p.user_id = u.id
            LEFT JOIN api_keys k ON k.account_id = u.id AND k.status = 'active'
            WHERE u.status = 'active'
            """
        )

        top_spenders: list[Row] = []
        anomalies: list[Row] = []
        near_quota: list[Row] = []

        for r in usage_result.rows:
            today = _Decimal(str(r["today_cost"] or 0))
            prior = _Decimal(str(r["prior_7d_total"] or 0))
            days = int(r["days_with_history"] or 0)
            avg_7d = (prior / days) if days > 0 else _Decimal("0")
            quota = r["quota_daily_cost_usd"]

            base_item = {
                "id": r["id"],
                "email": r["email"],
                "user_name": r["user_name"],
                "role": r["role"] or "free",
                "today_cost_usd": today,
                "avg_prior_7d_usd": avg_7d,
                "quota_daily_usd": float(quota) if quota else None,
                "multiplier": None,
            }

            if today > 0:
                top_spenders.append(dict(base_item))

            if (
                days >= anomaly_min_history_days
                and today >= anomaly_min_today
                and avg_7d > 0
                and today >= _Decimal(str(anomaly_multiplier)) * avg_7d
            ):
                multiplier = float(today / avg_7d) if avg_7d > 0 else None
                anomaly_item = dict(base_item)
                anomaly_item["multiplier"] = multiplier
                anomalies.append(anomaly_item)

            if quota and float(quota) > 0:
                pct = float(today) / float(quota)
                if pct >= near_quota_pct:
                    near_quota.append(dict(base_item))

        top_spenders.sort(key=lambda x: x["today_cost_usd"], reverse=True)
        anomalies.sort(key=lambda x: x["multiplier"] or 0, reverse=True)
        near_quota.sort(
            key=lambda x: (
                float(x["today_cost_usd"]) / x["quota_daily_usd"] if x["quota_daily_usd"] else 0
            ),
            reverse=True,
        )

        return {
            "pending": {
                "count": pending_count,
                "top": [
                    {
                        "id": r["id"],
                        "email": r["email"],
                        "user_name": r["user_name"],
                        "role": r["role"] or "free",
                        "today_cost_usd": _Decimal("0"),
                        "avg_prior_7d_usd": _Decimal("0"),
                        "quota_daily_usd": None,
                        "multiplier": None,
                    }
                    for r in pending_result.rows
                ],
            },
            "top_spenders_today": {
                "count": len([s for s in top_spenders if s["today_cost_usd"] > 0]),
                "top": top_spenders[:top_n],
            },
            "anomalies": {
                "count": len(anomalies),
                "top": anomalies[:top_n],
            },
            "near_quota": {
                "count": len(near_quota),
                "top": near_quota[:top_n],
            },
        }

    # -- signup domain allowlist --------------------------------------------

    async def list_signup_allowed_domains(self) -> list[Row]:
        """Return all allowlist rows joined with the creator's email."""
        result = await self._d1.query(
            "SELECT d.domain, d.is_wildcard, d.created_at, d.created_by, "
            "u.email AS created_by_email "
            "FROM signup_allowed_domains d "
            "LEFT JOIN users u ON u.id = d.created_by "
            "ORDER BY d.created_at DESC, d.domain ASC"
        )
        rows: list[Row] = []
        for raw in result.rows:
            row = dict(raw)
            row["is_wildcard"] = bool(row.get("is_wildcard"))
            if isinstance(row.get("created_at"), str):
                row["created_at"] = _iso_to_dt(row["created_at"])
            rows.append(row)
        return rows

    async def add_signup_allowed_domain(
        self,
        *,
        domain: str,
        is_wildcard: bool,
        created_by: str | None,
    ) -> Row:
        """Insert a new allowlist entry; raises on duplicate composite key."""
        await self._d1.execute(
            "INSERT INTO signup_allowed_domains (domain, is_wildcard, created_by) VALUES (?, ?, ?)",
            [domain, int(is_wildcard), created_by],
        )
        result = await self._d1.query(
            "SELECT d.domain, d.is_wildcard, d.created_at, d.created_by, "
            "u.email AS created_by_email "
            "FROM signup_allowed_domains d "
            "LEFT JOIN users u ON u.id = d.created_by "
            "WHERE d.domain = ? AND d.is_wildcard = ?",
            [domain, int(is_wildcard)],
        )
        if not result.rows:
            return {
                "domain": domain,
                "is_wildcard": is_wildcard,
                "created_at": None,
                "created_by": created_by,
                "created_by_email": None,
            }
        row = dict(result.rows[0])
        row["is_wildcard"] = bool(row.get("is_wildcard"))
        if isinstance(row.get("created_at"), str):
            row["created_at"] = _iso_to_dt(row["created_at"])
        return row

    async def remove_signup_allowed_domain(self, *, domain: str, is_wildcard: bool) -> bool:
        """Delete an allowlist entry; returns True if a row was removed."""
        # D1 returns no row count from execute; check existence first.
        existing = await self._d1.query(
            "SELECT 1 FROM signup_allowed_domains WHERE domain = ? AND is_wildcard = ? LIMIT 1",
            [domain, int(is_wildcard)],
        )
        if not existing.rows:
            return False
        await self._d1.execute(
            "DELETE FROM signup_allowed_domains WHERE domain = ? AND is_wildcard = ?",
            [domain, int(is_wildcard)],
        )
        return True

    async def signup_allowlist_is_empty(self) -> bool:
        """Return True if the allowlist table has no rows."""
        result = await self._d1.query("SELECT 1 FROM signup_allowed_domains LIMIT 1")
        return not result.rows

    async def is_signup_domain_allowed(self, email: str) -> bool:
        """Match *email*'s domain against the allowlist (exact or wildcard).

        Uses a single round-trip with a dynamic ``IN (?, ?, …)`` over the
        email domain plus each parent suffix that could be a wildcard
        match. The bare top-level domain is excluded from wildcard
        candidates so ``*.acme.com`` does not match ``alice@acme.com``.
        """
        if "@" not in email:
            return False
        domain = email.rsplit("@", 1)[1].strip().lower()
        if not domain:
            return False

        parts = domain.split(".")
        wildcard_candidates = [".".join(parts[i:]) for i in range(1, len(parts) - 1)]
        candidates = list({domain, *wildcard_candidates})
        placeholders = ", ".join(["?"] * len(candidates))
        result = await self._d1.query(
            f"SELECT domain, is_wildcard FROM signup_allowed_domains "
            f"WHERE domain IN ({placeholders})",
            candidates,
        )
        wildcard_set = set(wildcard_candidates)
        for row in result.rows:
            row_domain = row.get("domain")
            row_is_wildcard = bool(row.get("is_wildcard"))
            if not row_is_wildcard and row_domain == domain:
                return True
            if row_is_wildcard and row_domain in wildcard_set:
                return True
        return False
