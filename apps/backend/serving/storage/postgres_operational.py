"""PostgreSQL implementation of OperationalStore.

Consolidates all user, API-key, session, token, and audit SQL that was
previously scattered across auth.py, admin.py, auth_routes.py,
user_routes.py, internal.py, and user_stats.py.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from datetime import UTC, timedelta
from typing import TYPE_CHECKING, Any, Literal

from serving import quota
from serving.audit import AuditTarget, audit_columns
from serving.config.settings import ROLE_RANK, VALID_ROLES, get_settings
from serving.exceptions import (
    AmbiguousAPIKeyError,
    APIKeyLimitReachedError,
    HardDeleteRecoveryAvailable,
    HardDeleteStateChanged,
    PreferencesTooLargeError,
    ProjectNotFoundError,
    SelfDeleteRefused,
)
from serving.storage import user_usage_rollup
from serving.storage.audit_schema import (
    SELECT_COLUMNS as AUDIT_SELECT_COLUMNS,
    insert_audit_row,
)
from serving.storage.base import (
    USER_PREFERENCES_MAX_BYTES,
    CostIncrement,
    CostWriteOutcomeUnknown,
    HardDeleteClaim,
    HardDeleteClaimProvenance,
    HardDeleteRecoveryMode,
    OperationalStore,
    ProviderDefinitionRow,
    ProviderKeyRow,
    ResetRedemption,
    Row,
)
from serving.storage.erasure_fence import fence_account_digest, lock_fence_keys_shared
from serving.storage.phone_auth import PostgresPhoneAuthMixin
from serving.storage.pools import SingleConnectionPool, request_connection
from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence
    from contextlib import AbstractAsyncContextManager
    from datetime import datetime
    from decimal import Decimal

    import asyncpg

    from serving.audit import AuditActor

logger = get_logger(__name__)

# How long a hard-delete claim's lease may go unrenewed before another caller
# may take it over. Takeover is deliberately not immediate: a worker that is
# merely slow must not lose its claim to an ordinary retry, and the admin
# endpoint additionally requires an explicit operator opt-in. It bounds how
# long a worker that crashed before the erasure fence leaves the account
# un-resumable.
_HARD_DELETE_CLAIM_RECOVERY_GRACE = timedelta(hours=1)

# Adds a batch of cost increments to the account counters and the key counters
# in one statement, so both are written or neither, and returns the
# transaction id that ``increment_user_costs`` asks about when the COMMIT's
# answer is lost. Each target row is updated once: the batch is summed per
# (user, day) and per (key, day) first, as ON CONFLICT DO UPDATE requires.
# The sums keep each increment's rounding (``spend_ledger.counted_amount``), so
# a coalesced batch adds exactly what the separate increments would have.
# The counter writes of a cost batch, from its ``batch`` rows: shared by both
# statements below.
_COST_INCREMENT_WRITES = """
account_rows AS (
    INSERT INTO user_daily_cost AS c (user_id, day, cost_usd, requests, last_request_at)
    SELECT user_id, day, SUM(cost_usd), SUM(requests), NOW()
    FROM batch
    GROUP BY user_id, day
    ON CONFLICT (user_id, day) DO UPDATE SET
        cost_usd = c.cost_usd + EXCLUDED.cost_usd,
        requests = c.requests + EXCLUDED.requests,
        last_request_at = NOW()
),
key_rows AS (
    INSERT INTO api_key_daily_cost AS k (key_id, day, user_id, cost_usd, requests, last_request_at)
    SELECT key_id, day, MIN(user_id), SUM(cost_usd), SUM(requests), NOW()
    FROM batch
    WHERE key_id IS NOT NULL
    GROUP BY key_id, day
    ON CONFLICT (key_id, day) DO UPDATE SET
        cost_usd = k.cost_usd + EXCLUDED.cost_usd,
        requests = k.requests + EXCLUDED.requests,
        last_request_at = NOW()
),
scope_rows AS (
    INSERT INTO spend_counters AS c (scope_id, period, cost_usd, requests, last_request_at)
    SELECT scope_id, period, SUM(cost_usd), SUM(requests), NOW()
    FROM batch
    CROSS JOIN LATERAL (VALUES (project_id)) scopes(scope_id)
    CROSS JOIN LATERAL (VALUES ('d:' || day), ('m:' || left(day, 7))) periods(period)
    WHERE scope_id IS NOT NULL
    GROUP BY scope_id, period
    ON CONFLICT (scope_id, period) DO UPDATE SET
        cost_usd = c.cost_usd + EXCLUDED.cost_usd,
        requests = c.requests + EXCLUDED.requests,
        last_request_at = NOW()
)
SELECT txid_current()
"""
COST_INCREMENTS_SQL = (
    """
WITH batch AS (
    SELECT *
    FROM unnest($1::text[], $2::text[], $3::bigint[], $4::numeric[], $5::integer[], $6::text[], $7::text[])
        AS b(user_id, day, key_id, cost_usd, requests, org_id, project_id)
),"""
    + _COST_INCREMENT_WRITES
)

# The same, with each increment's erasure-fence digest ($8, NULL for none): an
# increment whose account was hard-deleted is left out (see
# ``PostgresOperationalStore.increment_user_costs``).
COST_INCREMENTS_FENCED_SQL = (
    """
WITH batch AS (
    SELECT b.user_id, b.day, b.key_id, b.cost_usd, b.requests, b.org_id, b.project_id
    FROM unnest($1::text[], $2::text[], $3::bigint[], $4::numeric[], $5::integer[], $6::text[], $7::text[], $8::text[])
        AS b(user_id, day, key_id, cost_usd, requests, org_id, project_id, fence_key)
    WHERE NOT EXISTS (SELECT 1 FROM erasure_fence AS f WHERE f.account_digest = b.fence_key)
),"""
    + _COST_INCREMENT_WRITES
)

# How long to keep asking how a cost transaction ended after its COMMIT's
# answer was lost: the delays before each try.
_COMMIT_STATUS_RETRY_DELAYS = (0.0, 0.2, 1.0, 3.0)

# The most accounts' shared fence locks one cost write takes. Every lock is an
# entry in PostgreSQL's shared lock table, which holds about 64 per connection
# (max_locks_per_transaction); a coalesced batch past this checks the fence in
# its statement alone rather than risk filling the table and failing every
# retry.
_COST_FENCE_LOCKS_MAX = 512

# Distinct ``api_logs.provider`` values with a row in the last $1 days; see
# ``PostgresOperationalStore.list_user_activity_providers``.
ACTIVITY_PROVIDERS_SQL = """
WITH RECURSIVE seen(provider) AS (
    (SELECT provider FROM api_logs WHERE provider > ''
     ORDER BY provider LIMIT 1)
    UNION ALL
    SELECT (SELECT l.provider FROM api_logs l WHERE l.provider > seen.provider
            ORDER BY l.provider LIMIT 1)
    FROM seen
    WHERE seen.provider IS NOT NULL
)
SELECT provider FROM seen
WHERE provider IS NOT NULL
  AND EXISTS (
      SELECT 1 FROM api_logs l
      WHERE l.provider = seen.provider
        AND l.timestamp >= NOW() - make_interval(days => $1)
  )
ORDER BY provider
"""


def _preferences_subset(column: str, *keys: str) -> str:
    """SQL for the entries *keys* of the preferences *column*, as a JSON object.

    A key the document lacks is left out, as is every key when the column
    holds no object (``->`` yields NULL there, and the nulls are stripped).
    """
    pairs = ", ".join(f"'{key}', {column} -> '{key}'" for key in keys)
    return f"jsonb_strip_nulls(jsonb_build_object({pairs}))"


# The preferences entries a request acts on: the models an administrator has
# disabled for the account (serving.model_access.DISABLED_MODELS_PREFERENCE_KEY)
# and, for the console, its interface language. The lookups every request makes
# select these and never the whole document. Its other entries (the prober
# layout) are the account holder's to fill, and decoding them on every request,
# and copying them on every cache hit, would put work in proportion to their
# size on the event loop each time.
_REQUEST_PREFERENCE_KEYS = ("disabled_models", "locale")
_USER_ROW_PREFERENCES = _preferences_subset("preferences", *_REQUEST_PREFERENCE_KEYS)
_KEY_AUTH_PREFERENCES = f"{_preferences_subset('u.preferences', 'disabled_models')} AS preferences"

# Columns of a single-user lookup (get_user_by_id / _by_email / _by_login_name),
# in select order.
_USER_COLUMNS = (
    "id",
    "email",
    "login_name",
    "phone",
    "phone_verified",
    "user_name",
    "role",
    "status",
    "email_verified",
    "created_at",
    "last_login_at",
    "password_hash",
    "preferences",
    "max_concurrent_requests",
    "signup_reason",
    "admin_note",
    "suspension_message",
)
_USER_SELECT = ", ".join(
    f"{_USER_ROW_PREFERENCES} AS preferences" if column == "preferences" else column
    for column in _USER_COLUMNS
)

# A stored preferences document this large (its stored size, which reading
# costs nothing) is one written before writes were bounded; a whole-document
# read takes the request entries alone from it. Far above
# USER_PREFERENCES_MAX_BYTES, so an administrator's entries added to a full
# document never push it over.
_PREFERENCES_READ_MAX_BYTES = 4 * USER_PREFERENCES_MAX_BYTES

# The preferences column as a JSON object, whatever an older row holds there
# (SQL NULL, or a JSON scalar), so a key can be set on it or removed from it.
_PREFERENCES_OBJECT = (
    "(CASE WHEN jsonb_typeof(preferences) = 'object' THEN preferences ELSE '{}'::jsonb END)"
)

# What a stored `chosen_at` must look like before it is cast to timestamptz.
_ISO_TIMESTAMP_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$"

# The identity columns the API-key and browser-session lookups take from the
# joined users row.
_JOINED_USER_IDENTITY = "u.email, u.login_name, u.phone, u.phone_verified"

# Serializes first-run setup claims across every process sharing the database.
_FIRST_RUN_SETUP_LOCK_SQL = (
    "SELECT pg_advisory_xact_lock(hashtext('hybridinference:first-run-setup'))"
)


def _parse_command_tag_count(command_tag: str) -> int:
    """Extract the affected row count from an asyncpg command tag."""
    parts = command_tag.split()
    if not parts:
        return 0
    try:
        return int(parts[-1])
    except ValueError:
        return 0


def _parse_admin_emails(raw: str) -> list[str]:
    """Return normalized admin email addresses from a comma-separated env var."""
    return [email.strip().lower() for email in raw.split(",") if email.strip()]


def _decode_grant(row: Any) -> Row:
    """Convert a grant row to a dict, decoding its two JSONB scope columns.

    Same reason as ``_coerce_user_row``: without a registered codec asyncpg
    hands back JSONB as a string, and every caller here asks "is this model in
    the list" — a question a string answers wrongly rather than loudly.
    """
    decoded = dict(row)
    for field in ("allowed_models",):
        value = decoded.get(field)
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                parsed = []
            decoded[field] = parsed if isinstance(parsed, list) else []
        elif not isinstance(value, list):
            decoded[field] = []
    return decoded


#: The columns a new key is inserted with.
_KEY_INSERT_COLUMNS = (
    "key_hash, key_prefix, user_id, user_name, quota_daily_cost_usd, "
    "quota_monthly_cost_usd, expires_at, notes, metadata, account_id, "
    "api_key_encrypted, name, org_id, project_id"
)
_KEY_INSERT_VALUES = "$1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10, $11, $12, $13, $14"

#: A key's own limits (P2a), on top of its account's quota. NULL means no limit
#: at the key.
_KEY_LIMIT_COLUMNS = "k.budget_daily_usd, k.budget_monthly_usd, k.allowed_models"

#: What the auth lookups add about the presented key itself: its limits, and
#: how a refusal names it.
_AUTH_KEY_COLUMNS = f"k.key_prefix, k.name AS key_name, {_KEY_LIMIT_COLUMNS}"

#: The settings an account's owner may change on one of its keys.
KEY_SETTINGS_COLUMNS = ("name", "budget_daily_usd", "budget_monthly_usd", "allowed_models")

#: What key ``k`` spent on the current UTC day and month, from
#: ``api_key_daily_cost``, read only for a key with a budget: at most a month of
#: counter rows, through the counter's primary key. The day strings are the
#: counter write path's, so a budget and its counter bucket midnight alike.
_KEY_SPEND_JOIN = (
    "LEFT JOIN LATERAL ("
    "  SELECT COALESCE(SUM(c.cost_usd) FILTER ("
    "           WHERE c.day = to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM-DD')), 0) "
    "           AS key_spent_today_usd, "
    "         COALESCE(SUM(c.cost_usd), 0) AS key_spent_month_usd "
    "  FROM api_key_daily_cost c "
    "  WHERE c.key_id = k.id "
    "    AND (k.budget_daily_usd IS NOT NULL OR k.budget_monthly_usd IS NOT NULL) "
    "    AND c.day >= to_char(date_trunc('month', NOW() AT TIME ZONE 'UTC'), 'YYYY-MM-DD')"
    ") ks ON TRUE "
)

#: What the owner of key ``k`` spent this UTC month, from ``user_daily_cost``,
#: read only when the account has a monthly quota: at most a month of counter
#: rows through the counter's primary key. Same days ``get_user_cost_period``
#: sums, so the gate and the console agree.
_ACCOUNT_MONTH_SPEND_JOIN = (
    "LEFT JOIN LATERAL ("
    "  SELECT COALESCE(SUM(m.cost_usd), 0) AS account_spent_month_usd "
    "  FROM user_daily_cost m "
    "  WHERE m.user_id = k.user_id "
    "    AND COALESCE(o.budget_monthly_usd, k.quota_monthly_cost_usd) IS NOT NULL "
    "    AND m.day >= to_char(date_trunc('month', NOW() AT TIME ZONE 'UTC'), 'YYYY-MM-DD')"
    ") am ON TRUE "
)


#: The key's project and organization: where the request is attributed, and the
#: budgets above the key. The org ceiling falls back to the legacy key quota
#: until every organization has one, so a half-migrated database keeps the
#: limit it had.
_PROJECT_SCOPE_COLUMNS = (
    "k.project_id, k.org_id, p.name AS project_name, o.name AS org_name, "
    "p.budget_daily_usd AS project_budget_daily_usd, "
    "p.budget_monthly_usd AS project_budget_monthly_usd, "
    "p.allowed_models AS project_allowed_models, "
    "COALESCE(o.budget_daily_usd, k.quota_daily_cost_usd) AS quota_daily_cost_usd, "
    "COALESCE(o.budget_monthly_usd, k.quota_monthly_cost_usd) AS quota_monthly_cost_usd, "
    "o.status AS org_status, "
    "p.max_concurrent_requests AS project_max_concurrent_requests, "
    "o.max_concurrent_requests AS org_max_concurrent_requests"
)

_PROJECT_SCOPE_JOIN = (
    "LEFT JOIN projects p ON p.id = k.project_id AND p.status = 'active' "
    "LEFT JOIN organizations o ON o.id = COALESCE(p.org_id, k.org_id) "
)

#: Daily and monthly spend for the key's project, read from ``spend_counters``
#: through its primary key. Project counters are written at settlement; the
#: account's own counters stay in ``user_daily_cost``.
_SCOPE_SPEND_COLUMNS = (
    "COALESCE(ps.cost_usd, 0) AS project_spent_today_usd, "
    "COALESCE(pm.cost_usd, 0) AS project_spent_month_usd"
)

_SCOPE_SPEND_JOIN = (
    "LEFT JOIN spend_counters ps "
    "  ON ps.scope_id = k.project_id "
    " AND ps.period = 'd:' || to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM-DD') "
    "LEFT JOIN spend_counters pm "
    "  ON pm.scope_id = k.project_id "
    " AND pm.period = 'm:' || to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM') "
)

#: Postgres types the project and organization write paths cast their bound
#: values to. A bare ``SET status = $1`` leaves the parameter's type unknown
#: to asyncpg's prepare step, so every assignment names it.
_COLUMN_CASTS = {
    "name": "text",
    "description": "text",
    "status": "text",
    "budget_daily_usd": "numeric",
    "budget_monthly_usd": "numeric",
    "max_concurrent_requests": "integer",
    "allowed_models": "text[]",
}

#: Advisory lock key held by every API-key write: shared by a write to one
#: account's keys, exclusive by a write that spans accounts. The per-account
#: keys are ``'hybridinference:api-keys:' || account_id``.
_ALL_ACCOUNT_KEYS_LOCK = "hashtextextended('hybridinference:api-keys', 0)"


async def _refuse_claimed_account(conn: asyncpg.Connection, user_id: str) -> None:
    """Refuse a write to an account under a hard-delete claim.

    Raises ``HardDeleteStateChanged`` for a claimed account; returns
    otherwise, and the caller's own statement still decides whether the
    account existed.

    The account row is read under the row lock, so the answer is taken with
    the lock the write would take anyway: a claim installed after this read
    cannot race the write, because the claim itself takes the same lock. A
    missing row is not a claim, and a claim cannot be installed on a row that
    is not there.

    The account-mutating writers that do not read the account row themselves
    use this, so that a pending erasure claim refuses the write whatever
    column it names — the account is being erased, so nothing about it may
    change. This matches every other store, which refuses any work for an
    account marked ``hard_delete_pending`` (see ``batch_store._lock_owner``).

    Not every write to the ``users`` table goes through here. The state
    machine's own writes (``update_user_fields``, ``suspend_user``,
    ``delete_user``, ``resume_user``, approve, reject) carry their own
    claim guard, the startup role seeding carries an equivalent predicate in
    its own statement, and the phone and email binding statements carry
    ``status = 'active'``. ``update_user_last_login`` is the one account
    write with no claim guard: the login route requires an ``active``
    account, so a claimed account cannot reach it, and it writes a timestamp
    rather than an identity field.
    """
    row = await conn.fetchrow(
        "SELECT hard_delete_pending FROM users WHERE id = $1 FOR UPDATE", user_id
    )
    if row is not None and row["hard_delete_pending"]:
        raise HardDeleteStateChanged(f"Account {user_id} has a hard-delete in progress.")


async def _lock_account_keys(conn: asyncpg.Connection, account_id: str) -> None:
    """Hold off other writes to *account_id*'s keys until this transaction ends.

    Taken first, before any row is read or written, by every write that adds
    a key or changes all of an account's keys at once: create, rotate, the
    admin's update and revoke, suspension and deletion.

    Row locks cannot do this. Under READ COMMITTED an ``UPDATE ... WHERE
    user_id = $1`` that waited on a row lock re-checks only the rows its
    snapshot found, so it never sees a key inserted while it waited
    (PostgreSQL documentation, "Read Committed Isolation Level"). That key
    would keep the quota or status it was created with: a lowered quota or a
    suspension would miss it.

    The all-accounts lock is taken in shared mode first, always in that
    order, so this cannot deadlock with :func:`_lock_all_account_keys`.

    ``account_id`` equals ``user_id`` on every key: ``create_key`` defaults
    one to the other, no caller passes a different value, and startup
    backfills older rows.
    """
    await conn.execute(f"SELECT pg_advisory_xact_lock_shared({_ALL_ACCOUNT_KEYS_LOCK})")
    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended('hybridinference:api-keys:' || $1, 0))",
        account_id,
    )


async def _lock_all_account_keys(conn: asyncpg.Connection) -> None:
    """Hold off every account's key writes until this transaction ends.

    For a write that spans accounts, such as a role's quota. It waits for the
    per-account writes in flight to commit, so the snapshot of its next
    statement holds every key they added. One lock however many accounts the
    write reaches, where a lock per account could exhaust the shared lock
    table.
    """
    await conn.execute(f"SELECT pg_advisory_xact_lock({_ALL_ACCOUNT_KEYS_LOCK})")


async def _sync_account_quota(
    conn: asyncpg.Connection,
    user_id: str,
    *,
    daily: Any,
    monthly: Any,
    except_id: int | None = None,
) -> None:
    """Give every unrevoked key of *user_id* the account quota ``(daily, monthly)``.

    The daily and monthly quota belong to the account but are stored on each
    of its keys: the auth lookup reads them from the presented key's row, so
    every unrevoked key must carry the same values. A revoked key never
    authenticates again and is left as it was.
    """
    await conn.execute(
        "UPDATE api_keys SET quota_daily_cost_usd = $2, quota_monthly_cost_usd = $3 "
        "WHERE user_id = $1 AND status <> 'revoked' AND ($4::bigint IS NULL OR id <> $4) "
        "AND (quota_daily_cost_usd IS DISTINCT FROM $2::numeric "
        "     OR quota_monthly_cost_usd IS DISTINCT FROM $3::numeric)",
        user_id,
        daily,
        monthly,
        except_id,
    )


#: The key whose quota a key added to an account takes (``create_key`` with
#: ``inherit_account_quota``). An unrevoked key when the account has one: each
#: carries the account quota (:func:`_sync_account_quota`). Failing that, the
#: most recently revoked key, which carried it when it was revoked: the quota is
#: the account's, so revoking every key must not reset it, or a cut an admin
#: made -- a lowered daily limit, a monthly cap -- would end with the next key
#: the user creates. Revocation keeps no time of its own, so the most recently
#: revoked key is taken to be the one last used or created: a key is used until
#: it is revoked, and an account that holds one key at a time revokes each
#: before it creates the next.
ACCOUNT_QUOTA_SOURCE_SQL = (
    "SELECT quota_daily_cost_usd, quota_monthly_cost_usd FROM api_keys WHERE user_id = $1 "
    "ORDER BY status = 'revoked', "
    "CASE WHEN status = 'revoked' THEN GREATEST(created_at, last_used_at) END DESC NULLS LAST, "
    "created_at, id "
    "LIMIT 1"
)


def _coerce_user_row(row: Any) -> Row | None:
    """Convert an asyncpg Row to a dict, decoding the JSONB preferences field.

    asyncpg returns JSONB columns as raw JSON strings when no type codec is
    registered. Callers that inspect ``preferences`` (e.g. disabled-model
    checks) need a Python dict, not a string.
    """
    if row is None:
        return None
    d = dict(row)
    val = d.get("preferences")
    if isinstance(val, str):
        try:
            parsed = json.loads(val)
            d["preferences"] = parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, TypeError):
            d["preferences"] = {}
    elif not isinstance(val, dict):
        d["preferences"] = {}
    for field in ("allowed_models", "project_allowed_models"):
        models = d.get(field)
        if isinstance(models, str):
            try:
                parsed = json.loads(models)
            except (json.JSONDecodeError, TypeError):
                parsed = []
            d[field] = parsed if isinstance(parsed, list) else []
    return d


#: The lookups a request makes before it is served -- authentication, spend and
#: quota, session liveness, and the per-model and runtime settings read on the
#: way -- which use the request pool (:mod:`serving.storage.pools`). Each is a
#: cheap primary-key or index lookup; nothing slow may join them.
REQUEST_PATH_READS = (
    "get_user_by_id",
    "get_auth_context_by_key_hash",
    "get_auth_context_with_spend",
    "get_auth_context_lightweight",
    "get_key_spend",
    "session_is_live",
    "get_quota_context_for_user",
    "get_agent_grant",
    "get_setting",
    "get_model_visibility_override",
    "get_model_concurrency_exemption",
    "get_user_cost_today",
    "get_user_cost_period",
)


class PostgresOperationalStore(PostgresPhoneAuthMixin, OperationalStore):
    """OperationalStore backed by an asyncpg connection pool."""

    def __init__(
        self,
        pool: asyncpg.Pool,
        *,
        request_pool: asyncpg.Pool | None = None,
        fence_secret: str | None = None,
    ) -> None:
        """Initialize with a shared asyncpg pool.

        The LogStore owns the erasure-fence schema and tombstone checks; this
        store owns the operational hard-delete claim.

        Args:
            pool: Shared asyncpg pool.
            request_pool: The pool reserved for the lookups a request makes
                before it is served (:data:`REQUEST_PATH_READS`); *pool* when
                not given. Those lookups wait at most
                :data:`~serving.storage.pools.REQUEST_ACQUIRE_TIMEOUT_SEC` for a
                connection, from whichever pool, and then raise
                :class:`~serving.storage.pools.DatabaseBusyError`.
            fence_secret: The erasure fence's secret, given where this store
                shares the log store's database (as bootstrap builds them):
                cost increments then skip hard-deleted accounts
                (:meth:`increment_user_costs`).
        """
        self._pool = pool
        self._request_pool = request_pool if request_pool is not None else pool
        self._fence_secret = fence_secret

    def _request_connection(self) -> AbstractAsyncContextManager[Any]:
        """A connection for a request-path lookup: the request pool, a bounded wait."""
        return request_connection(self._request_pool)

    def on_connection(self, connection: Any) -> PostgresOperationalStore:
        """This store on one connection: every call runs on *connection*, in its transaction.

        For writes that must commit or roll back with the caller's own, such as
        a configuration restore, which writes these tables inside the
        ``app_config`` transaction. The caller owns the connection and its
        transaction; request-path lookups use it as well.
        """
        pool = SingleConnectionPool(connection)
        return PostgresOperationalStore(pool, request_pool=pool, fence_secret=self._fence_secret)

    # -- lifecycle -----------------------------------------------------------

    async def initialize(self) -> None:
        """Give the accounts named in ``ADMIN_EMAILS`` the admin role.

        The tables come from the schema migrations. This is configuration, so
        it runs at every start: an address added to the setting promotes its
        account the next time the gateway starts, as long as the account still
        has the default role. An account without an email never matches.

        Only an active account is promoted. A soft-deleted one is not a
        candidate for the admin role whatever its claim state, and a
        hard-delete claim is in progress means the account is being erased:
        promoting it would change an identity field during the erasure, which
        the claim exists to prevent. A backend restart therefore leaves such
        an account exactly as it found it.
        """
        admin_emails = _parse_admin_emails(get_settings().admin_emails or "")
        if not admin_emails:
            return
        async with self._pool.acquire() as conn:
            tag = await conn.execute(
                "UPDATE users SET role = 'admin' "
                "WHERE lower(trim(email)) = ANY($1::text[]) "
                "AND role IN ('free') "
                "AND status = 'active' "
                "AND hard_delete_pending IS NOT TRUE",
                admin_emails,
            )
        seeded = _parse_command_tag_count(tag)
        if seeded:
            logger.info("Seeded %d admin role assignments from ADMIN_EMAILS.", seeded)

    async def cleanup(self) -> None:
        """No-op — pool lifecycle is managed externally."""

    async def health_check(self) -> bool:
        """Return True if the pool can execute a trivial query."""
        try:
            async with self._pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            return True
        except Exception:
            return False

    # -- users ---------------------------------------------------------------

    async def get_user_by_id(self, user_id: str) -> Row | None:
        """Fetch a single user row by primary key."""
        async with self._request_connection() as conn:
            row = await conn.fetchrow(
                f"SELECT {_USER_SELECT} FROM users WHERE id = $1",
                user_id,
            )
        return _coerce_user_row(row)

    async def get_user_by_email(self, email: str) -> Row | None:
        """Fetch a single user row by lowercased email."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {_USER_SELECT} FROM users WHERE email = $1",
                email.lower(),
            )
        return _coerce_user_row(row)

    async def get_user_by_login_name(self, login_name: str) -> Row | None:
        """Fetch a single user row by login name, compared case-insensitively."""
        async with self._pool.acquire() as conn:
            # Matches the expression and predicate of idx_users_login_name.
            row = await conn.fetchrow(
                f"SELECT {_USER_SELECT} FROM users "
                "WHERE lower(login_name) = $1 AND login_name IS NOT NULL",
                login_name.strip().lower(),
            )
        return _coerce_user_row(row)

    async def create_user(
        self,
        *,
        user_id: str,
        email: str | None,
        password_hash: str,
        user_name: str | None = None,
        email_verified: bool = False,
        status: str = "active",
        signup_reason: str | None = None,
        login_name: str | None = None,
    ) -> None:
        """Insert a new user row and its personal organization."""
        from serving.storage.project_backfill import ensure_personal_org

        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "INSERT INTO users (id, email, password_hash, user_name, "
                "email_verified, status, signup_reason, login_name) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
                user_id,
                email.lower() if email is not None else None,
                password_hash,
                user_name,
                email_verified,
                status,
                signup_reason,
                login_name.lower() if login_name is not None else None,
            )
            await ensure_personal_org(conn, user_id=user_id, user_name=user_name)

    async def get_or_create_setup_code(
        self,
        *,
        marker_key: str,
        code_key: str,
        completed_at: str,
        candidate_code: str,
    ) -> str | None:
        """Return the shared first-run setup code, or None once setup is complete.

        Everything after the marker check runs under the first-run setup lock,
        so it cannot interleave with :meth:`create_first_admin`: a code is
        never stored after the claim committed, and a code stored before it is
        deleted by it.
        """
        async with self._pool.acquire() as conn:
            # A settled deployment reads one row and takes no lock.
            if await conn.fetchval("SELECT 1 FROM site_settings WHERE key = $1", marker_key):
                return None
            async with conn.transaction():
                await conn.execute(_FIRST_RUN_SETUP_LOCK_SQL)
                if await conn.fetchval("SELECT 1 FROM site_settings WHERE key = $1", marker_key):
                    return None
                if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM users)"):
                    # Accounts but no marker: a deployment from before
                    # first-run setup, or an account an operator script
                    # inserted. Either way it is set up.
                    await conn.execute(
                        "INSERT INTO site_settings (key, value, value_type, updated_at, updated_by) "
                        "VALUES ($1, $2, 'str', NOW(), 'existing-users') "
                        "ON CONFLICT (key) DO NOTHING",
                        marker_key,
                        completed_at,
                    )
                    await conn.execute("DELETE FROM site_settings WHERE key = $1", code_key)
                    logger.info(
                        "First-run setup marked complete: the database already has user accounts."
                    )
                    return None
                await conn.execute(
                    "INSERT INTO site_settings (key, value, value_type, updated_at, updated_by) "
                    "VALUES ($1, $2, 'str', NOW(), 'first-run-setup') "
                    "ON CONFLICT (key) DO NOTHING",
                    code_key,
                    candidate_code,
                )
                return await conn.fetchval(
                    "SELECT value FROM site_settings WHERE key = $1", code_key
                )

    async def create_first_admin(
        self,
        *,
        user_id: str,
        login_name: str,
        password_hash: str,
        user_name: str,
        marker_key: str,
        marker_value: str,
        code_key: str,
        admin_ip: str | None = None,
        actor: AuditActor | None = None,
    ) -> bool:
        """Create the first administrator unless setup has already completed.

        The transaction-scoped advisory lock serializes concurrent claims, in
        this process or another, and :meth:`get_or_create_setup_code`. Under
        READ COMMITTED (asyncpg's default) every statement after the lock takes
        a fresh snapshot, so a claim that waited on the lock sees the winner's
        committed user and marker and backs out.
        """
        from serving.storage.project_backfill import ensure_personal_org

        audit = audit_columns(
            admin_ip=admin_ip, target_user_id=None, actor=actor, target=AuditTarget.user(user_id)
        )
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(_FIRST_RUN_SETUP_LOCK_SQL)
            if await conn.fetchval("SELECT 1 FROM site_settings WHERE key = $1", marker_key):
                return False
            if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM users)"):
                return False
            await conn.execute(
                "INSERT INTO users (id, email, login_name, password_hash, user_name, "
                "role, status, email_verified) "
                "VALUES ($1, NULL, $2, $3, $4, 'admin', 'active', TRUE)",
                user_id,
                login_name.lower(),
                password_hash,
                user_name,
            )
            await conn.execute(
                "INSERT INTO site_settings (key, value, value_type, updated_at, updated_by) "
                "VALUES ($1, $2, 'str', NOW(), $3)",
                marker_key,
                marker_value,
                login_name.lower(),
            )
            await ensure_personal_org(conn, user_id=user_id, user_name=user_name)
            # The code has done its job; it must not outlive the setup it guards.
            await conn.execute("DELETE FROM site_settings WHERE key = $1", code_key)
            await insert_audit_row(
                conn,
                columns=audit,
                action="setup.admin_created",
                details={"login_name": login_name.lower(), "user_name": user_name},
                success=True,
            )
        return True

    async def update_user_fields(
        self, user_id: str, *, expected_status: str | None = None, **fields: Any
    ) -> None:
        """Update one or more columns on the users table for *user_id*.

        When ``expected_status`` is given, the update is a compare-and-swap:
        it runs under the user row lock and raises
        ``HardDeleteStateChanged`` (writing nothing) if the status has moved
        since the caller read it.

        A pending hard-delete claim always fails closed, whichever columns the
        write names: the account is being erased, so no write may change what
        is erased — not its address, not its role, not its preferences. This
        matches every other store, which refuses any work for an account
        marked ``hard_delete_pending`` (see ``batch_store._lock_owner``).

        A claimed account is always soft-deleted, so the self-service writers
        never reach this guard: they require an ``active`` account. The one
        caller that can reach it is the password-reset CLI, which reports the
        conflict itself.
        """
        if not fields:
            return
        from .base import USERS_MUTABLE_COLUMNS

        invalid = set(fields) - USERS_MUTABLE_COLUMNS.keys()
        if invalid:
            raise ValueError(f"Invalid column(s) for users: {invalid}")
        set_parts = []
        params: list[Any] = [user_id]
        for idx, (key, val) in enumerate(fields.items(), start=2):
            col = USERS_MUTABLE_COLUMNS[key]
            set_parts.append(f"{col} = ${idx}")
            params.append(val)
        sql = f"UPDATE users SET {', '.join(set_parts)} WHERE id = $1"
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT status, hard_delete_pending FROM users WHERE id = $1 FOR UPDATE",
                user_id,
            )
            if row is None:
                if expected_status is not None:
                    raise HardDeleteStateChanged(f"Account {user_id} no longer exists.")
                return
            if row["hard_delete_pending"]:
                raise HardDeleteStateChanged(f"Account {user_id} has a hard-delete in progress.")
            if expected_status is not None and row["status"] != expected_status:
                raise HardDeleteStateChanged(
                    f"Account {user_id} status changed from {expected_status!r} "
                    f"to {row['status']!r}."
                )
            await conn.execute(sql, *params)

    async def suspend_user(
        self, user_id: str, *, expected_status: str | None = None, **fields: Any
    ) -> None:
        """Suspend *user_id* and revoke its keys and sessions atomically.

        ``expected_status`` is the compare-and-swap guard described on
        :meth:`update_user_fields`; a pending hard-delete claim fails closed.
        """
        from .base import USERS_MUTABLE_COLUMNS

        fields = {**fields, "status": "suspended"}
        invalid = set(fields) - USERS_MUTABLE_COLUMNS.keys()
        if invalid:
            raise ValueError(f"Invalid column(s) for users: {invalid}")
        set_parts = []
        params: list[Any] = [user_id]
        for idx, (key, val) in enumerate(fields.items(), start=2):
            set_parts.append(f"{USERS_MUTABLE_COLUMNS[key]} = ${idx}")
            params.append(val)
        async with self._pool.acquire() as conn, conn.transaction():
            await _lock_account_keys(conn, user_id)
            row = await conn.fetchrow(
                "SELECT status, hard_delete_pending FROM users WHERE id = $1 FOR UPDATE",
                user_id,
            )
            if row is not None:
                if row["hard_delete_pending"]:
                    raise HardDeleteStateChanged(
                        f"Account {user_id} has a hard-delete in progress."
                    )
                if expected_status is not None and row["status"] != expected_status:
                    raise HardDeleteStateChanged(
                        f"Account {user_id} status changed from {expected_status!r} "
                        f"to {row['status']!r}."
                    )
            await conn.execute(f"UPDATE users SET {', '.join(set_parts)} WHERE id = $1", *params)
            await conn.execute("UPDATE api_keys SET status = 'revoked' WHERE user_id = $1", user_id)
            await conn.execute(
                "UPDATE auth_sessions SET revoked = TRUE "
                "WHERE user_id = $1 AND revoked IS NOT TRUE",
                user_id,
            )

    async def update_user_last_login(self, user_id: str) -> None:
        """Set ``last_login_at`` to the current timestamp."""
        async with self._pool.acquire() as conn:
            await conn.execute("UPDATE users SET last_login_at = NOW() WHERE id = $1", user_id)

    async def delete_user(
        self,
        user_id: str,
        *,
        admin_ip: str | None = None,
        admin_id: str | None = None,
        reason: str | None = None,
        email: str | None = None,
        self_service: bool = False,
        actor: AuditActor | None = None,
    ) -> None:
        """Soft-delete: set status='deleted', revoke keys, purge sessions/tokens.

        All mutations and the audit-log insert are atomic (single transaction).
        *self_service* marks the audit entry as the owner's own deletion, and
        requires the account to still be active and not an administrator
        under the row lock; otherwise ``SelfDeleteRefused`` rolls it all back.
        """
        audit = audit_columns(
            admin_ip=admin_ip, target_user_id=None, actor=actor, target=AuditTarget.user(user_id)
        )
        details: dict[str, Any] = {"email": email, "reason": reason}
        if admin_id is not None:
            details["admin_id"] = admin_id
        if self_service:
            details["self_service"] = True
        async with self._pool.acquire() as conn, conn.transaction():
            await _lock_account_keys(conn, user_id)
            if self_service:
                # The route checked these before proving ownership; a promotion
                # or suspension committed since must win over the deletion.
                owner = await conn.fetchrow(
                    "SELECT status, role FROM users WHERE id = $1 FOR UPDATE", user_id
                )
                if owner is None or owner["status"] != "active" or owner["role"] == "admin":
                    raise SelfDeleteRefused(user_id)
            await conn.execute("UPDATE users SET status = 'deleted' WHERE id = $1", user_id)
            await conn.execute(
                "UPDATE api_keys SET status = 'revoked' "
                "WHERE (account_id = $1 OR user_id = $1) AND status = 'active'",
                user_id,
            )
            await self._forget_phone_challenges(conn, user_id, erase_attempts=False)
            await conn.execute("DELETE FROM auth_sessions WHERE user_id = $1", user_id)
            await self._forget_browser_session_account(conn, user_id)
            await conn.execute("DELETE FROM email_verification_tokens WHERE user_id = $1", user_id)
            await conn.execute("DELETE FROM password_reset_tokens WHERE user_id = $1", user_id)
            await insert_audit_row(
                conn, columns=audit, action="delete_user", details=details, success=True
            )

    @staticmethod
    async def _forget_browser_session_account(conn: asyncpg.Connection, user_id: str) -> None:
        """Take a deleted account off every browser that listed it.

        Its sessions are gone already; this removes the account from browsers'
        account lists, which name it, and from any browser that had it active.
        """
        await conn.execute("DELETE FROM browser_session_accounts WHERE user_id = $1", user_id)
        await conn.execute(
            "UPDATE browser_sessions SET active_user_id = NULL WHERE active_user_id = $1",
            user_id,
        )

    @staticmethod
    async def _forget_phone_challenges(
        conn: asyncpg.Connection, user_id: str, *, erase_attempts: bool
    ) -> None:
        """Delete a deleted account's phone codes and, on erasure, its number's attempts."""
        await conn.execute(
            "DELETE FROM phone_auth_codes WHERE user_id = $1 OR "
            "phone = (SELECT phone FROM users WHERE id = $1)",
            user_id,
        )
        if erase_attempts:
            await conn.execute(
                "DELETE FROM phone_auth_attempts WHERE "
                "phone = (SELECT phone FROM users WHERE id = $1)",
                user_id,
            )

    async def _hard_delete_claim_is_stale(self, conn: Any, claimed_at: Any) -> bool:
        """Whether a claim's lease is older than the recovery grace period.

        Asked of PostgreSQL so every caller shares one clock. A NULL lease is
        not stale by this question: it is a claim written before the lease
        columns existed, and the caller stamps it first (see
        :meth:`begin_hard_delete_user`).
        """
        return bool(
            await conn.fetchval(
                "SELECT $1::TIMESTAMPTZ IS NOT NULL AND $1::TIMESTAMPTZ <= NOW() - $2::INTERVAL",
                claimed_at,
                _HARD_DELETE_CLAIM_RECOVERY_GRACE,
            )
        )

    async def begin_hard_delete_user(
        self,
        user_id: str,
        *,
        recovery_mode: HardDeleteRecoveryMode | None = None,
    ) -> HardDeleteClaim:
        """Claim a soft-deleted user before purging rows in another store.

        A pending claim belongs exclusively to the worker that made it. It is
        refused — not silently taken over — until its lease has gone
        unrenewed for the recovery grace period, and even then only a caller
        that verified a durable erasure fence or an operator's attestation
        (``recovery_mode``) may replace it. The takeover reuses none of the
        old token, so the superseded worker's next renewal and final purge
        both fail closed.
        """
        if recovery_mode is not None and recovery_mode not in set(HardDeleteRecoveryMode):
            raise ValueError(f"Unknown hard-delete recovery mode: {recovery_mode!r}")
        claim_token = secrets.token_urlsafe(32)
        async with self._pool.acquire() as conn:
            # A pending claim with a NULL lease predates these columns. Stamp
            # it outside the claim transaction so the stamp commits even
            # though this request is refused; every later caller then sees a
            # live lease instead of taking the old claim over immediately.
            check = await conn.fetchrow(
                "SELECT status, hard_delete_pending, hard_delete_claimed_at "
                "FROM users WHERE id = $1",
                user_id,
            )
            if (
                check is not None
                and check["hard_delete_pending"]
                and check["hard_delete_claimed_at"] is None
            ):
                await conn.execute(
                    "UPDATE users SET hard_delete_claimed_at = NOW() "
                    "WHERE id = $1 AND hard_delete_pending = TRUE AND hard_delete_claimed_at IS NULL",
                    user_id,
                )
                raise HardDeleteStateChanged(
                    f"Account {user_id} has a claim from before claim leases; "
                    "its lease was stamped now, so recovery must wait for the grace period."
                )
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT status, hard_delete_pending, hard_delete_claim_token, "
                    "hard_delete_claimed_at FROM users WHERE id = $1 FOR UPDATE",
                    user_id,
                )
                if row is None or row["status"] != "deleted":
                    raise HardDeleteStateChanged(
                        f"Account {user_id} is no longer eligible for hard-delete."
                    )
                if row["hard_delete_pending"]:
                    # Lost a race with a claim installed since the check above.
                    if row["hard_delete_claimed_at"] is None:
                        await conn.execute(
                            "UPDATE users SET hard_delete_claimed_at = NOW() "
                            "WHERE id = $1 AND hard_delete_pending = TRUE "
                            "AND hard_delete_claimed_at IS NULL",
                            user_id,
                        )
                        raise HardDeleteStateChanged(
                            f"Account {user_id} has a claim from before claim leases; "
                            "its lease was stamped now, so recovery must wait for the grace period."
                        )
                    stale = await self._hard_delete_claim_is_stale(
                        conn, row["hard_delete_claimed_at"]
                    )
                    if recovery_mode is not None and stale:
                        # A stale claim is abandoned, not shareable: replace its
                        # token and renew the lease in the same row-locked
                        # transaction, so two recovery callers cannot both
                        # proceed. The recovered provenance stops this request
                        # from releasing a claim that may already be fenced.
                        await conn.execute(
                            "UPDATE users SET hard_delete_claim_token = $2, "
                            "hard_delete_claimed_at = NOW(), "
                            "hard_delete_claim_recovered = TRUE WHERE id = $1",
                            user_id,
                            claim_token,
                        )
                        return HardDeleteClaim(
                            token=claim_token,
                            provenance=HardDeleteClaimProvenance.RECOVERED,
                        )
                    if recovery_mode is None and stale:
                        raise HardDeleteRecoveryAvailable(
                            f"Account {user_id} has a stale hard-delete claim."
                        )
                    raise HardDeleteStateChanged(
                        f"Account {user_id} already has a hard-delete in progress or its "
                        "claim has not reached the recovery grace period."
                    )
                await conn.execute(
                    "UPDATE users SET hard_delete_pending = TRUE, "
                    "hard_delete_claim_token = $2, hard_delete_claimed_at = NOW(), "
                    "hard_delete_claim_recovered = FALSE WHERE id = $1",
                    user_id,
                    claim_token,
                )
        return HardDeleteClaim(token=claim_token, provenance=HardDeleteClaimProvenance.NEW)

    async def renew_hard_delete_user_claim(self, user_id: str, claim_token: str) -> None:
        """Extend the claim lease, failing closed if superseded."""
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                UPDATE users SET hard_delete_claimed_at = NOW()
                WHERE id = $1 AND status = 'deleted'
                  AND hard_delete_pending = TRUE
                  AND hard_delete_claim_token = $2
                RETURNING id
                """,
                user_id,
                claim_token,
            )
            if row is None:
                raise HardDeleteStateChanged(
                    f"Account {user_id} no longer belongs to hard-delete claim {claim_token}."
                )

    async def release_hard_delete_user_claim(self, user_id: str, claim_token: str) -> None:
        """Release a still-unfenced hard-delete claim for a deleted user.

        A recovered claim is left in place: the worker it was taken from may
        still be unwinding after a process-level timeout, and clearing the
        marker would let that worker (or a retry) re-enter the purge without
        an operator deciding it is safe. It clears only after the grace
        period, via an explicit recovery.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            # The status and claim predicates make this a guarded cleanup:
            # never clear a claim after another operation has changed the
            # account state, and never turn an active account into a pending
            # hard-delete candidate.
            await conn.execute(
                """
                UPDATE users
                SET hard_delete_pending = FALSE, hard_delete_claim_token = NULL,
                    hard_delete_claimed_at = NULL, hard_delete_claim_recovered = FALSE
                WHERE id = $1
                  AND status = 'deleted'
                  AND hard_delete_pending = TRUE
                  AND hard_delete_claim_recovered = FALSE
                  AND hard_delete_claim_token = $2
                """,
                user_id,
                claim_token,
            )

    async def resume_user(
        self,
        user_id: str,
        *,
        admin_ip: str | None = None,
        admin_id: str | None = None,
        reason: str | None = None,
        email: str | None = None,
        recover_stale_claim: bool = False,
        actor: AuditActor | None = None,
    ) -> None:
        """Resume a soft-deleted user: set status='active' and audit it as *actor*.

        API keys remain ``revoked`` — the user re-creates one through the
        normal flow.  All mutations and the audit-log insert are atomic.

        Hard-delete coordination (issue #1421): the admin route checks the
        LogStore fence, while this transaction refuses a durable pending
        hard-delete claim. The row lock makes resume and the operational
        hard-delete claim mutually exclusive. A claim whose lease outlived the
        recovery grace period can be cleared only through
        *recover_stale_claim*, an explicit operator attestation that the
        worker holding it is gone; the lease is rechecked and the claim
        cleared in the same transaction as the status change.
        """
        audit = audit_columns(
            admin_ip=admin_ip, target_user_id=None, actor=actor, target=AuditTarget.user(user_id)
        )
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT status, hard_delete_pending, hard_delete_claim_token, "
                "hard_delete_claimed_at FROM users WHERE id = $1 FOR UPDATE",
                user_id,
            )
            if row is None or row["status"] != "deleted":
                raise HardDeleteStateChanged(
                    f"Account {user_id} is no longer soft-deleted and cannot be resumed."
                )
            if row["hard_delete_pending"]:
                claim_started_at = row["hard_delete_claimed_at"]
                stale = await self._hard_delete_claim_is_stale(conn, claim_started_at)
                if recover_stale_claim and not stale:
                    raise HardDeleteStateChanged(
                        f"Account {user_id} claim has not reached the recovery grace period."
                    )
                if not stale:
                    raise HardDeleteStateChanged(
                        f"Account {user_id} has a hard-delete in progress."
                    )
                if not recover_stale_claim:
                    raise HardDeleteRecoveryAvailable(
                        f"Account {user_id} has a stale hard-delete claim."
                    )
                cleared = await conn.fetchrow(
                    "UPDATE users SET status = 'active', hard_delete_pending = FALSE, "
                    "hard_delete_claim_token = NULL, hard_delete_claimed_at = NULL, "
                    "hard_delete_claim_recovered = FALSE "
                    "WHERE id = $1 AND status = 'deleted' AND hard_delete_pending = TRUE "
                    "AND hard_delete_claim_token = $2 RETURNING id",
                    user_id,
                    row["hard_delete_claim_token"],
                )
                if cleared is None:
                    raise HardDeleteStateChanged(
                        f"Account {user_id} changed during stale-claim recovery."
                    )
            else:
                await conn.execute("UPDATE users SET status = 'active' WHERE id = $1", user_id)
            await insert_audit_row(
                conn,
                columns=audit,
                action="resume_user",
                details={"admin_id": admin_id, "email": email, "reason": reason},
                success=True,
            )

    async def hard_delete_user(
        self,
        user_id: str,
        *,
        claim_token: str,
        admin_ip: str | None = None,
        admin_id: str | None = None,
        reason: str | None = None,
        email: str | None = None,
        actor: AuditActor | None = None,
    ) -> dict[str, int]:
        """Permanently delete a user row and all operationally-linked rows.

        ``api_logs`` and ``email_broadcast_recipients`` live in the LogStore —
        the caller must purge them separately via
        ``LogStore.hard_delete_user_data``.  Returns ``{table_name: row_count}``
        for audit details.

        The erased audit rows are those whose user target is this account;
        rows it wrote as an actor about something else keep its id.
        """
        audit = audit_columns(
            admin_ip=admin_ip, target_user_id=None, actor=actor, target=AuditTarget.user(user_id)
        )

        def _row_count(status: str) -> int:
            # asyncpg execute() returns "DELETE <n>" / "UPDATE <n>" — parse n
            try:
                return int(status.rsplit(" ", 1)[-1])
            except (ValueError, IndexError):
                return 0

        async with self._pool.acquire() as conn, conn.transaction():
            await _lock_account_keys(conn, user_id)
            user_row = await conn.fetchrow(
                "SELECT status, hard_delete_pending, hard_delete_claim_token "
                "FROM users WHERE id = $1 FOR UPDATE",
                user_id,
            )
            if (
                user_row is None
                or user_row["status"] != "deleted"
                or not user_row["hard_delete_pending"]
                or user_row["hard_delete_claim_token"] != claim_token
            ):
                raise HardDeleteStateChanged(f"Account {user_id} was not claimed for hard-delete.")

            from serving.storage.project_backfill import personal_org_id

            org_id = personal_org_id(user_id)
            await conn.execute(
                "DELETE FROM spend_counters WHERE scope_id = $1 "
                "OR scope_id IN (SELECT id FROM projects WHERE org_id = $1)",
                org_id,
            )
            await conn.execute("DELETE FROM organizations WHERE id = $1", org_id)

            keys_status = await conn.execute(
                "DELETE FROM api_keys WHERE account_id = $1 OR user_id = $1",
                user_id,
            )
            sessions_status = await conn.execute(
                "DELETE FROM auth_sessions WHERE user_id = $1", user_id
            )
            await self._forget_browser_session_account(conn, user_id)
            await self._forget_phone_challenges(conn, user_id, erase_attempts=True)
            login_events_status = await conn.execute(
                "DELETE FROM login_events WHERE user_id = $1", user_id
            )
            verif_status = await conn.execute(
                "DELETE FROM email_verification_tokens WHERE user_id = $1", user_id
            )
            reset_status = await conn.execute(
                "DELETE FROM password_reset_tokens WHERE user_id = $1", user_id
            )
            cost_status = await conn.execute(
                "DELETE FROM user_daily_cost WHERE user_id = $1", user_id
            )
            key_cost_status = await conn.execute(
                "DELETE FROM api_key_daily_cost WHERE user_id = $1", user_id
            )
            audit_status = await conn.execute(
                "DELETE FROM admin_audit_log WHERE target_user_id = $1", user_id
            )
            user_status = await conn.execute("DELETE FROM users WHERE id = $1", user_id)

            counts = {
                "api_keys": _row_count(keys_status),
                "auth_sessions": _row_count(sessions_status),
                "login_events": _row_count(login_events_status),
                "email_verification_tokens": _row_count(verif_status),
                "password_reset_tokens": _row_count(reset_status),
                "user_daily_cost": _row_count(cost_status),
                "api_key_daily_cost": _row_count(key_cost_status),
                "admin_audit_log": _row_count(audit_status),
                "users": _row_count(user_status),
            }

            # Insert NEW audit row for the hard-delete itself.  The prior
            # entries for this user were wiped above — the caller accepted
            # that compliance loss.  No FK on target_user_id, so an orphan
            # reference here is fine.
            await insert_audit_row(
                conn,
                columns=audit,
                action="hard_delete_user",
                details={
                    "admin_id": admin_id,
                    "email": email,
                    "reason": reason,
                    "rows_wiped": counts,
                },
                success=True,
            )

        return counts

    async def list_user_activity_providers(self, *, days: int = 30) -> list[str]:
        """Distinct ``api_logs.provider`` values seen in the last *days* days.

        Same table and window as the ``provider`` filter in ``list_users``.

        A skip scan of ``idx_api_logs_provider`` ``(provider, timestamp DESC)``:
        the recursive part walks the index from one distinct provider to the
        next, and each is kept if its newest row falls in the window -- one
        index probe each. A ``SELECT DISTINCT`` over the window read every row
        of the last *days* days instead, ~15 s on staging for a dozen providers.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(ACTIVITY_PROVIDERS_SQL, days)
        return [str(row["provider"]) for row in rows]

    async def get_bulk_user_usage_totals(self, user_ids: list[str]) -> dict[str, Row]:
        """Return lifetime totals separately so user management can load first."""
        if not user_ids:
            return {}
        async with self._pool.acquire() as conn:
            return await user_usage_rollup.fetch_totals(conn, user_ids)

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
        # NEW filters (Phase 1 admin Users redesign)
        min_cost_today: Decimal | None = None,
        min_cost_month: Decimal | None = None,
        quota_state: Literal["near", "over", "custom", "default"] | None = None,
        provider: str | None = None,
        active_within_hours: int | None = None,
        anomaly: bool | None = None,
        include_usage_totals: bool = True,
    ) -> tuple[int, list[Row], Row]:
        """Return ``(total_count, user_rows, status_counts_row)``.

        All filters are applied in SQL so ``total``, the returned rows, and
        pagination are consistent. Cost-based filters and sorts use CTEs that
        join against ``api_logs`` (which lives in the same Postgres instance
        for this implementation).

        ``sort_by`` also supports ``requests`` (all-time request count) and
        ``tokens`` (all-time ``total_tokens`` total). Both count every logged
        ``api_logs`` row, read through the daily rollup in
        :mod:`serving.storage.user_usage_rollup` -- not
        ``user_daily_cost.requests``, which only tracks billable (cost > 0)
        requests and would miss free/zero-cost calls. Both metrics
        are returned by default. With ``include_usage_totals=False``, metrics
        not needed for sorting are ``None`` and can be fetched separately.

        Filters (all keyword-only):
        - ``min_cost_today`` / ``min_cost_month``: filter to users whose
          today/month spend in api_logs meets the threshold (USD).
        - ``quota_state``: ``"default"`` / ``"custom"`` filter via EXISTS on
          api_keys; ``"near"`` (>=80% of daily quota) / ``"over"`` (>=100%)
          compare today's api_logs spend against the active key's quota.
        - ``provider``: keep only users who hit ``provider`` in api_logs in
          the last 30 days. Uses the ``provider`` column on api_logs.
        - ``active_within_hours``: ``users.last_login_at`` must be within the
          window.
        - ``anomaly``: when ``True``, keep only *active* users whose today's
          spend is anomalously high vs. their prior 7-day average (today >=
          $1, history >= 3 days, today >= 5x avg). Uses ``user_daily_cost``
          (same rule as ``get_users_summary``).

        ``total`` reflects the count after every filter is applied.
        ``status_counts`` is intentionally computed from the unfiltered users
        table (no filters applied) — it serves as a global navigation aid
        showing how many users exist per status across the whole dataset,
        independent of the table view's filters.
        """
        # ──────────────────────────────────────────────────────────────────
        # Shared SQL fragments used by both the count query and the row
        # query so total/rows are guaranteed consistent.
        # ──────────────────────────────────────────────────────────────────
        from datetime import datetime, timedelta
        from decimal import Decimal as _Decimal

        anomaly_multiplier = _Decimal("5.0")
        anomaly_min_today = _Decimal("1.00")
        anomaly_min_history_days = 3
        today_str = datetime.now(UTC).date().isoformat()
        prior_7d_start = (datetime.now(UTC).date() - timedelta(days=7)).isoformat()
        prior_7d_end = (datetime.now(UTC).date() - timedelta(days=1)).isoformat()

        where_clauses: list[str] = []
        filter_params: list[Any] = []

        if status:
            where_clauses.append(f"u.status = ${len(filter_params) + 1}")
            filter_params.append(status)
        if search:
            # Search matches email, login name, phone and display name as
            # substrings, plus a user.id prefix and an active key_prefix
            # prefix.
            substr_idx = len(filter_params) + 1  # %search%
            id_idx = len(filter_params) + 2  # search% (id prefix)
            kp_idx = len(filter_params) + 3  # search% (key prefix)
            where_clauses.append(
                f"(u.email ILIKE ${substr_idx} "
                f"OR u.login_name ILIKE ${substr_idx} "
                f"OR u.phone ILIKE ${substr_idx} "
                f"OR u.user_name ILIKE ${substr_idx} "
                f"OR u.id::text LIKE ${id_idx} "
                f"OR EXISTS (SELECT 1 FROM api_keys k2 "
                f"           WHERE k2.account_id = u.id "
                f"             AND k2.status = 'active' "
                f"             AND k2.key_prefix LIKE ${kp_idx}))"
            )
            filter_params.append(f"%{search}%")
            filter_params.append(f"{search}%")
            filter_params.append(f"{search}%")
        if active_within_hours is not None:
            where_clauses.append(
                f"u.last_login_at >= NOW() - ${len(filter_params) + 1}::int * INTERVAL '1 hour'"
            )
            filter_params.append(active_within_hours)
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
        if provider:
            # Keep users who hit `provider` in the last 30 days.
            where_clauses.append(
                f"EXISTS (SELECT 1 FROM api_logs l WHERE l.user_id = u.id "
                f"  AND l.provider = ${len(filter_params) + 1} "
                f"  AND l.timestamp >= NOW() - INTERVAL '30 days')"
            )
            filter_params.append(provider)

        # Whether we need today/month aggregates for filtering or sorting.
        needs_today_filter = (
            min_cost_today is not None or quota_state in ("near", "over") or sort_by == "cost_today"
        )
        needs_month_filter = min_cost_month is not None or sort_by == "cost_month"
        needs_alltime_sort = sort_by == "cost_alltime"
        needs_today = needs_today_filter or needs_alltime_sort
        needs_month = needs_month_filter or needs_alltime_sort
        needs_alltime = needs_alltime_sort
        # All-time request count and all-time token total — both count every
        # logged api_logs row, through the daily rollup in user_usage_rollup.
        # The request count must not come from user_daily_cost: it is only
        # incremented for billable (cost > 0) requests, so zero-priced / free /
        # local-model calls would be missed. Both are joined into the row query
        # as one CTE when either is the active sort key; otherwise both are
        # enriched per-page after the main query (like cost_alltime).
        needs_usage_sort = sort_by in ("requests", "tokens")

        # Cost-based scalar correlated subqueries — placed in WHERE so
        # filters apply before LIMIT/OFFSET.
        if min_cost_today is not None:
            where_clauses.append(
                f"COALESCE((SELECT SUM(cost_usd) FROM api_logs l "
                f"  WHERE l.user_id = u.id "
                f"    AND l.timestamp >= (date_trunc('day', NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')), 0) "
                f">= ${len(filter_params) + 1}"
            )
            filter_params.append(min_cost_today)
        if min_cost_month is not None:
            where_clauses.append(
                f"COALESCE((SELECT SUM(cost_usd) FROM api_logs l "
                f"  WHERE l.user_id = u.id "
                f"    AND l.timestamp >= (date_trunc('month', NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')), 0) "
                f">= ${len(filter_params) + 1}"
            )
            filter_params.append(min_cost_month)
        if quota_state in ("near", "over"):
            # threshold: 0.80 for near, 1.00 for over.
            threshold = "0.80" if quota_state == "near" else "1.00"
            where_clauses.append(
                f"EXISTS ("
                f"  SELECT 1 FROM api_keys kq "
                f"  WHERE kq.account_id = u.id "
                f"    AND kq.status = 'active' "
                f"    AND kq.quota_daily_cost_usd IS NOT NULL "
                f"    AND kq.quota_daily_cost_usd > 0 "
                f"    AND COALESCE((SELECT SUM(cost_usd) FROM api_logs l "
                f"                  WHERE l.user_id = u.id "
                f"                    AND l.timestamp >= (date_trunc('day', "
                f"                        NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')"
                f"                 ), 0) >= {threshold} * kq.quota_daily_cost_usd"
                f")"
            )
        if anomaly:
            # Anomaly applies only to active users (status='active').
            # Today's user_daily_cost row must exist with cost >= $1 AND
            # prior 7-day window must have >=3 days of history AND today
            # must be >= 5x the prior-7d average.
            today_idx = len(filter_params) + 1
            prior_start_idx = len(filter_params) + 2
            prior_end_idx = len(filter_params) + 3
            min_today_idx = len(filter_params) + 4
            min_history_idx = len(filter_params) + 5
            multiplier_idx = len(filter_params) + 6
            where_clauses.append(
                f"u.status = 'active' AND EXISTS ("
                f"  WITH t AS ("
                f"    SELECT COALESCE(SUM(cost_usd), 0) AS today_cost "
                f"    FROM user_daily_cost "
                f"    WHERE user_id = u.id AND day = ${today_idx} "
                f"  ), p AS ("
                f"    SELECT COALESCE(SUM(cost_usd), 0) AS total_prior, "
                f"           COUNT(DISTINCT day) AS days_history "
                f"    FROM user_daily_cost "
                f"    WHERE user_id = u.id "
                f"      AND day BETWEEN ${prior_start_idx} AND ${prior_end_idx}"
                f"  ) "
                f"  SELECT 1 FROM t, p "
                f"  WHERE t.today_cost >= ${min_today_idx} "
                f"    AND p.days_history >= ${min_history_idx} "
                f"    AND p.total_prior > 0 "
                f"    AND t.today_cost >= ${multiplier_idx} * (p.total_prior / p.days_history)"
                f")"
            )
            filter_params.extend(
                [
                    today_str,
                    prior_7d_start,
                    prior_7d_end,
                    anomaly_min_today,
                    anomaly_min_history_days,
                    anomaly_multiplier,
                ]
            )

        where_sql = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""

        # Sort clause map (uses fu.* aliases defined in the CTE below).
        _sort_clauses = {
            "created": "fu.created_at DESC, fu.id",
            "cost_today": "COALESCE(ut.cost, 0) DESC, fu.created_at DESC, fu.id",
            "cost_month": "COALESCE(um.cost, 0) DESC, fu.created_at DESC, fu.id",
            "cost_alltime": "COALESCE(ua.cost, 0) DESC, fu.created_at DESC, fu.id",
            "last_login": "fu.last_login_at DESC NULLS LAST, fu.created_at DESC, fu.id",
            "requests": "COALESCE(ux.request_count, 0) DESC, fu.created_at DESC, fu.id",
            "tokens": "COALESCE(ux.total_tokens, 0) DESC, fu.created_at DESC, fu.id",
        }
        order_clause = _sort_clauses[sort_by]

        limit_idx = len(filter_params) + 1
        offset_idx = len(filter_params) + 2
        query_params = [*filter_params, limit, offset]

        async with self._pool.acquire() as conn:
            # Total count — uses the SAME WHERE clause as the row query,
            # so total reflects every applied filter.
            count_row = await conn.fetchrow(
                f"SELECT COUNT(*) as total FROM users u {where_sql}",
                *filter_params,
            )
            total = count_row["total"] if count_row else 0

            # Status counts (unfiltered — global navigation aid).
            count_rows = await conn.fetch(
                "SELECT status, COUNT(*) as cnt FROM users GROUP BY status"
            )
            sc = {r["status"]: r["cnt"] for r in count_rows}
            status_counts: Row = {
                "all": sum(sc.values()),
                "pending_approval": sc.get("pending_approval", 0),
                "active": sc.get("active", 0),
                "suspended": sc.get("suspended", 0),
                "rejected": sc.get("rejected", 0),
                "deleted": sc.get("deleted", 0),
            }

            # Main query — always uses CTE form (filtered_users) so we can
            # share the WHERE clause and join optional cost CTEs uniformly.
            identity = "u.id, u.email, u.login_name, u.phone, u.phone_verified, u.user_name"
            cte_parts = [
                f"filtered_users AS ("
                f"  SELECT {identity}, u.role, u.status, "
                f"  u.email_verified, u.approval_note, u.reviewed_at, u.reviewed_by, "
                f"  u.signup_reason, u.admin_note, "
                f"  u.created_at, u.last_login_at, "
                f"  k.key_prefix, k.status AS key_status "
                f"  FROM users u "
                # One row per user: an account can hold several active keys,
                # and the list shows its newest.
                f"  LEFT JOIN LATERAL ("
                f"    SELECT key_prefix, status FROM api_keys "
                f"    WHERE account_id = u.id AND status = 'active' "
                f"    ORDER BY created_at DESC, id DESC LIMIT 1"
                f"  ) k ON TRUE "
                f"  {where_sql}"
                f")"
            ]
            join_parts: list[str] = []
            select_extras: list[str] = []

            if needs_today:
                cte_parts.append(
                    "usage_today AS ("
                    "  SELECT user_id, COALESCE(SUM(cost_usd), 0) AS cost"
                    "  FROM api_logs"
                    "  WHERE user_id IN (SELECT id FROM filtered_users)"
                    "    AND timestamp >= (date_trunc('day', NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')"
                    "  GROUP BY user_id"
                    ")"
                )
                join_parts.append("LEFT JOIN usage_today ut ON ut.user_id = fu.id")
                select_extras.append("COALESCE(ut.cost, 0) AS usage_today")

            if needs_month:
                cte_parts.append(
                    "usage_month AS ("
                    "  SELECT user_id, COALESCE(SUM(cost_usd), 0) AS cost"
                    "  FROM api_logs"
                    "  WHERE user_id IN (SELECT id FROM filtered_users)"
                    "    AND timestamp >= (date_trunc('month', NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')"
                    "  GROUP BY user_id"
                    ")"
                )
                join_parts.append("LEFT JOIN usage_month um ON um.user_id = fu.id")
                select_extras.append("COALESCE(um.cost, 0) AS usage_month")

            if needs_alltime:
                cte_parts.append(
                    "usage_alltime AS ("
                    "  SELECT user_id, COALESCE(SUM(cost_usd), 0) AS cost"
                    "  FROM api_logs"
                    "  WHERE user_id IN (SELECT id FROM filtered_users)"
                    "  GROUP BY user_id"
                    ")"
                )
                join_parts.append("LEFT JOIN usage_alltime ua ON ua.user_id = fu.id")
                select_extras.append("COALESCE(ua.cost, 0) AS usage_alltime")

            if needs_usage_sort:
                usage_totals = user_usage_rollup.metrics_sql("IN (SELECT id FROM filtered_users)")
                cte_parts.append(f"usage_totals AS ({usage_totals})")
                join_parts.append("LEFT JOIN usage_totals ux ON ux.user_id = fu.id")
                select_extras.append("COALESCE(ux.request_count, 0) AS usage_alltime_requests")
                select_extras.append("COALESCE(ux.total_tokens, 0) AS usage_alltime_tokens")

            extra_cols = ", " + ", ".join(select_extras) if select_extras else ""
            joins = " ".join(join_parts)
            ctes = ", ".join(cte_parts)

            async with conn.transaction():
                if needs_usage_sort:
                    await conn.execute(user_usage_rollup.NO_JIT_SQL)
                rows = await conn.fetch(
                    f"WITH {ctes} "
                    f"SELECT fu.*{extra_cols} FROM filtered_users fu {joins} "
                    f"ORDER BY {order_clause} "
                    f"LIMIT ${limit_idx} OFFSET ${offset_idx}",
                    *query_params,
                )

            if not rows:
                return total, [], status_counts

            # Post-fetch: batch-query usage dimensions not in CTEs so the
            # response always carries usage costs for the page.
            user_ids = [row["id"] for row in rows]

            if user_ids and not needs_today:
                today_rows = await conn.fetch(
                    "SELECT user_id, COALESCE(SUM(cost_usd), 0) AS cost "
                    "FROM api_logs "
                    "WHERE user_id = ANY($1::text[]) "
                    "  AND timestamp >= (date_trunc('day', NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC') "
                    "GROUP BY user_id",
                    user_ids,
                )
                today_map = {r["user_id"]: r["cost"] for r in today_rows}
            else:
                today_map = {}

            if user_ids and not needs_month:
                month_rows = await conn.fetch(
                    "SELECT user_id, COALESCE(SUM(cost_usd), 0) AS cost "
                    "FROM api_logs "
                    "WHERE user_id = ANY($1::text[]) "
                    "  AND timestamp >= (date_trunc('month', NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC') "
                    "GROUP BY user_id",
                    user_ids,
                )
                month_map = {r["user_id"]: r["cost"] for r in month_rows}
            else:
                month_map = {}

            if user_ids and not needs_alltime:
                alltime_rows = await conn.fetch(
                    "SELECT user_id, COALESCE(SUM(cost_usd), 0) AS cost "
                    "FROM user_daily_cost "
                    "WHERE user_id = ANY($1::text[]) "
                    "GROUP BY user_id",
                    user_ids,
                )
                alltime_map = {r["user_id"]: r["cost"] for r in alltime_rows}
            else:
                alltime_map = {}

            # Callers that load lifetime statistics separately skip them here.
            # Sorting by either metric already returned both above.
            totals = (
                await user_usage_rollup.fetch_totals(conn, user_ids)
                if include_usage_totals and not needs_usage_sort
                else {}
            )

        # Assemble result rows with usage columns.
        result_rows: list[Row] = []
        for row in rows:
            r = dict(row)
            r["usage_today"] = r.get("usage_today") or today_map.get(r["id"], 0)
            r["usage_month"] = r.get("usage_month") or month_map.get(r["id"], 0)
            r["usage_alltime"] = r.get("usage_alltime") or alltime_map.get(r["id"], 0)
            for field, metric in (
                ("usage_alltime_requests", "requests"),
                ("usage_alltime_tokens", "tokens"),
            ):
                if field not in r:
                    r[field] = totals.get(r["id"], {}).get(metric)
            result_rows.append(r)

        return total, result_rows, status_counts

    async def approve_user(
        self,
        user_id: str,
        *,
        admin_id: str,
        note: str | None = None,
        expected_status: str | None = "pending_approval",
    ) -> None:
        """Set status='active', record reviewer and note.

        Compare-and-swap: under the row lock the account must still be in
        ``expected_status`` (default ``pending_approval``) with no pending
        hard-delete claim, otherwise ``HardDeleteStateChanged`` and no write.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT status, hard_delete_pending FROM users WHERE id = $1 FOR UPDATE",
                user_id,
            )
            if row is None or row["hard_delete_pending"]:
                raise HardDeleteStateChanged(f"Account {user_id} changed during approval.")
            if expected_status is not None and row["status"] != expected_status:
                raise HardDeleteStateChanged(
                    f"Account {user_id} status changed from {expected_status!r} "
                    f"to {row['status']!r}."
                )
            await conn.execute(
                "UPDATE users SET status = 'active', approval_note = $1, "
                "reviewed_at = NOW(), reviewed_by = $2 WHERE id = $3",
                note,
                admin_id,
                user_id,
            )

    async def reject_user(
        self,
        user_id: str,
        *,
        admin_id: str,
        reason: str,
        expected_status: str | None = "pending_approval",
    ) -> None:
        """Set status='rejected', record reviewer and reason.

        Compare-and-swap on ``expected_status`` (default ``pending_approval``)
        under the row lock; a pending hard-delete claim fails closed.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT status, hard_delete_pending FROM users WHERE id = $1 FOR UPDATE",
                user_id,
            )
            if row is None or row["hard_delete_pending"]:
                raise HardDeleteStateChanged(f"Account {user_id} changed during rejection.")
            if expected_status is not None and row["status"] != expected_status:
                raise HardDeleteStateChanged(
                    f"Account {user_id} status changed from {expected_status!r} "
                    f"to {row['status']!r}."
                )
            await conn.execute(
                "UPDATE users SET status = 'rejected', approval_note = $1, "
                "reviewed_at = NOW(), reviewed_by = $2 WHERE id = $3",
                reason,
                admin_id,
                user_id,
            )

    async def get_user_counts_by_status(self) -> dict[str, int]:
        """Return ``{status_value: count}`` for all statuses."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT status, COUNT(*) as cnt FROM users GROUP BY status")
        return {r["status"]: r["cnt"] for r in rows}

    async def get_active_user_counts(self) -> dict[str, int]:
        """Return ``{"total": N, "dau": N, "mau": N}``."""
        async with self._pool.acquire() as conn:
            total_row = await conn.fetchrow(
                "SELECT COUNT(*) as count FROM users WHERE status = 'active'"
            )
            dau_row = await conn.fetchrow(
                "SELECT COUNT(DISTINCT id) as count FROM users "
                "WHERE status = 'active' AND last_login_at >= NOW() - INTERVAL '24 hours'"
            )
            mau_row = await conn.fetchrow(
                "SELECT COUNT(DISTINCT id) as count FROM users "
                "WHERE status = 'active' AND last_login_at >= NOW() - INTERVAL '30 days'"
            )
        return {
            "total": total_row["count"] if total_row else 0,
            "dau": dau_row["count"] if dau_row else 0,
            "mau": mau_row["count"] if mau_row else 0,
        }

    # -- api keys ------------------------------------------------------------

    async def get_auth_context_by_key_hash(self, key_hash: str) -> Row | None:
        """Materialized auth lookup for ``verify_api_key``.

        Returns full projection in a single round-trip.
        """
        identity = _JOINED_USER_IDENTITY
        async with self._request_connection() as conn:
            row = await conn.fetchrow(
                "SELECT k.id, k.user_id, k.user_name, "
                f"{_PROJECT_SCOPE_COLUMNS}, {_AUTH_KEY_COLUMNS}, "
                f"{identity}, u.role, u.email_verified, {_KEY_AUTH_PREFERENCES}, u.max_concurrent_requests "
                "FROM api_keys k "
                "LEFT JOIN users u ON u.id = k.user_id "
                f"{_PROJECT_SCOPE_JOIN}"
                "WHERE k.key_hash = $1 "
                "  AND k.status = 'active' "
                "  AND (k.expires_at IS NULL OR k.expires_at > NOW()) "
                "  AND u.id IS NOT NULL AND u.status = 'active' "
                "AND (k.project_id IS NULL OR p.id IS NOT NULL)",
                key_hash,
            )
        return _coerce_user_row(row)

    async def get_auth_context_with_spend(self, key_hash: str) -> Row | None:
        """Auth context and today's spend in one round-trip (issue #185).

        The identity projection and filters are :meth:`get_auth_context_by_key_hash`'s
        exactly; the LEFT JOIN adds the owner's ``user_daily_cost`` counter for
        the UTC-midnight day, ``COALESCE``d to ``0.0`` so a caller with no
        counter row yet reads the same as the separate query's default. The
        day expression is the counter write path's, so both sides of the quota
        gate bucket midnight identically.

        The key's own limits come with it, and, for a key with a budget, what
        the key spent today and this month (``key_spent_today_usd``,
        ``key_spent_month_usd``; see ``_KEY_SPEND_JOIN``), and, for an
        account with a monthly quota, what it spent this month
        (``account_spent_month_usd``; see ``_ACCOUNT_MONTH_SPEND_JOIN``). Still
        one round-trip.
        """
        identity = _JOINED_USER_IDENTITY
        async with self._request_connection() as conn:
            row = await conn.fetchrow(
                "SELECT k.id, k.user_id, k.user_name, "
                f"{_PROJECT_SCOPE_COLUMNS}, am.account_spent_month_usd, "
                f"{_AUTH_KEY_COLUMNS}, ks.key_spent_today_usd, ks.key_spent_month_usd, "
                f"{_SCOPE_SPEND_COLUMNS}, "
                f"{identity}, u.role, u.email_verified, {_KEY_AUTH_PREFERENCES}, u.max_concurrent_requests, "
                "COALESCE(s.cost_usd, 0) AS cost_usd "
                "FROM api_keys k "
                "LEFT JOIN users u ON u.id = k.user_id "
                "LEFT JOIN user_daily_cost s "
                "  ON s.user_id = k.user_id "
                " AND s.day = to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM-DD') "
                f"{_PROJECT_SCOPE_JOIN}"
                f"{_KEY_SPEND_JOIN}"
                f"{_ACCOUNT_MONTH_SPEND_JOIN}"
                f"{_SCOPE_SPEND_JOIN}"
                "WHERE k.key_hash = $1 "
                "  AND k.status = 'active' "
                "  AND (k.expires_at IS NULL OR k.expires_at > NOW()) "
                "  AND u.id IS NOT NULL AND u.status = 'active' "
                "AND (k.project_id IS NULL OR p.id IS NOT NULL)",
                key_hash,
            )
        return _coerce_user_row(row)

    async def get_auth_context_lightweight(self, key_hash: str) -> Row | None:
        """Lightweight identity lookup (no quota check, no last_used write).

        Carries the key's model allowlist, which the model listings apply.
        """
        identity = _JOINED_USER_IDENTITY
        async with self._request_connection() as conn:
            row = await conn.fetchrow(
                f"SELECT k.user_id, {identity}, u.role, u.email_verified, {_KEY_AUTH_PREFERENCES}, "
                "u.max_concurrent_requests, k.allowed_models, "
                "p.allowed_models AS project_allowed_models, o.status AS org_status "
                "FROM api_keys k "
                "LEFT JOIN users u ON u.id = k.user_id "
                f"{_PROJECT_SCOPE_JOIN}"
                "WHERE k.key_hash = $1 "
                "  AND k.status = 'active' "
                "  AND (k.expires_at IS NULL OR k.expires_at > NOW()) "
                "  AND u.id IS NOT NULL AND u.status = 'active' "
                "AND (k.project_id IS NULL OR p.id IS NOT NULL)",
                key_hash,
            )
        return _coerce_user_row(row)

    async def get_key_owner_for_audit(self, key_hash: str) -> Row | None:
        """Identity behind a presented key, with no status or expiry filter.

        Diagnostic only -- see :meth:`OperationalStore.get_key_owner_for_audit`.
        The join is LEFT so a key whose user row was deleted still names the
        account it was issued to; ``key_expired`` is computed here rather than
        derived from ``expires_at``, so the deadline is compared against the
        database clock exactly as the auth lookups compare it.

        ``expires_in_sec`` comes back alongside it for one reason: expiry is
        the only state change here that fires no write, so a caching layer has
        nothing to invalidate on and needs the remaining time to keep from
        serving ``key_expired: False`` past the deadline. It is measured here
        rather than by subtracting ``expires_at`` from the reader's clock --
        the same clock that decides ``key_expired`` should decide how long that
        answer holds, or a gateway running behind the database would extend the
        window by exactly its own skew. NULL when the key has no deadline.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT k.user_id, u.role, "
                "k.status AS key_status, "
                "(k.expires_at IS NOT NULL AND k.expires_at <= NOW()) AS key_expired, "
                "EXTRACT(EPOCH FROM (k.expires_at - NOW()))::float8 AS expires_in_sec, "
                "u.status AS user_status "
                "FROM api_keys k "
                "LEFT JOIN users u ON u.id = k.user_id "
                "WHERE k.key_hash = $1",
                key_hash,
            )
        return dict(row) if row is not None else None

    async def update_key_last_used(self, key_id: int) -> None:
        """Set ``last_used_at = NOW()`` for the given key id, at most once a minute.

        The guard leaves a row written in the last minute alone: no new row
        version and no row lock held to the end of a write, so the processes
        serving a busy key do not queue on its row for a column the console
        reads at minute resolution.
        """
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE api_keys SET last_used_at = NOW() WHERE id = $1 "
                "AND (last_used_at IS NULL OR last_used_at < NOW() - INTERVAL '60 seconds')",
                key_id,
            )

    async def get_key_spend(self, key_id: int) -> tuple[float, float]:
        """What key *key_id* spent on the current UTC day and month, from its counter."""
        async with self._request_connection() as conn:
            row = await conn.fetchrow(
                "SELECT COALESCE(SUM(cost_usd) FILTER ("
                "         WHERE day = to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM-DD')), 0) "
                "         AS today, "
                "       COALESCE(SUM(cost_usd), 0) AS month "
                "FROM api_key_daily_cost "
                "WHERE key_id = $1 "
                "  AND day >= to_char(date_trunc('month', NOW() AT TIME ZONE 'UTC'), 'YYYY-MM-DD')",
                key_id,
            )
        return float(row["today"]), float(row["month"])

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
        api_key_encrypted: str | None = None,
        metadata: str | dict[str, Any] | None = None,
        account_id: str | None = None,
        inherit_account_quota: bool = False,
        name: str | None = None,
        max_active_keys: int | None = None,
        project_id: str | None = None,
    ) -> Row:
        """Insert a new API key. Returns the inserted row.

        The quota is the account's (see :meth:`OperationalStore.create_key`).
        With ``inherit_account_quota`` a key added to an account that has held
        a key takes the account's quota (:data:`ACCOUNT_QUOTA_SOURCE_SQL`),
        and the given values only seed an account's first key. Without it the
        given values become the account's, and every other unrevoked key of
        the user is set to them in the same transaction.

        ``max_active_keys`` caps the account's active keys. Everything runs
        under the account lock (:func:`_lock_account_keys`), so two concurrent
        creates cannot both pass a cap of one remaining slot, and a quota or
        status written to the account meanwhile reaches the new key.

        Raises:
            APIKeyLimitReachedError: The account already holds ``max_active_keys``.
        """
        # Default account_id to user_id so self-service AND admin-created keys
        # are visible/manageable in the dashboard (list/info/lookup all key off
        # account_id). Callers that omit account_id (e.g. admin key creation)
        # would otherwise persist NULL and hide the key from the user. This
        # stops the ongoing NULL-producing path at its single source (#630).
        effective_account_id = account_id if account_id is not None else user_id

        # asyncpg has no jsonb codec registered on this pool, so jsonb
        # parameters must be JSON strings; admin routes pass dicts through.
        if isinstance(metadata, dict):
            metadata = json.dumps(metadata)

        async with self._pool.acquire() as conn, conn.transaction():
            await _lock_account_keys(conn, user_id)
            if max_active_keys is not None:
                active = await conn.fetchval(
                    "SELECT COUNT(*) FROM api_keys WHERE user_id = $1 AND status = 'active'",
                    user_id,
                )
                if active >= max_active_keys:
                    raise APIKeyLimitReachedError(max_active_keys)
            daily, monthly = quota_daily_cost_usd, quota_monthly_cost_usd
            if inherit_account_quota:
                current = await conn.fetchrow(ACCOUNT_QUOTA_SOURCE_SQL, user_id)
                if current is not None:
                    daily = current["quota_daily_cost_usd"]
                    monthly = current["quota_monthly_cost_usd"]
            from serving.storage.project_backfill import (
                default_project_id,
                ensure_personal_org,
                personal_org_id,
            )

            target_project = project_id or default_project_id(user_id)
            project_query = (
                "SELECT org_id FROM projects WHERE id = $1 AND org_id = $2 AND status = 'active'"
            )
            project_row = await conn.fetchrow(
                project_query, target_project, personal_org_id(user_id)
            )
            if project_row is None and project_id is None:
                await ensure_personal_org(conn, user_id=user_id, user_name=user_name)
                project_row = await conn.fetchrow(
                    project_query, target_project, personal_org_id(user_id)
                )
            if project_row is None:
                raise ProjectNotFoundError("Project not found")
            org = project_row["org_id"]
            row = await conn.fetchrow(
                f"INSERT INTO api_keys ({_KEY_INSERT_COLUMNS}) VALUES ({_KEY_INSERT_VALUES}) "
                "RETURNING id, created_at",
                key_hash,
                key_prefix,
                user_id,
                user_name,
                daily,
                monthly,
                expires_at,
                notes,
                metadata,
                effective_account_id,
                api_key_encrypted,
                name,
                org,
                target_project,
            )
            await _sync_account_quota(
                conn, user_id, daily=daily, monthly=monthly, except_id=row["id"]
            )
            # Without inheritance the given quota is the account's; the
            # organization ceiling is what the gate enforces, so write it too.
            if not inherit_account_quota:
                await conn.execute(
                    "UPDATE organizations SET budget_daily_usd = $2, budget_monthly_usd = $3 "
                    "WHERE id = $1",
                    org,
                    daily,
                    monthly,
                )
        return dict(row)

    async def count_active_keys(self, user_id: str) -> int:
        """Return how many active keys *user_id* holds."""
        async with self._pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM api_keys WHERE user_id = $1 AND status = 'active'",
                user_id,
            )
        return int(count or 0)

    async def list_account_keys(self, account_id: str) -> list[Row]:
        """Every key the account owns, live first then newest, for its own console.

        Includes the ciphertext the console decrypts to show a saved key; the
        hash never leaves the store.

        ``spent_today_usd`` and ``spent_month_usd`` are what the key spent on
        the current UTC day and month, from ``api_key_daily_cost``, the days
        that bucket the account's quota. At most a month of counter rows per
        key is read, through its primary key.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT k.id, k.key_prefix, k.name, k.created_at, k.last_used_at, k.status, "
                "k.project_id, p.name AS project_name, "
                f"k.api_key_encrypted, {_KEY_LIMIT_COLUMNS}, "
                "COALESCE(s.today, 0) AS spent_today_usd, "
                "COALESCE(s.month, 0) AS spent_month_usd "
                "FROM api_keys k "
                "LEFT JOIN projects p ON p.id = k.project_id "
                "LEFT JOIN LATERAL ("
                "  SELECT SUM(c.cost_usd) FILTER (WHERE c.day = d.today) AS today, "
                "         SUM(c.cost_usd) AS month "
                "  FROM api_key_daily_cost c, "
                "       (SELECT to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM-DD') AS today, "
                "               to_char(date_trunc('month', NOW() AT TIME ZONE 'UTC'), "
                "                       'YYYY-MM-DD') AS month_start) d "
                "  WHERE c.key_id = k.id AND c.day >= d.month_start"
                ") s ON TRUE "
                "WHERE k.account_id = $1 "
                "ORDER BY (k.status = 'active') DESC, k.created_at DESC, k.id DESC",
                account_id,
            )
        return [dict(row) for row in rows]

    # -- projects ------------------------------------------------------------

    async def create_project(
        self,
        *,
        project_id: str,
        user_id: str,
        name: str,
        description: str | None = None,
        budget_daily_usd: Decimal | float | None = None,
        budget_monthly_usd: Decimal | float | None = None,
        allowed_models: list[str] | None = None,
        max_concurrent_requests: int | None = None,
    ) -> Row:
        """Create a project in the account's personal organization."""
        from serving.storage.project_backfill import personal_org_id

        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO projects "
                "(id, org_id, name, budget_daily_usd, budget_monthly_usd, "
                "allowed_models, max_concurrent_requests, description) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING *",
                project_id,
                personal_org_id(user_id),
                name,
                budget_daily_usd,
                budget_monthly_usd,
                allowed_models,
                max_concurrent_requests,
                description,
            )
        return self._project_row(row)

    async def list_projects(self, user_id: str) -> list[Row]:
        """List active projects owned by the account."""
        from serving.storage.project_backfill import personal_org_id

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM projects WHERE org_id = $1 AND status = 'active' "
                "ORDER BY is_default DESC, created_at DESC",
                personal_org_id(user_id),
            )
        return [self._project_row(row) for row in rows]

    async def get_default_project(self, user_id: str) -> Row | None:
        """Return Default, provisioning it if startup backfill has not reached this account."""
        from serving.storage.project_backfill import ensure_personal_org, personal_org_id

        async with self._pool.acquire() as conn, conn.transaction():
            await _lock_account_keys(conn, user_id)
            row = await conn.fetchrow(
                "SELECT * FROM projects WHERE org_id = $1 AND is_default AND status = 'active'",
                personal_org_id(user_id),
            )
            if row is None:
                user = await conn.fetchrow("SELECT user_name FROM users WHERE id = $1", user_id)
                if user is None:
                    return None
                await ensure_personal_org(conn, user_id=user_id, user_name=user["user_name"])
                row = await conn.fetchrow(
                    "SELECT * FROM projects WHERE org_id = $1 AND is_default AND status = 'active'",
                    personal_org_id(user_id),
                )
        return self._project_row(row) if row else None

    async def get_project(self, user_id: str, project_id: str) -> Row | None:
        """Return an active project owned by the account."""
        from serving.storage.project_backfill import personal_org_id

        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM projects WHERE id = $1 AND org_id = $2 AND status = 'active'",
                project_id,
                personal_org_id(user_id),
            )
        return self._project_row(row) if row else None

    async def update_project(
        self, user_id: str, project_id: str, changes: dict[str, Any]
    ) -> Row | None:
        """Update an active project owned by the account."""
        from serving.storage.project_backfill import personal_org_id

        allowed = {
            "name",
            "description",
            "budget_daily_usd",
            "budget_monthly_usd",
            "allowed_models",
            "max_concurrent_requests",
        }
        assignments: list[str] = []
        values: list[Any] = []
        for name, value in changes.items():
            if name not in allowed:
                continue
            values.append(value)
            assignments.append(f"{name} = ${len(values) + 2}::{_COLUMN_CASTS[name]}")
        if not assignments:
            return await self.get_project(user_id, project_id)
        assignments.append("archived_at = archived_at")
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"UPDATE projects SET {', '.join(assignments)} "
                "WHERE id = $1::text AND org_id = $2::text AND status = 'active' RETURNING *",
                project_id,
                personal_org_id(user_id),
                *values,
            )
        return self._project_row(row) if row else None

    async def archive_project(self, user_id: str, project_id: str) -> int:
        """Archive a non-default project and revoke its keys atomically."""
        from serving.storage.project_backfill import personal_org_id

        async with self._pool.acquire() as conn, conn.transaction():
            await _lock_account_keys(conn, user_id)
            project = await conn.fetchrow(
                "SELECT is_default FROM projects WHERE id = $1 AND org_id = $2 "
                "AND status = 'active'",
                project_id,
                personal_org_id(user_id),
            )
            if project is None:
                return 0
            if project["is_default"]:
                raise ValueError("The Default project cannot be archived.")
            revoked = await conn.execute(
                "UPDATE api_keys SET status = 'revoked', revoked_at = NOW() "
                "WHERE project_id = $1 AND status <> 'revoked'",
                project_id,
            )
            await conn.execute(
                "UPDATE projects SET status = 'archived', archived_at = NOW() WHERE id = $1",
                project_id,
            )
        return _parse_command_tag_count(revoked)

    async def update_org(self, org_id: str, changes: dict[str, Any]) -> Row | None:
        """Update the organization's ceilings and status.

        A personal organization's budget is the account's quota: its unrevoked
        keys are given it too, so the console's key columns and the enforced
        ceiling cannot diverge.
        """
        allowed = {
            "name",
            "status",
            "budget_daily_usd",
            "budget_monthly_usd",
            "max_concurrent_requests",
        }
        assignments: list[str] = []
        values: list[Any] = []
        for name, value in changes.items():
            if name not in allowed:
                continue
            values.append(value)
            assignments.append(f"{name} = ${len(values)}::{_COLUMN_CASTS[name]}")
        if not assignments:
            return None
        from serving.storage.project_backfill import personal_org_user_id

        user_id = personal_org_user_id(org_id)
        async with self._pool.acquire() as conn, conn.transaction():
            if user_id is not None:
                await _lock_account_keys(conn, user_id)
            row = await conn.fetchrow(
                f"UPDATE organizations SET {', '.join(assignments)} "
                f"WHERE id = ${len(values) + 1}::text RETURNING *",
                *values,
                org_id,
            )
            if row is not None and row["kind"] == "personal" and user_id is not None:
                for key, column in (
                    ("budget_daily_usd", "quota_daily_cost_usd"),
                    ("budget_monthly_usd", "quota_monthly_cost_usd"),
                ):
                    if key in changes:
                        await conn.execute(
                            f"UPDATE api_keys SET {column} = $1 "
                            "WHERE user_id = $2 AND status <> 'revoked'",
                            changes[key],
                            user_id,
                        )
        return dict(row) if row else None

    async def get_account_budgets(self, user_id: str) -> tuple[float, float | None]:
        """Resolve organization ceilings with the legacy account fallback."""
        from serving.storage.project_backfill import personal_org_id

        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT o.budget_daily_usd, o.budget_monthly_usd, "
                " (SELECT k.quota_daily_cost_usd FROM api_keys k "
                "  WHERE k.user_id = $2 AND k.status <> 'revoked' "
                "  ORDER BY k.created_at LIMIT 1) AS legacy_daily, "
                " (SELECT k.quota_monthly_cost_usd FROM api_keys k "
                "  WHERE k.user_id = $2 AND k.status <> 'revoked' "
                "  ORDER BY k.created_at LIMIT 1) AS legacy_monthly "
                "FROM organizations o WHERE o.id = $1",
                personal_org_id(user_id),
                user_id,
            )
        if row is None:
            return quota.DEFAULT_DAILY_QUOTA_USD, None
        daily = row["budget_daily_usd"]
        if daily is None:
            daily = row["legacy_daily"]
        monthly = row["budget_monthly_usd"]
        if monthly is None:
            monthly = row["legacy_monthly"]
        return (
            quota.resolve_quota(daily),
            None if monthly is None else float(monthly),
        )

    async def get_personal_org_status(self, user_id: str) -> str | None:
        """Return the status of the account's personal organization, or None."""
        from serving.storage.project_backfill import personal_org_id

        async with self._pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT status FROM organizations WHERE id = $1", personal_org_id(user_id)
            )

    async def count_project_keys(self, project_id: str) -> int:
        """Count active keys attached to a project."""
        async with self._pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM api_keys WHERE project_id = $1 AND status <> 'revoked'",
                project_id,
            )
        return int(count or 0)

    async def get_project_spend(self, project_id: str) -> tuple[float, float]:
        """Read the project's current UTC daily and monthly spend."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT period, cost_usd FROM spend_counters "
                "WHERE scope_id = $1 AND period IN ("
                "  'd:' || to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM-DD'), "
                "  'm:' || to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM'))",
                project_id,
            )
        today = month = 0.0
        for row in rows:
            if row["period"].startswith("d:"):
                today = float(row["cost_usd"])
            else:
                month = float(row["cost_usd"])
        return today, month

    @staticmethod
    def _project_row(row: Any) -> Row:
        record = dict(row)
        models = record.get("allowed_models")
        if isinstance(models, str):
            try:
                parsed = json.loads(models)
                record["allowed_models"] = parsed if isinstance(parsed, list) else []
            except (json.JSONDecodeError, TypeError):
                record["allowed_models"] = []
        return record

    async def update_key_settings(
        self, account_id: str, key_prefix: str, changes: dict[str, Any]
    ) -> tuple[Row, Row] | None:
        """Change the owner's settings on the account's unrevoked key *key_prefix*.

        *changes* maps columns of :data:`KEY_SETTINGS_COLUMNS` to their new
        values; a column it leaves out is unchanged, and None clears one.
        Returns the key's settings ``(before, after)``, or None when the account
        has no such key. The row lock keeps two concurrent changes from both
        reporting the same ``before``.

        Raises:
            ValueError: *changes* is empty or names another column.
        """
        if not changes or set(changes) - set(KEY_SETTINGS_COLUMNS):
            raise ValueError(f"key settings must be a non-empty subset of {KEY_SETTINGS_COLUMNS}")
        columns = [c for c in KEY_SETTINGS_COLUMNS if c in changes]
        assignments = ", ".join(f"{c} = ${i}" for i, c in enumerate(columns, start=3))
        returned = ", ".join(KEY_SETTINGS_COLUMNS)
        async with self._pool.acquire() as conn, conn.transaction():
            before = await conn.fetchrow(
                f"SELECT id, {returned} FROM api_keys "
                "WHERE account_id = $1 AND key_prefix = $2 AND status <> 'revoked' "
                "FOR UPDATE",
                account_id,
                key_prefix,
            )
            if before is None:
                return None
            after = await conn.fetchrow(
                f"UPDATE api_keys SET {assignments} WHERE account_id = $1 AND key_prefix = $2 "
                f"AND id = ${len(columns) + 3} RETURNING {returned}",
                account_id,
                key_prefix,
                *(changes[c] for c in columns),
                before["id"],
            )
        old = dict(before)
        del old["id"]
        return old, dict(after)

    async def rotate_key(
        self,
        account_id: str,
        key_prefix: str | None,
        *,
        new_key_hash: str,
        new_key_prefix: str,
        api_key_encrypted: str | None,
    ) -> Row | None:
        """Give the account's active key *key_prefix* a new credential.

        The key stays the same key: its row, and so its id, name, limits,
        quota, expiry, notes, metadata and creation time, are kept, and only
        its secret changes (hash, prefix and ciphertext). Everything keyed by
        the id carries on: what the key spent counts against its budgets after
        the rotation as before, and its request history stays its own. The old
        secret stops authenticating with the commit.

        One transaction under the account lock. A *key_prefix* of None means the
        account's only active key, for clients written when an account held
        one. Which key that is gets decided under the lock, so a key created
        meanwhile is never rotated in its place.

        Returns ``{"id", "created_at", "old_key_prefix"}``, or None when the
        account has no such active key.

        Raises:
            AmbiguousAPIKeyError: *key_prefix* is None and the account holds
                several active keys.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            await _lock_account_keys(conn, account_id)
            if key_prefix is None:
                live = await conn.fetch(
                    "SELECT key_prefix FROM api_keys "
                    "WHERE account_id = $1 AND status = 'active' LIMIT 2",
                    account_id,
                )
                if len(live) > 1:
                    raise AmbiguousAPIKeyError()
                if not live:
                    return None
                key_prefix = live[0]["key_prefix"]
            row = await conn.fetchrow(
                "UPDATE api_keys SET key_hash = $3, key_prefix = $4, api_key_encrypted = $5 "
                "WHERE account_id = $1 AND key_prefix = $2 AND status = 'active' "
                "RETURNING id, created_at",
                account_id,
                key_prefix,
                new_key_hash,
                new_key_prefix,
                api_key_encrypted,
            )
        if row is None:
            return None
        return {"id": row["id"], "created_at": row["created_at"], "old_key_prefix": key_prefix}

    async def check_active_key_exists(self, user_id: str) -> bool:
        """Return True if *user_id* already has an active key."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id FROM api_keys WHERE user_id = $1 AND status = 'active'",
                user_id,
            )
        return row is not None

    async def list_keys(
        self,
        *,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[int, list[Row]]:
        """Return ``(total_count, key_rows)`` with optional filters."""
        where_clauses: list[str] = []
        params: list[Any] = []

        if status:
            where_clauses.append(f"k.status = ${len(params) + 1}")
            params.append(status)

        where_sql = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""
        params.append(limit)
        params.append(offset)

        async with self._pool.acquire() as conn:
            count_row = await conn.fetchrow(
                f"SELECT COUNT(*) as total FROM api_keys k {where_sql}",
                *params[: len(params) - 2],
            )
            total = count_row["total"] if count_row else 0

            rows = await conn.fetch(
                f"SELECT k.user_id, k.user_name, k.key_prefix, k.status, "
                f"k.quota_daily_cost_usd, k.quota_monthly_cost_usd, "
                f"k.created_at, k.last_used_at, k.expires_at, k.notes, "
                f"k.project_id, p.name AS project_name "
                f"FROM api_keys k LEFT JOIN projects p ON p.id = k.project_id {where_sql} "
                f"ORDER BY k.created_at DESC "
                f"LIMIT ${len(params) - 1} OFFSET ${len(params)}",
                *params,
            )

        return total, [dict(r) for r in rows]

    async def get_key_detail(self, user_id: str) -> Row | None:
        """Fetch the current key row for a given *user_id*.

        A user accumulates rows over time (revoke + re-create leaves the
        revoked row behind), so prefer the live key: active first, then
        suspended, then revoked, newest first within each status.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT user_id, user_name, key_prefix, status, "
                "quota_daily_cost_usd, quota_monthly_cost_usd, "
                "created_at, last_used_at, expires_at, notes, metadata "
                "FROM api_keys WHERE user_id = $1 "
                "ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'suspended' THEN 1 ELSE 2 END, "
                "created_at DESC "
                "LIMIT 1",
                user_id,
            )
        if row is None:
            return None
        d = dict(row)
        # asyncpg returns jsonb as a raw string without a codec; callers
        # (e.g. APIKeyDetailResponse) expect a dict.
        val = d.get("metadata")
        if isinstance(val, str):
            try:
                parsed = json.loads(val)
                d["metadata"] = parsed if isinstance(parsed, dict) else None
            except (json.JSONDecodeError, TypeError):
                d["metadata"] = None
        return d

    async def update_key(self, user_id: str, **fields: Any) -> None:
        """Dynamically update the columns of every unrevoked key *user_id* holds.

        An account-level write: an account may hold several keys, and the
        fields here (quota, status, expiry, notes) describe the account, so
        all of its live keys change together -- which is also what keeps the
        account quota identical on each of them (see :func:`_sync_account_quota`).

        Revoked rows are never touched: a user keeps old revoked rows after
        regenerating a key, and updating them would silently resurrect dead
        keys.
        """
        if not fields:
            return
        from .base import API_KEYS_MUTABLE_COLUMNS

        invalid = set(fields) - API_KEYS_MUTABLE_COLUMNS.keys()
        if invalid:
            raise ValueError(f"Invalid column(s) for api_keys: {invalid}")
        set_parts = []
        params: list[Any] = [user_id]
        for idx, (key, val) in enumerate(fields.items(), start=2):
            col = API_KEYS_MUTABLE_COLUMNS[key]
            if key == "metadata" and isinstance(val, dict):
                # jsonb params must be JSON strings (no codec on this pool).
                val = json.dumps(val)
            set_parts.append(f"{col} = ${idx}")
            params.append(val)
        sql = (
            f"UPDATE api_keys SET {', '.join(set_parts)} WHERE user_id = $1 AND status <> 'revoked'"
        )
        async with self._pool.acquire() as conn, conn.transaction():
            await _lock_account_keys(conn, user_id)
            await conn.execute(sql, *params)
            from serving.storage.project_backfill import personal_org_id

            for key, column in (
                ("quota_daily_cost_usd", "budget_daily_usd"),
                ("quota_monthly_cost_usd", "budget_monthly_usd"),
            ):
                if key in fields:
                    await conn.execute(
                        f"UPDATE organizations SET {column} = $2 WHERE id = $1",
                        personal_org_id(user_id),
                        fields[key],
                    )

    async def revoke_key(self, user_id: str, *, hard_delete: bool = False) -> None:
        """Soft-revoke (status='revoked') or hard-delete every key of *user_id*."""
        async with self._pool.acquire() as conn, conn.transaction():
            await _lock_account_keys(conn, user_id)
            if hard_delete:
                await conn.execute("DELETE FROM api_keys WHERE user_id = $1", user_id)
                await conn.execute("DELETE FROM api_key_daily_cost WHERE user_id = $1", user_id)
            else:
                await conn.execute(
                    "UPDATE api_keys SET status = 'revoked' WHERE user_id = $1",
                    user_id,
                )

    async def regenerate_key(
        self,
        user_id: str,
        *,
        new_key_hash: str,
        new_key_prefix: str,
    ) -> str:
        """Atomically replace the newest active key's hash/prefix. Returns old key_prefix.

        Scoped to one row: users keep old revoked rows around, and an account
        may hold several active keys. Rewriting key_hash on more than one row
        would violate the unique key_hash constraint (and resurrect revoked
        credentials).
        """
        async with self._pool.acquire() as conn, conn.transaction():
            old_row = await conn.fetchrow(
                "SELECT id, key_prefix FROM api_keys WHERE user_id = $1 AND status = 'active' "
                "ORDER BY created_at DESC, id DESC LIMIT 1 FOR UPDATE",
                user_id,
            )
            if not old_row:
                raise ValueError(f"No active key found for user_id={user_id}")
            await conn.execute(
                "UPDATE api_keys SET key_hash = $1, key_prefix = $2 WHERE id = $3",
                new_key_hash,
                new_key_prefix,
                old_row["id"],
            )
        return old_row["key_prefix"]

    async def get_key_by_account_or_user(self, account_id: str) -> Row | None:
        """Fetch the newest active key by account_id OR user_id.

        Unlike :meth:`get_active_key_by_account`, this matches by ``user_id``
        too, so it also finds legacy keys whose ``account_id`` is NULL.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, key_prefix, created_at, last_used_at, status, "
                "quota_daily_cost_usd "
                "FROM api_keys "
                "WHERE (account_id = $1 OR user_id = $1) AND status = 'active' "
                "ORDER BY created_at DESC, id DESC LIMIT 1",
                account_id,
            )
        return dict(row) if row else None

    async def get_active_key_by_account(self, account_id: str) -> Row | None:
        """Fetch the newest active key of a self-registered user by account_id.

        The quota on it is the account's: every unrevoked key carries the same.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT k.id, k.key_prefix, k.created_at, k.last_used_at, k.status, "
                "k.quota_daily_cost_usd, k.project_id, p.name AS project_name "
                "FROM api_keys k LEFT JOIN projects p ON p.id = k.project_id "
                "WHERE k.account_id = $1 AND k.status = 'active' "
                "ORDER BY k.created_at DESC, k.id DESC LIMIT 1",
                account_id,
            )
        return dict(row) if row else None

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
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO auth_sessions "
                "(id, user_id, refresh_token_hash, jti, sid, expires_at, revoked) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7)",
                session_id,
                user_id,
                refresh_token_hash,
                jti,
                sid,
                expires_at,
                False,
            )

    async def get_session_by_token_hash(self, token_hash: str) -> Row | None:
        """Fetch session row by refresh_token_hash."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, user_id, sid, expires_at, revoked "
                "FROM auth_sessions WHERE refresh_token_hash = $1",
                token_hash,
            )
        return dict(row) if row else None

    async def session_is_live(self, user_id: str, sid: str) -> bool:
        """Whether *user_id*'s session *sid* exists, unrevoked and unexpired.

        One probe of ``idx_auth_sessions_user_sid`` (user_id, sid), however
        many sessions the account holds.
        """
        async with self._request_connection() as conn:
            return bool(
                await conn.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM auth_sessions "
                    "WHERE user_id = $1 AND expires_at > NOW() AND sid = $2 "
                    "AND revoked IS NOT TRUE)",
                    user_id,
                    sid,
                )
            )

    async def rotate_session(
        self,
        session_id: str,
        *,
        new_refresh_token_hash: str,
        new_jti: str,
    ) -> None:
        """Update the session with a new refresh token hash and JTI."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE auth_sessions "
                "SET last_used_at = NOW(), jti = $1, refresh_token_hash = $2 "
                "WHERE id = $3",
                new_jti,
                new_refresh_token_hash,
                session_id,
            )

    async def revoke_session(self, session_id: str) -> str | None:
        """Set ``revoked = TRUE`` on the session; return its account, or None if none."""
        async with self._pool.acquire() as conn:
            return await conn.fetchval(
                "UPDATE auth_sessions SET revoked = TRUE WHERE id = $1 RETURNING user_id",
                session_id,
            )

    async def delete_user_sessions(self, user_id: str) -> None:
        """Delete all sessions for a user."""
        async with self._pool.acquire() as conn:
            await conn.execute("DELETE FROM auth_sessions WHERE user_id = $1", user_id)

    async def revoke_user_sessions(self, user_id: str, *, except_sid: str | None = None) -> int:
        """Revoke every live session of *user_id* but the one whose sid is *except_sid*.

        The rows stay, so a browser that listed the account shows it signed
        out rather than forgetting it.
        """
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE auth_sessions SET revoked = TRUE "
                "WHERE user_id = $1 AND revoked IS NOT TRUE AND sid IS DISTINCT FROM $2::text",
                user_id,
                except_sid,
            )
        return int(result.split()[-1])

    async def revoke_session_by_sid(self, user_id: str, sid: str) -> bool:
        """Revoke *user_id*'s session *sid*, found through the account's sessions."""
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE auth_sessions SET revoked = TRUE "
                "WHERE user_id = $1 AND sid = $2 AND revoked IS NOT TRUE",
                user_id,
                sid,
            )
        return int(result.split()[-1]) > 0

    # -- browser sessions ----------------------------------------------------

    async def create_browser_session(
        self, *, browser_session_id: str, secret_hash: str, expires_at: datetime
    ) -> None:
        """Insert a browser session with no accounts yet."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO browser_sessions (id, secret_hash, expires_at) VALUES ($1, $2, $3)",
                browser_session_id,
                secret_hash,
                expires_at,
            )

    async def get_browser_session(self, secret_hash: str) -> Row | None:
        """Fetch a browser session by the hash of its cookie."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, active_user_id, expires_at, revoked "
                "FROM browser_sessions WHERE secret_hash = $1",
                secret_hash,
            )
        return dict(row) if row else None

    async def rotate_browser_session(
        self,
        browser_session_id: str,
        *,
        current_secret_hash: str,
        new_secret_hash: str,
        expires_at: datetime | None = None,
    ) -> bool:
        """Replace the cookie hash if it is still *current_secret_hash*."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE browser_sessions SET secret_hash = $1, last_used_at = NOW(), "
                "expires_at = GREATEST(expires_at, COALESCE($2, expires_at)) "
                "WHERE id = $3 AND secret_hash = $4 AND NOT revoked RETURNING id",
                new_secret_hash,
                expires_at,
                browser_session_id,
                current_secret_hash,
            )
        return row is not None

    async def list_browser_session_accounts(self, browser_session_id: str) -> list[Row]:
        """Return the accounts signed in to a browser, oldest first."""
        identity = _JOINED_USER_IDENTITY
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT a.user_id, a.auth_session_id, a.added_at, "
                "s.sid, s.expires_at AS session_expires_at, s.revoked AS session_revoked, "
                f"{identity}, u.user_name, u.role, "
                "u.status, u.email_verified, u.password_hash IS NOT NULL AS has_password, "
                "u.created_at, u.last_login_at "
                "FROM browser_session_accounts a "
                "LEFT JOIN auth_sessions s ON s.id = a.auth_session_id "
                "LEFT JOIN users u ON u.id = a.user_id "
                "WHERE a.browser_session_id = $1 ORDER BY a.added_at, a.user_id",
                browser_session_id,
            )
        return [dict(row) for row in rows]

    async def attach_browser_session_account(
        self, browser_session_id: str, *, user_id: str, auth_session_id: str
    ) -> bool:
        """Make *auth_session_id* the browser's session for *user_id*, and its active account.

        A session the browser already held for the same account is revoked:
        signing in again replaces it, and nothing can reach the old one.
        Returns False, changing nothing, when the browser session has been
        retired; the browser row lock orders this against a sign-out that
        retires it.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            revoked = await conn.fetchval(
                "SELECT revoked FROM browser_sessions WHERE id = $1 FOR UPDATE",
                browser_session_id,
            )
            if revoked is None or revoked:
                return False
            previous = await conn.fetchval(
                "SELECT auth_session_id FROM browser_session_accounts "
                "WHERE browser_session_id = $1 AND user_id = $2 FOR UPDATE",
                browser_session_id,
                user_id,
            )
            await conn.execute(
                "INSERT INTO browser_session_accounts (browser_session_id, user_id, auth_session_id) "
                "VALUES ($1, $2, $3) ON CONFLICT (browser_session_id, user_id) "
                "DO UPDATE SET auth_session_id = EXCLUDED.auth_session_id",
                browser_session_id,
                user_id,
                auth_session_id,
            )
            if previous is not None and previous != auth_session_id:
                await conn.execute(
                    "UPDATE auth_sessions SET revoked = TRUE WHERE id = $1", previous
                )
            await conn.execute(
                "UPDATE browser_sessions SET active_user_id = $1, last_used_at = NOW() "
                "WHERE id = $2",
                user_id,
                browser_session_id,
            )
        return True

    async def set_browser_session_active(
        self, browser_session_id: str, user_id: str | None
    ) -> None:
        """Choose the browser's active account, or none."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE browser_sessions SET active_user_id = $1, last_used_at = NOW() "
                "WHERE id = $2",
                user_id,
                browser_session_id,
            )

    async def detach_browser_session_account(self, browser_session_id: str, user_id: str) -> int:
        """Sign *user_id* out of one browser; return how many accounts remain there.

        A browser left with no account is retired in the same transaction,
        under the browser row lock a sign-in also takes, so a sign-in racing
        it either lands before (and the browser is kept) or finds it retired.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "SELECT 1 FROM browser_sessions WHERE id = $1 FOR UPDATE", browser_session_id
            )
            auth_session_id = await conn.fetchval(
                "DELETE FROM browser_session_accounts "
                "WHERE browser_session_id = $1 AND user_id = $2 RETURNING auth_session_id",
                browser_session_id,
                user_id,
            )
            if auth_session_id is not None:
                await conn.execute(
                    "UPDATE auth_sessions SET revoked = TRUE WHERE id = $1", auth_session_id
                )
            await conn.execute(
                "UPDATE browser_sessions SET active_user_id = NULL "
                "WHERE id = $1 AND active_user_id = $2",
                browser_session_id,
                user_id,
            )
            remaining = int(
                await conn.fetchval(
                    "SELECT COUNT(*) FROM browser_session_accounts WHERE browser_session_id = $1",
                    browser_session_id,
                )
                or 0
            )
            if remaining == 0:
                await conn.execute(
                    "UPDATE browser_sessions SET revoked = TRUE, active_user_id = NULL "
                    "WHERE id = $1",
                    browser_session_id,
                )
        return remaining

    async def revoke_browser_session(self, browser_session_id: str) -> list[str]:
        """Sign every account out of one browser and retire its cookie.

        Returns the accounts whose sessions were revoked.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            # Same lock as attach: a racing sign-in starts a fresh browser.
            await conn.execute(
                "SELECT 1 FROM browser_sessions WHERE id = $1 FOR UPDATE", browser_session_id
            )
            revoked = await conn.fetch(
                "UPDATE auth_sessions SET revoked = TRUE WHERE id IN ("
                "SELECT auth_session_id FROM browser_session_accounts "
                "WHERE browser_session_id = $1) RETURNING user_id",
                browser_session_id,
            )
            await conn.execute(
                "DELETE FROM browser_session_accounts WHERE browser_session_id = $1",
                browser_session_id,
            )
            await conn.execute(
                "UPDATE browser_sessions SET revoked = TRUE, active_user_id = NULL WHERE id = $1",
                browser_session_id,
            )
        return sorted({row["user_id"] for row in revoked})

    # -- login events --------------------------------------------------------

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
        """Insert one ``login_events`` row."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO login_events
                    (user_id, email, outcome, failure_reason, ip, user_agent)
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                user_id,
                email,
                outcome,
                failure_reason,
                ip,
                user_agent,
            )

    async def purge_login_events_older_than(self, days: int) -> int:
        """Delete rows older than ``days`` days. Returns the deleted count."""
        async with self._pool.acquire() as conn:
            status = await conn.execute(
                "DELETE FROM login_events "
                "WHERE created_at < NOW() - ($1::int || ' days')::interval",
                days,
            )
        try:
            return int(status.rsplit(" ", 1)[-1])
        except (ValueError, IndexError):
            return 0

    async def purge_login_events_for_user(self, user_id: str) -> int:
        """Delete all rows for ``user_id``. Returns the deleted count."""
        async with self._pool.acquire() as conn:
            status = await conn.execute("DELETE FROM login_events WHERE user_id = $1", user_id)
        try:
            return int(status.rsplit(" ", 1)[-1])
        except (ValueError, IndexError):
            return 0

    # -- email verification tokens -------------------------------------------

    async def create_verification_token(
        self,
        *,
        token: str,
        user_id: str,
        expires_at: datetime,
        pending_email: str | None = None,
    ) -> None:
        """Insert a new email verification token."""
        async with self._pool.acquire() as conn:
            if pending_email is not None:
                await conn.execute(
                    "INSERT INTO email_verification_tokens "
                    "(token, user_id, expires_at, pending_email) "
                    "VALUES ($1, $2, $3, $4)",
                    token,
                    user_id,
                    expires_at,
                    pending_email,
                )
            else:
                await conn.execute(
                    "INSERT INTO email_verification_tokens (token, user_id, expires_at) "
                    "VALUES ($1, $2, $3)",
                    token,
                    user_id,
                    expires_at,
                )

    async def get_verification_token(self, token: str) -> Row | None:
        """Fetch verification token row."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT token, user_id, created_at, expires_at, used_at, pending_email "
                "FROM email_verification_tokens WHERE token = $1",
                token,
            )
        return dict(row) if row else None

    async def get_live_verification_token(
        self, user_id: str, *, valid_until: datetime
    ) -> str | None:
        """Return the newest unused signup verification token valid past *valid_until*."""
        async with self._pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT token FROM email_verification_tokens "
                "WHERE user_id = $1 AND used_at IS NULL AND pending_email IS NULL "
                "AND expires_at > $2 ORDER BY expires_at DESC LIMIT 1",
                user_id,
                valid_until,
            )

    async def mark_verification_used(self, token: str) -> None:
        """Set ``used_at = NOW()`` on the token."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE email_verification_tokens SET used_at = NOW() WHERE token = $1",
                token,
            )

    async def mark_user_email_verified(self, user_id: str) -> None:
        """Set ``email_verified = TRUE`` on the user."""
        async with self._pool.acquire() as conn, conn.transaction():
            await _refuse_claimed_account(conn, user_id)
            await conn.execute("UPDATE users SET email_verified = TRUE WHERE id = $1", user_id)

    async def delete_user_verification_tokens(self, user_id: str) -> None:
        """Delete all verification tokens for a user."""
        async with self._pool.acquire() as conn:
            await conn.execute("DELETE FROM email_verification_tokens WHERE user_id = $1", user_id)

    # -- password reset tokens -----------------------------------------------

    async def create_reset_token(
        self,
        *,
        token: str,
        user_id: str,
        expires_at: datetime,
    ) -> None:
        """Insert a new password reset token."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO password_reset_tokens (token, user_id, expires_at) "
                "VALUES ($1, $2, $3)",
                token,
                user_id,
                expires_at,
            )

    async def get_reset_token(self, token: str) -> Row | None:
        """Fetch reset token row."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT token, user_id, created_at, expires_at, used_at "
                "FROM password_reset_tokens WHERE token = $1",
                token,
            )
        return dict(row) if row else None

    async def get_live_reset_token(self, user_id: str, *, valid_until: datetime) -> str | None:
        """Return the newest unused password reset token valid past *valid_until*."""
        async with self._pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT token FROM password_reset_tokens "
                "WHERE user_id = $1 AND used_at IS NULL AND expires_at > $2 "
                "ORDER BY expires_at DESC LIMIT 1",
                user_id,
                valid_until,
            )

    async def mark_reset_used(self, token: str) -> None:
        """Set ``used_at = NOW()`` on the token."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE password_reset_tokens SET used_at = NOW() WHERE token = $1",
                token,
            )

    async def delete_user_reset_tokens(self, user_id: str) -> None:
        """Delete all reset tokens for a user."""
        async with self._pool.acquire() as conn:
            await conn.execute("DELETE FROM password_reset_tokens WHERE user_id = $1", user_id)

    async def redeem_reset_token(self, token: str, *, password_hash: str) -> ResetRedemption:
        """Spend reset link *token* on *password_hash*; see the base class.

        The account row is locked before any link is read under a lock, as the
        account deletions lock it before deleting its links and sessions, so
        one account's redemptions take turns in that order and cannot deadlock
        with each other or with a deletion. The link is then read again under
        the lock: a redemption that waited finds it gone, spent with every
        sibling by the one that went first.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            owner = await conn.fetchval(
                "SELECT user_id FROM password_reset_tokens WHERE token = $1", token
            )
            if owner is None:
                return ResetRedemption("invalid")
            if await conn.fetchval("SELECT id FROM users WHERE id = $1 FOR UPDATE", owner) is None:
                return ResetRedemption("invalid")
            link = await conn.fetchrow(
                "SELECT used_at, expires_at < NOW() AS expired "
                "FROM password_reset_tokens WHERE token = $1 AND user_id = $2 FOR UPDATE",
                token,
                owner,
            )
            if link is None:
                return ResetRedemption("invalid")
            if link["used_at"] is not None:
                return ResetRedemption("used")
            if link["expired"]:
                return ResetRedemption("expired")
            await conn.execute(
                "UPDATE users SET password_hash = $2 WHERE id = $1", owner, password_hash
            )
            await conn.execute("DELETE FROM password_reset_tokens WHERE user_id = $1", owner)
            await conn.execute("DELETE FROM auth_sessions WHERE user_id = $1", owner)
        return ResetRedemption("reset", owner)

    # -- identity authorization codes ----------------------------------------

    async def create_identity_auth_code(
        self,
        *,
        code_hash: str,
        user_id: str,
        client_id: str,
        redirect_uri: str,
        code_challenge: str,
        expires_at: datetime,
    ) -> None:
        """Insert a one-time cross-service authorization code."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO identity_auth_codes "
                "(code_hash, user_id, client_id, redirect_uri, code_challenge, expires_at) "
                "VALUES ($1, $2, $3, $4, $5, $6)",
                code_hash,
                user_id,
                client_id,
                redirect_uri,
                code_challenge,
                expires_at,
            )
            # Codes live 60 seconds and are written once per sign-in, so the
            # table would grow forever for no reason. Sweeping here keeps it
            # bounded without a scheduled job to own and monitor.
            await conn.execute(
                "DELETE FROM identity_auth_codes WHERE expires_at < NOW() - INTERVAL '1 hour'"
            )

    async def consume_identity_auth_code(self, code_hash: str) -> Row | None:
        """Atomically claim an unused, unexpired authorization code."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE identity_auth_codes SET used_at = NOW() "
                "WHERE code_hash = $1 AND used_at IS NULL AND expires_at > NOW() "
                "RETURNING user_id, client_id, redirect_uri, code_challenge",
                code_hash,
            )
        return dict(row) if row else None

    # -- inference grants ----------------------------------------------------

    async def get_quota_context_for_user(self, user_id: str) -> list[Row]:
        """Return the account's quota row: one row when it holds a live key, else none.

        An account may hold several active keys, all carrying the account's
        quota (see :func:`_sync_account_quota`), so any one of them answers;
        the oldest is taken so repeated lookups read the same row.
        """
        async with self._request_connection() as conn:
            rows = await conn.fetch(
                "SELECT k.id, k.user_id, p.id AS project_id, p.org_id, "
                "p.name AS project_name, p.allowed_models AS project_allowed_models, "
                "p.budget_daily_usd AS project_budget_daily_usd, "
                "p.budget_monthly_usd AS project_budget_monthly_usd, "
                "p.max_concurrent_requests AS project_max_concurrent_requests, "
                "o.max_concurrent_requests AS org_max_concurrent_requests, "
                f"{_SCOPE_SPEND_COLUMNS}, "
                "COALESCE(o.budget_daily_usd, k.quota_daily_cost_usd) AS quota_daily_cost_usd, "
                "COALESCE(o.budget_monthly_usd, k.quota_monthly_cost_usd) AS quota_monthly_cost_usd, u.role "
                "FROM api_keys k "
                "LEFT JOIN users u ON u.id = k.user_id "
                "LEFT JOIN organizations o ON o.id = k.org_id "
                "LEFT JOIN projects p ON p.org_id = o.id AND p.is_default AND p.status = 'active' "
                f"{_SCOPE_SPEND_JOIN.replace('k.project_id', 'p.id')}"
                "WHERE k.user_id = $1 "
                "  AND k.status = 'active' "
                "  AND (k.expires_at IS NULL OR k.expires_at > NOW()) "
                "  AND u.id IS NOT NULL AND u.status = 'active' "
                "  AND (o.id IS NULL OR o.status = 'active') "
                "ORDER BY k.created_at, k.id "
                "LIMIT 1",
                user_id,
            )
        return [_coerce_user_row(row) for row in rows]

    async def upsert_agent_grant(
        self,
        *,
        grant_id: str,
        user_id: str,
        external_job_id: str,
        external_attempt_id: str,
        allowed_models: list[str],
        expires_at: datetime,
    ) -> Row:
        """Create a grant, or return the existing one for this attempt."""
        async with self._pool.acquire() as conn:
            # DO NOTHING rather than DO UPDATE: a retry must not be able to
            # extend or re-scope a grant that already exists. The first mint
            # for an attempt is the one that counts; renewal is its own,
            # separately authorized call.
            row = await conn.fetchrow(
                "INSERT INTO agent_grants "
                "(grant_id, user_id, external_job_id, external_attempt_id, "
                " allowed_models, expires_at) "
                "VALUES ($1, $2, $3, $4, $5::jsonb, $6) "
                "ON CONFLICT (external_job_id, external_attempt_id) DO NOTHING "
                "RETURNING grant_id, user_id, external_job_id, external_attempt_id, "
                "          allowed_models, created_at, expires_at, revoked_at",
                grant_id,
                user_id,
                external_job_id,
                external_attempt_id,
                json.dumps(list(allowed_models)),
                expires_at,
            )
            if row is None:
                row = await conn.fetchrow(
                    "SELECT grant_id, user_id, external_job_id, external_attempt_id, "
                    "       allowed_models, created_at, expires_at, revoked_at "
                    "FROM agent_grants WHERE external_job_id = $1 AND external_attempt_id = $2",
                    external_job_id,
                    external_attempt_id,
                )
        if row is None:
            # The insert conflicted, so a row for this attempt exists — unless
            # the table lost its unique index, in which case DO NOTHING fired
            # for some other reason. Say that, rather than failing later on a
            # None nobody expected.
            raise RuntimeError(
                "agent_grants upsert matched no row for "
                f"({external_job_id}, {external_attempt_id}); is the unique index present?"
            )
        return _decode_grant(row)

    async def get_agent_grant(self, grant_id: str) -> Row | None:
        """Fetch a grant row by id, live or not."""
        async with self._request_connection() as conn:
            row = await conn.fetchrow(
                "SELECT grant_id, user_id, external_job_id, external_attempt_id, "
                "       allowed_models, created_at, expires_at, revoked_at "
                "FROM agent_grants WHERE grant_id = $1",
                grant_id,
            )
        return _decode_grant(row) if row else None

    async def renew_agent_grant(self, grant_id: str, *, expires_at: datetime) -> Row | None:
        """Extend a live grant's expiry, returning the updated row."""
        async with self._pool.acquire() as conn:
            # Liveness is in the WHERE clause, not a prior read: a grant that
            # is revoked or lapsed between check and write must not be renewed.
            row = await conn.fetchrow(
                "UPDATE agent_grants SET expires_at = $2 "
                "WHERE grant_id = $1 AND revoked_at IS NULL AND expires_at > NOW() "
                "RETURNING grant_id, user_id, external_job_id, external_attempt_id, "
                "          allowed_models, created_at, expires_at, revoked_at",
                grant_id,
                expires_at,
            )
        return _decode_grant(row) if row else None

    async def revoke_agent_grant(self, grant_id: str) -> bool:
        """Mark a grant revoked. Returns whether this call did the revoking."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE agent_grants SET revoked_at = NOW() "
                "WHERE grant_id = $1 AND revoked_at IS NULL "
                "RETURNING grant_id",
                grant_id,
            )
        return row is not None

    # -- admin audit log -----------------------------------------------------

    async def log_admin_action(
        self,
        *,
        admin_ip: str | None = None,
        action: str,
        target_user_id: str | None = None,
        details: dict[str, Any] | None = None,
        success: bool = True,
        actor: AuditActor | None = None,
        target: AuditTarget | None = None,
    ) -> None:
        """Insert a row into admin_audit_log, attributed to *actor* and *target*."""
        columns = audit_columns(
            admin_ip=admin_ip, target_user_id=target_user_id, actor=actor, target=target
        )
        async with self._pool.acquire() as conn:
            await insert_audit_row(
                conn, columns=columns, action=action, details=details, success=success
            )

    async def list_audit_log(
        self,
        *,
        action: str | None = None,
        target_user_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[int, list[Row]]:
        """Return ``(total_count, audit_rows)`` with optional filters."""
        where_clauses: list[str] = []
        params: list[Any] = []

        if action:
            where_clauses.append(f"action = ${len(params) + 1}")
            params.append(action)
        if target_user_id:
            where_clauses.append(f"target_user_id = ${len(params) + 1}")
            params.append(target_user_id)

        where_sql = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""
        params.append(limit)
        params.append(offset)

        async with self._pool.acquire() as conn:
            count_row = await conn.fetchrow(
                f"SELECT COUNT(*) as total FROM admin_audit_log {where_sql}",
                *params[: len(params) - 2],
            )
            total = count_row["total"] if count_row else 0

            rows = await conn.fetch(
                f"SELECT {AUDIT_SELECT_COLUMNS} FROM admin_audit_log "
                f"{where_sql} ORDER BY timestamp DESC "
                f"LIMIT ${len(params) - 1} OFFSET ${len(params)}",
                *params,
            )

        return total, [dict(r) for r in rows]

    # -- user preferences ----------------------------------------------------

    async def get_user_preferences(self, user_id: str) -> dict[str, Any]:
        """Return the preferences JSONB column for *user_id*, parsed as dict.

        An oversized document (see ``_PREFERENCES_READ_MAX_BYTES``) reads as its
        request entries. The CASE tries the stored size first, which costs no
        read of the value, and measures the JSON text only of a document that
        passes it.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT CASE "
                f"WHEN pg_column_size(preferences) > $2 THEN {_USER_ROW_PREFERENCES} "
                f"WHEN octet_length(preferences::text) > $2 THEN {_USER_ROW_PREFERENCES} "
                "ELSE preferences END AS preferences FROM users WHERE id = $1",
                user_id,
                _PREFERENCES_READ_MAX_BYTES,
            )
        coerced = _coerce_user_row(row)
        return coerced["preferences"] if coerced else {}

    async def set_user_preference(
        self, user_id: str, key: str, value: Any, *, max_total_bytes: int | None = None
    ) -> bool:
        """Set one preference key in place, leaving every other key as it is.

        The size bound is in the UPDATE's WHERE, so it is measured on the
        document as it is when the write lands, other keys' concurrent writes
        included.
        """
        updated = f"jsonb_set({_PREFERENCES_OBJECT}, ARRAY[$2::text], $3::jsonb, true)"
        async with self._pool.acquire() as conn, conn.transaction():
            await _refuse_claimed_account(conn, user_id)
            if max_total_bytes is None:
                status = await conn.execute(
                    f"UPDATE users SET preferences = {updated} WHERE id = $1",
                    user_id,
                    key,
                    json.dumps(value),
                )
                return status == "UPDATE 1"
            written = await conn.fetchval(
                f"UPDATE users SET preferences = {updated} "
                f"WHERE id = $1 AND octet_length({updated}::text) <= $4 RETURNING true",
                user_id,
                key,
                json.dumps(value),
                max_total_bytes,
            )
            if written:
                return True
            exists = await conn.fetchval("SELECT true FROM users WHERE id = $1", user_id)
        if exists:
            raise PreferencesTooLargeError(max_total_bytes)
        return False

    async def delete_user_preference(self, user_id: str, key: str) -> bool:
        """Remove one preference key in place, leaving every other key as it is."""
        async with self._pool.acquire() as conn, conn.transaction():
            await _refuse_claimed_account(conn, user_id)
            status = await conn.execute(
                f"UPDATE users SET preferences = {_PREFERENCES_OBJECT} - $2::text WHERE id = $1",
                user_id,
                key,
            )
        return status == "UPDATE 1"

    async def set_user_preference_if_newer(
        self,
        user_id: str,
        key: str,
        value: dict[str, Any],
        chosen_at: datetime,
    ) -> dict[str, Any] | None:
        """Set a timestamped preference unless the stored one is at least as recent.

        The comparison is in the UPDATE's WHERE, so it is re-checked against
        the latest row version when a concurrent update holds the row: an
        older choice cannot overwrite a newer one by landing second. A stored
        ``chosen_at`` that is not an ISO 8601 timestamp counts as no choice
        (the CASE keeps the cast from running on it).
        """
        async with self._pool.acquire() as conn, conn.transaction():
            await _refuse_claimed_account(conn, user_id)
            updated = await conn.fetchval(
                f"UPDATE users SET preferences = jsonb_set({_PREFERENCES_OBJECT}, "
                "ARRAY[$2::text], $3::jsonb, true) "
                "WHERE id = $1 AND NOT COALESCE(CASE "
                "  WHEN preferences -> $2::text ->> 'chosen_at' ~ $5 "
                "  THEN (preferences -> $2::text ->> 'chosen_at')::timestamptz >= $4 "
                "END, false) "
                "RETURNING preferences -> $2::text",
                user_id,
                key,
                json.dumps(value),
                chosen_at,
                _ISO_TIMESTAMP_PATTERN,
            )
            if updated is None:
                # Not written: either there is no such user, or a newer choice
                # is stored. Read it in a new statement, which sees it committed.
                updated = await conn.fetchval(
                    "SELECT preferences -> $2::text FROM users WHERE id = $1", user_id, key
                )
        if updated is None:
            return None
        stored = json.loads(updated) if isinstance(updated, str) else updated
        return stored if isinstance(stored, dict) else None

    async def get_user_avatar(self, user_id: str) -> str | None:
        """Return the user's portrait data URL, or None."""
        async with self._pool.acquire() as conn:
            return await conn.fetchval("SELECT avatar_data_url FROM users WHERE id = $1", user_id)

    async def set_user_avatar(self, user_id: str, avatar_data_url: str | None) -> None:
        """Replace the user's portrait; None removes it.

        The account row is locked before the write, and a pending hard-delete
        claim refuses it: the account is being erased, so nothing about it may
        change — the portrait included. The portrait is not written through
        :meth:`update_user_fields`, so the guard is stated here rather than
        inherited.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            await _refuse_claimed_account(conn, user_id)
            await conn.execute(
                "UPDATE users SET avatar_data_url = $1 WHERE id = $2", avatar_data_url, user_id
            )

    # -- signup domain allowlist --------------------------------------------

    async def list_signup_allowed_domains(self) -> list[Row]:
        """Return all allowlist rows joined with the creator's email."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT d.domain, d.is_wildcard, d.created_at, d.created_by, "
                "u.email AS created_by_email "
                "FROM signup_allowed_domains d "
                "LEFT JOIN users u ON u.id = d.created_by "
                "ORDER BY d.created_at DESC, d.domain ASC"
            )
        return [dict(r) for r in rows]

    async def add_signup_allowed_domain(
        self,
        *,
        domain: str,
        is_wildcard: bool,
        created_by: str | None,
    ) -> Row:
        """Insert a new allowlist entry. Raises on duplicate composite key."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO signup_allowed_domains (domain, is_wildcard, created_by) "
                "VALUES ($1, $2, $3) "
                "RETURNING domain, is_wildcard, created_at, created_by",
                domain,
                is_wildcard,
                created_by,
            )
        # Backfill creator email so the response shape matches list_*.
        result = dict(row) if row else {}
        if result.get("created_by"):
            async with self._pool.acquire() as conn:
                creator = await conn.fetchrow(
                    "SELECT email FROM users WHERE id = $1", result["created_by"]
                )
            result["created_by_email"] = creator["email"] if creator else None
        else:
            result["created_by_email"] = None
        return result

    async def remove_signup_allowed_domain(
        self,
        *,
        domain: str,
        is_wildcard: bool,
    ) -> bool:
        """Delete an allowlist entry; returns True when a row was removed."""
        async with self._pool.acquire() as conn:
            tag = await conn.execute(
                "DELETE FROM signup_allowed_domains WHERE domain = $1 AND is_wildcard = $2",
                domain,
                is_wildcard,
            )
        return _parse_command_tag_count(tag) > 0

    async def signup_allowlist_is_empty(self) -> bool:
        """Return True if the allowlist table has no rows."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT 1 FROM signup_allowed_domains LIMIT 1")
        return row is None

    async def is_signup_domain_allowed(self, email: str) -> bool:
        """Match *email*'s domain against the allowlist (exact or wildcard).

        Uses a single round-trip: builds the set of candidate strings
        (the email domain itself, plus each parent suffix that could be
        a wildcard match) and checks whether any allowlist row matches
        with the appropriate ``is_wildcard`` flag. The bare top-level
        domain is excluded from wildcard candidates so ``*.acme.com``
        does not match ``alice@acme.com``.
        """
        if "@" not in email:
            return False
        domain = email.rsplit("@", 1)[1].strip().lower()
        if not domain:
            return False

        parts = domain.split(".")
        # Wildcard candidates: parent suffixes that *.suffix would match.
        # For a.b.example.com (parts=4): b.example.com, example.com.
        # Stops at len(parts)-1 to exclude the bare TLD.
        wildcard_candidates = [".".join(parts[i:]) for i in range(1, len(parts) - 1)]
        # Single query covers both exact and wildcard checks: for each
        # returned row, decide acceptance based on its is_wildcard flag.
        candidates = list({domain, *wildcard_candidates})
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT domain, is_wildcard FROM signup_allowed_domains "
                "WHERE domain = ANY($1::text[])",
                candidates,
            )
        wildcard_set = set(wildcard_candidates)
        for row in rows:
            row_domain = row["domain"]
            row_is_wildcard = bool(row["is_wildcard"])
            if not row_is_wildcard and row_domain == domain:
                return True
            if row_is_wildcard and row_domain in wildcard_set:
                return True
        return False

    # -- site settings --------------------------------------------------------

    async def get_setting(self, key: str) -> Row | None:
        """Fetch a single site_settings row by key."""
        async with self._request_connection() as conn:
            row = await conn.fetchrow(
                "SELECT key, value, value_type, updated_at, updated_by "
                "FROM site_settings WHERE key = $1",
                key,
            )
        return dict(row) if row else None

    async def set_setting(
        self, key: str, value: str, value_type: str, updated_by: str | None
    ) -> None:
        """Upsert a site_settings row."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO site_settings (key, value, value_type, updated_at, updated_by) "
                "VALUES ($1, $2, $3, NOW(), $4) "
                "ON CONFLICT (key) DO UPDATE SET "
                "value = $2, value_type = $3, updated_at = NOW(), updated_by = $4",
                key,
                value,
                value_type,
                updated_by,
            )

    async def delete_setting(self, key: str) -> bool:
        """Delete a site_settings row by key. Returns True when removed."""
        async with self._pool.acquire() as conn:
            tag = await conn.execute(
                "DELETE FROM site_settings WHERE key = $1",
                key,
            )
        return _parse_command_tag_count(tag) > 0

    async def compare_and_set_setting(
        self,
        key: str,
        expected: str | None,
        value: str,
        value_type: str,
        updated_by: str | None,
    ) -> bool:
        """Keep a delayed external fetch from overwriting a newer published value."""
        async with self._pool.acquire() as conn:
            if expected is None:
                result = await conn.fetchval(
                    "INSERT INTO site_settings (key, value, value_type, updated_at, updated_by) "
                    "VALUES ($1, $2, $3, NOW(), $4) ON CONFLICT (key) DO NOTHING RETURNING key",
                    key,
                    value,
                    value_type,
                    updated_by,
                )
            else:
                result = await conn.fetchval(
                    "UPDATE site_settings SET value = $3, value_type = $4, "
                    "updated_at = NOW(), updated_by = $5 WHERE key = $1 AND value = $2 "
                    "RETURNING key",
                    key,
                    expected,
                    value,
                    value_type,
                    updated_by,
                )
        return result is not None

    async def list_settings(self) -> list[Row]:
        """Return all site_settings rows ordered by key."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT key, value, value_type, updated_at, updated_by "
                "FROM site_settings ORDER BY key"
            )
        return [dict(r) for r in rows]

    async def get_model_visibility_override(self, model_id: str) -> Row | None:
        """Fetch a single model_visibility_overrides row by model_id."""
        async with self._request_connection() as conn:
            row = await conn.fetchrow(
                "SELECT model_id, required_role, updated_at, updated_by "
                "FROM model_visibility_overrides WHERE model_id = $1",
                model_id,
            )
        return dict(row) if row else None

    async def set_model_visibility_override(
        self,
        model_id: str,
        required_role: str,
        updated_by: str | None,
    ) -> None:
        """Upsert a model visibility override row."""
        if required_role not in VALID_ROLES:
            raise ValueError(f"Invalid required_role: {required_role}")
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO model_visibility_overrides "
                "(model_id, required_role, updated_at, updated_by) "
                "VALUES ($1, $2, NOW(), $3) "
                "ON CONFLICT (model_id) DO UPDATE SET "
                "required_role = EXCLUDED.required_role, "
                "updated_at = NOW(), "
                "updated_by = EXCLUDED.updated_by",
                model_id,
                required_role,
                updated_by,
            )

    async def delete_model_visibility_override(self, model_id: str) -> bool:
        """Delete a model visibility override row. Returns True when removed."""
        async with self._pool.acquire() as conn:
            tag = await conn.execute(
                "DELETE FROM model_visibility_overrides WHERE model_id = $1",
                model_id,
            )
        return _parse_command_tag_count(tag) > 0

    async def list_model_visibility_overrides(self) -> list[Row]:
        """Return all model visibility override rows ordered by model_id."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT model_id, required_role, updated_at, updated_by "
                "FROM model_visibility_overrides ORDER BY model_id"
            )
        return [dict(r) for r in rows]

    async def list_disabled_providers(self) -> list[Row]:
        """Return all disabled-provider rows ordered by provider."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT provider, updated_at, updated_by FROM disabled_providers ORDER BY provider"
            )
        return [dict(r) for r in rows]

    async def set_provider_disabled(self, provider: str, updated_by: str | None) -> None:
        """Mark an upstream provider disabled (upsert)."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO disabled_providers (provider, updated_at, updated_by) "
                "VALUES ($1, NOW(), $2) "
                "ON CONFLICT (provider) DO UPDATE SET "
                "updated_at = NOW(), updated_by = EXCLUDED.updated_by",
                provider,
                updated_by,
            )

    async def clear_provider_disabled(self, provider: str) -> bool:
        """Re-enable a provider by deleting its row. Returns True if removed."""
        async with self._pool.acquire() as conn:
            tag = await conn.execute(
                "DELETE FROM disabled_providers WHERE provider = $1",
                provider,
            )
        return _parse_command_tag_count(tag) > 0

    async def get_model_concurrency_exemption(self, model_id: str) -> Row | None:
        """Fetch a single model_concurrency_exemptions row by model_id."""
        async with self._request_connection() as conn:
            row = await conn.fetchrow(
                "SELECT model_id, updated_at, updated_by "
                "FROM model_concurrency_exemptions WHERE model_id = $1",
                model_id,
            )
        return dict(row) if row else None

    async def set_model_concurrency_exemption(
        self,
        model_id: str,
        updated_by: str | None,
    ) -> None:
        """Upsert a model concurrency exemption row (presence of row = exempt)."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO model_concurrency_exemptions "
                "(model_id, updated_at, updated_by) "
                "VALUES ($1, NOW(), $2) "
                "ON CONFLICT (model_id) DO UPDATE SET "
                "updated_at = NOW(), "
                "updated_by = EXCLUDED.updated_by",
                model_id,
                updated_by,
            )

    async def delete_model_concurrency_exemption(self, model_id: str) -> bool:
        """Delete a model concurrency exemption row. Returns True when removed."""
        async with self._pool.acquire() as conn:
            tag = await conn.execute(
                "DELETE FROM model_concurrency_exemptions WHERE model_id = $1",
                model_id,
            )
        return _parse_command_tag_count(tag) > 0

    async def list_model_concurrency_exemptions(self) -> list[Row]:
        """Return all model concurrency exemption rows ordered by model_id."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT model_id, updated_at, updated_by "
                "FROM model_concurrency_exemptions ORDER BY model_id"
            )
        return [dict(r) for r in rows]

    async def list_weight_overrides_for_model(self, model_id: str) -> list[Row]:
        """Return provider weight override rows for a model ordered by endpoint_id."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT model_id, endpoint_id, weight, updated_at, updated_by "
                "FROM provider_weight_overrides WHERE model_id = $1 ORDER BY endpoint_id",
                model_id,
            )
        return [dict(r) for r in rows]

    async def list_all_weight_overrides(self) -> list[Row]:
        """Return all provider weight override rows ordered by model_id and endpoint_id."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT model_id, endpoint_id, weight, updated_at, updated_by "
                "FROM provider_weight_overrides ORDER BY model_id, endpoint_id"
            )
        return [dict(r) for r in rows]

    async def upsert_weight_override(
        self,
        model_id: str,
        endpoint_id: str,
        weight: float,
        updated_by: str | None,
    ) -> None:
        """Upsert a provider weight override row."""
        if weight < 0:
            raise ValueError("weight must be >= 0")
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO provider_weight_overrides "
                "(model_id, endpoint_id, weight, updated_at, updated_by) "
                "VALUES ($1, $2, $3, NOW(), $4) "
                "ON CONFLICT (model_id, endpoint_id) DO UPDATE SET "
                "weight = EXCLUDED.weight, "
                "updated_at = NOW(), "
                "updated_by = EXCLUDED.updated_by",
                model_id,
                endpoint_id,
                weight,
                updated_by,
            )

    async def delete_weight_override(self, model_id: str, endpoint_id: str) -> bool:
        """Delete a provider weight override row. Returns True when removed."""
        async with self._pool.acquire() as conn:
            tag = await conn.execute(
                "DELETE FROM provider_weight_overrides WHERE model_id = $1 AND endpoint_id = $2",
                model_id,
                endpoint_id,
            )
        return _parse_command_tag_count(tag) > 0

    async def list_route_capability_overrides_for_model(self, model_id: str) -> list[Row]:
        """Return admin capability overrides for one model's routes, ordered by route_id."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT model_id, route_id, capabilities, updated_at, updated_by "
                "FROM provider_route_capabilities WHERE model_id = $1 ORDER BY route_id",
                model_id,
            )
        return [dict(r) for r in rows]

    async def list_all_route_capability_overrides(self) -> list[Row]:
        """Return all admin capability override rows ordered by model_id and route_id."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT model_id, route_id, capabilities, updated_at, updated_by "
                "FROM provider_route_capabilities ORDER BY model_id, route_id"
            )
        return [dict(r) for r in rows]

    async def upsert_route_capability_override(
        self,
        model_id: str,
        route_id: str,
        capabilities: dict[str, Any],
        updated_by: str | None,
    ) -> None:
        """Upsert the capabilities an admin set for one route."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO provider_route_capabilities "
                "(model_id, route_id, capabilities, updated_at, updated_by) "
                "VALUES ($1, $2, $3::jsonb, NOW(), $4) "
                "ON CONFLICT (model_id, route_id) DO UPDATE SET "
                "capabilities = EXCLUDED.capabilities, "
                "updated_at = NOW(), "
                "updated_by = EXCLUDED.updated_by",
                model_id,
                route_id,
                json.dumps(capabilities),
                updated_by,
            )

    async def delete_route_capability_override(self, model_id: str, route_id: str) -> bool:
        """Delete one route's capability override. Returns True when removed."""
        async with self._pool.acquire() as conn:
            tag = await conn.execute(
                "DELETE FROM provider_route_capabilities WHERE model_id = $1 AND route_id = $2",
                model_id,
                route_id,
            )
        return _parse_command_tag_count(tag) > 0

    async def list_provider_route_configs_for_model(self, model_id: str) -> list[Row]:
        """Return provider route override rows for one model ordered by route_id."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT model_id, route_id, provider, openrouter_sort, base_url, api_key_id, "
                "provider_model_id, quota_limit, concurrency_limit, created_at, updated_at, "
                "updated_by, deployment_type "
                "FROM provider_route_configs WHERE model_id = $1 ORDER BY route_id",
                model_id,
            )
        return [dict(r) for r in rows]

    async def list_all_provider_route_configs(self) -> list[Row]:
        """Return all provider route override rows ordered by model_id and route_id."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT model_id, route_id, provider, openrouter_sort, base_url, api_key_id, "
                "provider_model_id, quota_limit, concurrency_limit, created_at, updated_at, "
                "updated_by, deployment_type "
                "FROM provider_route_configs ORDER BY model_id, route_id"
            )
        return [dict(r) for r in rows]

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
        concurrency_limit: int | None,
        updated_by: str | None,
        deployment_type: str | None = None,
    ) -> None:
        """Upsert a runtime provider route override row."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO provider_route_configs "
                "(model_id, route_id, provider, openrouter_sort, base_url, api_key_id, "
                "provider_model_id, quota_limit, concurrency_limit, created_at, updated_at, "
                "updated_by, deployment_type) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, NOW(), NOW(), $10, $11) "
                "ON CONFLICT (model_id, route_id) DO UPDATE SET "
                "provider = EXCLUDED.provider, "
                "openrouter_sort = EXCLUDED.openrouter_sort, "
                "base_url = EXCLUDED.base_url, "
                "api_key_id = EXCLUDED.api_key_id, "
                "provider_model_id = EXCLUDED.provider_model_id, "
                "quota_limit = EXCLUDED.quota_limit, "
                "concurrency_limit = EXCLUDED.concurrency_limit, "
                "updated_at = NOW(), "
                "updated_by = EXCLUDED.updated_by, "
                "deployment_type = EXCLUDED.deployment_type",
                model_id,
                route_id,
                provider,
                openrouter_sort,
                base_url,
                api_key_id,
                provider_model_id,
                quota_limit,
                concurrency_limit,
                updated_by,
                deployment_type,
            )

    async def delete_provider_route_config(self, model_id: str, route_id: str) -> bool:
        """Delete a runtime provider route override row. Returns True when removed."""
        async with self._pool.acquire() as conn:
            tag = await conn.execute(
                "DELETE FROM provider_route_configs WHERE model_id = $1 AND route_id = $2",
                model_id,
                route_id,
            )
        return _parse_command_tag_count(tag) > 0

    async def list_provider_route_candidates_for_model(self, model_id: str) -> list[Row]:
        """Return runtime provider route candidate rows for one model."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT model_id, route_id, route_type, provider, openrouter_sort, base_url, api_key_id, "
                "provider_model_id, quota_limit, concurrency_limit, weight, pricing, capabilities, "
                "created_at, updated_at, updated_by, deployment_type, endpoint_id "
                "FROM provider_route_candidates WHERE model_id = $1 ORDER BY route_id",
                model_id,
            )
        return [dict(r) for r in rows]

    async def list_all_provider_route_candidates(self) -> list[Row]:
        """Return all runtime provider route candidate rows ordered by model and route."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT model_id, route_id, route_type, provider, openrouter_sort, base_url, api_key_id, "
                "provider_model_id, quota_limit, concurrency_limit, weight, pricing, capabilities, "
                "created_at, updated_at, updated_by, deployment_type, endpoint_id "
                "FROM provider_route_candidates ORDER BY model_id, route_id"
            )
        return [dict(r) for r in rows]

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
        capabilities: dict[str, Any] | None,
        updated_by: str | None,
        deployment_type: str = "auto",
        endpoint_id: str | None = None,
    ) -> None:
        """Upsert a DB-backed runtime provider route candidate row."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO provider_route_candidates "
                "(model_id, route_id, route_type, provider, openrouter_sort, base_url, api_key_id, "
                "provider_model_id, quota_limit, concurrency_limit, weight, pricing, capabilities, "
                "created_at, updated_at, updated_by, deployment_type, endpoint_id) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12::jsonb, $13::jsonb, "
                "NOW(), NOW(), $14, $15, $16) "
                "ON CONFLICT (model_id, route_id) DO UPDATE SET "
                "route_type = EXCLUDED.route_type, "
                "provider = EXCLUDED.provider, "
                "openrouter_sort = EXCLUDED.openrouter_sort, "
                "base_url = EXCLUDED.base_url, "
                "api_key_id = EXCLUDED.api_key_id, "
                "provider_model_id = EXCLUDED.provider_model_id, "
                "quota_limit = EXCLUDED.quota_limit, "
                "concurrency_limit = EXCLUDED.concurrency_limit, "
                "weight = EXCLUDED.weight, "
                "pricing = EXCLUDED.pricing, "
                # A write without a declaration (a route that restored without
                # one, or a concurrency edit on it) keeps whatever is stored, so
                # a declaration is never replaced by NULL and then by defaults.
                "capabilities = COALESCE(EXCLUDED.capabilities, "
                "provider_route_candidates.capabilities), "
                "updated_at = NOW(), "
                "updated_by = EXCLUDED.updated_by, "
                "deployment_type = EXCLUDED.deployment_type, "
                "endpoint_id = EXCLUDED.endpoint_id",
                model_id,
                route_id,
                route_type,
                provider,
                openrouter_sort,
                base_url,
                api_key_id,
                provider_model_id,
                quota_limit,
                concurrency_limit,
                weight,
                json.dumps(pricing) if pricing is not None else None,
                json.dumps(capabilities) if capabilities is not None else None,
                updated_by,
                deployment_type,
                endpoint_id,
            )

    async def update_provider_route_candidate(
        self,
        model_id: str,
        route_id: str,
        *,
        expected_updated_at: datetime,
        provider: str,
        openrouter_sort: str | None,
        base_url: str,
        api_key_id: str | None,
        provider_model_id: str,
        quota_limit: int | None,
        concurrency_limit: int | None,
        pricing: dict[str, str] | None,
        deployment_type: str,
        endpoint_id: str | None,
        previous_endpoint_id: str,
        updated_by: str | None,
    ) -> datetime | None:
        """Edit one runtime provider route candidate in place, moving its weight override."""
        new_endpoint_id = endpoint_id or route_id
        async with self._pool.acquire() as conn, conn.transaction():
            updated_at = await conn.fetchval(
                "UPDATE provider_route_candidates SET "
                "provider = $4, openrouter_sort = $5, base_url = $6, api_key_id = $7, "
                "provider_model_id = $8, quota_limit = $9, concurrency_limit = $10, "
                "pricing = $11::jsonb, deployment_type = $12, endpoint_id = $13, "
                "updated_at = clock_timestamp(), updated_by = $14 "
                "WHERE model_id = $1 AND route_id = $2 AND updated_at = $3 "
                "RETURNING updated_at",
                model_id,
                route_id,
                expected_updated_at,
                provider,
                openrouter_sort,
                base_url,
                api_key_id,
                provider_model_id,
                quota_limit,
                concurrency_limit,
                json.dumps(pricing) if pricing is not None else None,
                deployment_type,
                endpoint_id,
                updated_by,
            )
            if updated_at is None:
                return None
            if new_endpoint_id != previous_endpoint_id:
                # A row already at the new id belongs to no live route (the
                # caller refuses a move onto one), so it must not start
                # weighting this route.
                await conn.execute(
                    "DELETE FROM provider_weight_overrides "
                    "WHERE model_id = $1 AND endpoint_id = $2",
                    model_id,
                    new_endpoint_id,
                )
                await conn.execute(
                    "UPDATE provider_weight_overrides SET endpoint_id = $3 "
                    "WHERE model_id = $1 AND endpoint_id = $2",
                    model_id,
                    previous_endpoint_id,
                    new_endpoint_id,
                )
        return updated_at

    async def list_all_provider_route_states(self) -> list[Row]:
        """Return every stored route state ordered by model and route."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT model_id, route_id, disabled, removed, updated_at, updated_by "
                "FROM provider_route_states ORDER BY model_id, route_id"
            )
        return [dict(r) for r in rows]

    async def set_provider_route_state(
        self,
        model_id: str,
        route_id: str,
        *,
        disabled: bool,
        removed: bool,
        updated_by: str | None,
        expected_disabled: bool | None = None,
        expected_removed: bool | None = None,
    ) -> bool:
        """Upsert one route's state, or delete its row when neither flag is set.

        With an expectation, each case is one statement, so a concurrent write
        either lands first and makes this one match nothing, or lands after it.
        """
        if (expected_disabled is None) != (expected_removed is None):
            raise ValueError("expected_disabled and expected_removed go together")
        expecting = expected_disabled is not None
        # A route with neither flag set has no row.
        expected_row = bool(expected_disabled or expected_removed)
        async with self._pool.acquire() as conn:
            if expecting and not expected_row:
                if not disabled and not removed:
                    # Nothing to write; the route must still have no state.
                    current = await conn.fetchrow(
                        "SELECT disabled, removed FROM provider_route_states "
                        "WHERE model_id = $1 AND route_id = $2",
                        model_id,
                        route_id,
                    )
                    return current is None or not (current["disabled"] or current["removed"])
                # Insert, or take over a row that sets neither flag; a row any
                # other process stored in between keeps its flags.
                written = await conn.fetchval(
                    "INSERT INTO provider_route_states "
                    "(model_id, route_id, disabled, removed, updated_at, updated_by) "
                    "VALUES ($1, $2, $3, $4, NOW(), $5) "
                    "ON CONFLICT (model_id, route_id) DO UPDATE SET "
                    "disabled = EXCLUDED.disabled, removed = EXCLUDED.removed, "
                    "updated_at = NOW(), updated_by = EXCLUDED.updated_by "
                    "WHERE provider_route_states.disabled = FALSE "
                    "AND provider_route_states.removed = FALSE "
                    "RETURNING TRUE",
                    model_id,
                    route_id,
                    disabled,
                    removed,
                    updated_by,
                )
                return bool(written)
            if expected_row:
                if not disabled and not removed:
                    tag = await conn.execute(
                        "DELETE FROM provider_route_states "
                        "WHERE model_id = $1 AND route_id = $2 AND disabled = $3 AND removed = $4",
                        model_id,
                        route_id,
                        expected_disabled,
                        expected_removed,
                    )
                else:
                    tag = await conn.execute(
                        "UPDATE provider_route_states SET disabled = $5, removed = $6, "
                        "updated_at = NOW(), updated_by = $7 "
                        "WHERE model_id = $1 AND route_id = $2 AND disabled = $3 AND removed = $4",
                        model_id,
                        route_id,
                        expected_disabled,
                        expected_removed,
                        disabled,
                        removed,
                        updated_by,
                    )
                return _parse_command_tag_count(tag) > 0
            if not disabled and not removed:
                await conn.execute(
                    "DELETE FROM provider_route_states WHERE model_id = $1 AND route_id = $2",
                    model_id,
                    route_id,
                )
                return True
            await conn.execute(
                "INSERT INTO provider_route_states "
                "(model_id, route_id, disabled, removed, updated_at, updated_by) "
                "VALUES ($1, $2, $3, $4, NOW(), $5) "
                "ON CONFLICT (model_id, route_id) DO UPDATE SET "
                "disabled = EXCLUDED.disabled, removed = EXCLUDED.removed, "
                "updated_at = NOW(), updated_by = EXCLUDED.updated_by",
                model_id,
                route_id,
                disabled,
                removed,
                updated_by,
            )
            return True

    async def delete_provider_route_candidate(self, model_id: str, route_id: str) -> bool:
        """Delete a runtime provider route candidate row. Returns True when removed."""
        async with self._pool.acquire() as conn:
            tag = await conn.execute(
                "DELETE FROM provider_route_candidates WHERE model_id = $1 AND route_id = $2",
                model_id,
                route_id,
            )
        return _parse_command_tag_count(tag) > 0

    async def list_all_provider_route_batch_policies(self) -> list[Row]:
        """Return every stored batch choice ordered by model and route."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT model_id, route_id, batch_allowed, updated_at, updated_by "
                "FROM provider_route_batch_policies ORDER BY model_id, route_id"
            )
        return [dict(r) for r in rows]

    async def set_provider_route_batch_policy(
        self,
        model_id: str,
        route_id: str,
        *,
        batch_allowed: bool | None,
        updated_by: str | None,
    ) -> None:
        """Store one route's batch choice; ``None`` deletes it (the route follows its locality)."""
        async with self._pool.acquire() as conn:
            if batch_allowed is None:
                await conn.execute(
                    "DELETE FROM provider_route_batch_policies "
                    "WHERE model_id = $1 AND route_id = $2",
                    model_id,
                    route_id,
                )
                return
            await conn.execute(
                """
                INSERT INTO provider_route_batch_policies
                    (model_id, route_id, batch_allowed, updated_at, updated_by)
                VALUES ($1, $2, $3, NOW(), $4)
                ON CONFLICT (model_id, route_id) DO UPDATE SET
                    batch_allowed = EXCLUDED.batch_allowed,
                    updated_at = EXCLUDED.updated_at,
                    updated_by = EXCLUDED.updated_by
                """,
                model_id,
                route_id,
                batch_allowed,
                updated_by,
            )

    async def delete_provider_route_candidate_with_config(
        self,
        model_id: str,
        route_id: str,
    ) -> bool:
        """Atomically delete a route candidate, its override row, route state and batch choice."""
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "DELETE FROM provider_route_configs WHERE model_id = $1 AND route_id = $2",
                model_id,
                route_id,
            )
            await conn.execute(
                "DELETE FROM provider_route_states WHERE model_id = $1 AND route_id = $2",
                model_id,
                route_id,
            )
            await conn.execute(
                "DELETE FROM provider_route_batch_policies WHERE model_id = $1 AND route_id = $2",
                model_id,
                route_id,
            )
            tag = await conn.execute(
                "DELETE FROM provider_route_candidates WHERE model_id = $1 AND route_id = $2",
                model_id,
                route_id,
            )
        return _parse_command_tag_count(tag) > 0

    async def delete_runtime_model_state(
        self,
        model_id: str,
        setting_keys: Sequence[str],
    ) -> bool:
        """Atomically delete all persisted state owned by a runtime model."""
        async with self._pool.acquire() as conn, conn.transaction():
            candidate_tag = await conn.execute(
                "DELETE FROM provider_route_candidates WHERE model_id = $1",
                model_id,
            )
            await conn.execute(
                "DELETE FROM provider_route_configs WHERE model_id = $1",
                model_id,
            )
            await conn.execute(
                "DELETE FROM provider_route_states WHERE model_id = $1",
                model_id,
            )
            await conn.execute(
                "DELETE FROM provider_route_batch_policies WHERE model_id = $1",
                model_id,
            )
            await conn.execute(
                "DELETE FROM model_visibility_overrides WHERE model_id = $1",
                model_id,
            )
            await conn.execute(
                "DELETE FROM model_concurrency_exemptions WHERE model_id = $1",
                model_id,
            )
            await conn.execute(
                "DELETE FROM provider_weight_overrides WHERE model_id = $1",
                model_id,
            )
            await conn.execute(
                "DELETE FROM provider_route_capabilities WHERE model_id = $1",
                model_id,
            )
            await conn.execute(
                "DELETE FROM site_settings WHERE key = ANY($1::text[])",
                list(setting_keys),
            )
        return _parse_command_tag_count(candidate_tag) > 0

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
        """Persist one active RouteWise latency probe outcome."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO routewise_probe_samples "
                "(model_id, endpoint_id, ttft_ms, ok, error, cost_usd, checked_at) "
                "VALUES ($1, $2, $3, $4, $5, $6, COALESCE($7::timestamptz, NOW())) "
                "RETURNING id",
                model_id,
                endpoint_id,
                ttft_ms,
                ok,
                error,
                cost_usd,
                checked_at,
            )
        return int(row["id"]) if row is not None and row["id"] is not None else None

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
        limit = max(int(limit), 1)
        order = "checked_at DESC, id DESC" if newest_first else "id ASC"
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, model_id, endpoint_id, ttft_ms, ok, error, cost_usd, checked_at "
                "FROM routewise_probe_samples "
                "WHERE ($1::text IS NULL OR model_id = $1) "
                "AND ($2::text IS NULL OR endpoint_id = $2) "
                "AND ($3::timestamptz IS NULL OR checked_at >= $3) "
                "AND ($4::bigint IS NULL OR id > $4) "
                f"ORDER BY {order} "
                "LIMIT $5",
                model_id,
                endpoint_id,
                since,
                after_id,
                limit,
            )
        return [dict(r) for r in rows]

    async def try_acquire_routewise_probe_lease(
        self,
        *,
        lease_key: str,
        holder_id: str,
        ttl_sec: float,
    ) -> bool:
        """Acquire or renew a scoped RouteWise probe lease."""
        ttl = max(float(ttl_sec), 1.0)
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO routewise_probe_leases
                    (lease_key, holder_id, expires_at, updated_at)
                VALUES (
                    $1,
                    $2,
                    NOW() + ($3::double precision * INTERVAL '1 second'),
                    NOW()
                )
                ON CONFLICT (lease_key) DO UPDATE
                SET holder_id = EXCLUDED.holder_id,
                    expires_at = EXCLUDED.expires_at,
                    updated_at = NOW()
                WHERE routewise_probe_leases.expires_at <= NOW()
                   OR routewise_probe_leases.holder_id = EXCLUDED.holder_id
                RETURNING holder_id
                """,
                lease_key,
                holder_id,
                ttl,
            )
        return bool(row is not None and row["holder_id"] == holder_id)

    # -- cost counters -------------------------------------------------------

    async def increment_user_cost(
        self,
        user_id: str,
        cost_usd: float,
        *,
        day: str | None = None,
        api_key_id: int | None = None,
        requests: int = 1,
        org_id: str | None = None,
        project_id: str | None = None,
    ) -> None:
        """Atomically increment the daily cost counters via upsert.

        With ``api_key_id`` the key's own counter (``api_key_daily_cost``) is
        bumped in the same statement: the key's budgets read it, so a write
        that left it behind would let the key spend past them.
        """
        from datetime import datetime

        if day is None:
            day = datetime.now(UTC).strftime("%Y-%m-%d")
        await self.increment_user_costs(
            [CostIncrement(user_id, day, api_key_id, cost_usd, requests, org_id, project_id)]
        )

    atomic_cost_increments = True

    async def increment_user_costs(self, increments: Sequence[CostIncrement]) -> None:
        """Add a batch of increments to the counters in one transaction, exactly once.

        A failure before the COMMIT was sent applied nothing and is raised as
        it is, so the caller may write the batch again. When the COMMIT was
        sent and its answer lost (the connection dropped), the database is
        asked how the transaction ended (``txid_status``): committed returns
        normally, aborted raises the original error, and an answer that cannot
        be had raises :class:`CostWriteOutcomeUnknown` -- retrying then could
        count the batch twice.

        With a fence secret, an increment for a hard-deleted account is left
        out. A stream still running when its account was erased records its
        cost when it ends, after the hard delete purged these counters: the
        write takes each account's shared fence lock and checks the tombstones,
        as an ``api_logs`` write does (:mod:`serving.storage.erasure_fence`), so
        it either commits before the hard delete establishes the tombstone --
        and the hard delete purges the counters after that -- or skips the
        account. Past :data:`_COST_FENCE_LOCKS_MAX` accounts, only the
        statement's own check is made.
        """
        if not increments:
            return
        args = (
            [increment.user_id for increment in increments],
            [increment.day for increment in increments],
            [increment.api_key_id for increment in increments],
            [increment.cost_usd for increment in increments],
            [increment.requests for increment in increments],
            [increment.org_id for increment in increments],
            [increment.project_id for increment in increments],
        )
        fence_keys = (
            [
                fence_account_digest(increment.user_id, self._fence_secret)
                for increment in increments
            ]
            if self._fence_secret
            else None
        )
        xid: int | None = None
        try:
            async with self._pool.acquire() as conn, conn.transaction():
                if fence_keys is None:
                    xid = await conn.fetchval(COST_INCREMENTS_SQL, *args)
                else:
                    accounts = sorted(set(fence_keys))
                    if len(accounts) <= _COST_FENCE_LOCKS_MAX:
                        await lock_fence_keys_shared(conn, accounts)
                    xid = await conn.fetchval(COST_INCREMENTS_FENCED_SQL, *args, fence_keys)
            return
        except Exception as exc:
            if xid is None:
                raise
            failure = exc
        status = await self._transaction_status(xid)
        if status == "committed":
            return
        if status == "aborted":
            raise failure
        raise CostWriteOutcomeUnknown(xid) from failure

    async def _transaction_status(self, xid: int) -> str | None:
        """How transaction *xid* ended: ``committed``, ``aborted``, or None when untold."""
        for delay in _COMMIT_STATUS_RETRY_DELAYS:
            if delay:
                await asyncio.sleep(delay)
            try:
                async with self._pool.acquire() as conn:
                    status = await conn.fetchval("SELECT txid_status($1)", xid)
            except Exception:
                continue
            if status in ("committed", "aborted"):
                return status
        return None

    async def get_user_cost_today(self, user_id: str) -> float:
        """Return today's cost from the counter table."""
        async with self._request_connection() as conn:
            row = await conn.fetchrow(
                "SELECT cost_usd FROM user_daily_cost "
                "WHERE user_id = $1 AND day = to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM-DD')",
                user_id,
            )
        return float(row["cost_usd"]) if row else 0.0

    async def query_users_at_daily_quota(
        self,
        *,
        limit: int = 500,
    ) -> list[tuple[str, str, float, float]]:
        """Return users whose today's spend has consumed their own key's quota.

        Asks the question the gate asks, with the gate's own numbers: would
        this account's next request be refused? See
        :meth:`serving.storage.base.OperationalStore.query_users_at_daily_quota`
        for why the per-role predicate this replaced could never fire.

        Three details are load-bearing:

        **The join is the canonical one.** Same shape as
        :meth:`get_quota_context_for_user`, which is what the enforcer resolves
        a user's limit through — ``k.user_id = u.id``, key ``active`` and
        unexpired, user ``active``. A different join here would alert on a
        limit no request is ever measured against. An account may hold several
        such keys, each carrying the account's quota, so the join is a LATERAL
        taking one of them: a user is never fanned out into several rows.

        **The predicate is the enforcer's** — ``spend +
        ESTIMATED_REQUEST_COST_USD > quota`` — but read carefully *whose*
        constants those are, because there are two doors and only one of them
        shares them:

        * The **grant door** (:mod:`serving.grant_auth`, cloud-agent tokens)
          calls :func:`serving.quota.first_refusal` and :func:`serving.quota.resolve_quota`,
          so it and this query move together by construction: retune
          ``serving.quota`` and both follow.
        * The **API-key door** (``verify_api_key`` in
          :mod:`serving.servers.auth`) is the dominant path, and it does *not*
          import :mod:`serving.quota` for the gate. It hardcodes its own
          ``estimated_cost = 0.01`` and its own ``1000.0`` NULL fallback inline.

        The two sets of numbers are equal today, so today the alert and the 429
        do flip on the same request. That is a coincidence being maintained by
        hand, not a guarantee: tune either side alone and this query and the
        API-key gate silently disagree about who is out of quota. Change one,
        change the other.

        Note also that a user can appear here *just under* their cap (19.991
        against 20.00): that is not an off-by-one, it is the optimistic
        pre-charge, and the gate has already started refusing them. Exactly
        19.99 does not appear: 19.99 + 0.01 equals the cap, and both the gate
        and this query refuse only when the sum exceeds it. The
        comparison is done in float8 rather than ``numeric`` for a related
        reason — both doors compare Python floats, and matching that arithmetic
        exactly is worth more here than numeric's extra precision.

        **The scan drives off today's counter rows.** ``user_daily_cost`` is
        keyed ``(user_id, day)`` with an index on ``day``, so today's slice is
        a few hundred rows; each one probes the unique active-key index and the
        users primary key. Driving from ``users`` instead (as the per-role
        query did) would scan every account that ever existed. It also means a
        user with no spend today cannot appear — which is what "consumed their
        quota" should mean, and incidentally keeps a hypothetical zero-dollar
        key from alerting every day on no traffic at all.

        Args:
            limit: Most rows to return, largest overage first.

        Returns:
            ``(user_id, role, spend_usd, quota_usd)``, largest overage first.
        """
        # Two notes on the query below.
        #
        # ``user_daily_cost.day`` is TEXT (YYYY-MM-DD); compare on the same
        # representation rather than casting the column, which would cost the
        # index scan.
        #
        # It orders by overage, not by raw spend. With per-key caps the biggest
        # spender is usually just the user with the biggest allowance: a $200
        # key one dollar past its cap would outrank a $20 free account five
        # dollars past its own, and on a truncated list the free account — the
        # one whose behaviour actually changed — would be the row that fell
        # off. Overage is what a truncated list must keep.
        sql = """
            SELECT u.id AS user_id,
                   u.role AS role,
                   udc.cost_usd::float8 AS spend_usd,
                   COALESCE(k.quota_daily_cost_usd, $2::float8)::float8 AS quota_usd
            FROM user_daily_cost udc
            JOIN users u
              ON u.id = udc.user_id
             AND u.status = 'active'
            -- One row per account: its keys all carry the account's quota,
            -- and an account may hold several.
            JOIN LATERAL (
                SELECT k.quota_daily_cost_usd
                FROM api_keys k
                WHERE k.user_id = u.id
                  AND k.status = 'active'
                  AND (k.expires_at IS NULL OR k.expires_at > NOW())
                ORDER BY k.created_at, k.id
                LIMIT 1
            ) k ON TRUE
            WHERE udc.day = to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM-DD')
              AND udc.cost_usd + $3::float8 > COALESCE(k.quota_daily_cost_usd, $2::float8)
            ORDER BY (udc.cost_usd - COALESCE(k.quota_daily_cost_usd, $2::float8)) DESC
            LIMIT $1
        """
        # Fetch one more row than asked for, so truncation is a fact rather
        # than an inference from "len == limit".
        capped = max(1, limit)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                sql,
                capped + 1,
                quota.DEFAULT_DAILY_QUOTA_USD,
                quota.ESTIMATED_REQUEST_COST_USD,
            )
        if len(rows) > capped:
            logger.warning(
                "query_users_at_daily_quota truncated at %d rows; more users are at "
                "their daily quota than this sweep will report. Raise the limit or "
                "check for a cap misconfiguration.",
                capped,
            )
            rows = rows[:capped]
        return [
            (r["user_id"], r["role"], float(r["spend_usd"]), float(r["quota_usd"])) for r in rows
        ]

    async def get_user_cost_period(
        self,
        user_id: str,
        period: Literal["today", "month"],
    ) -> float:
        """Return cost for a period from the counter table."""
        if period == "today":
            return await self.get_user_cost_today(user_id)
        # month: sum all days in current UTC month
        async with self._request_connection() as conn:
            row = await conn.fetchrow(
                "SELECT COALESCE(SUM(cost_usd), 0) as total FROM user_daily_cost "
                "WHERE user_id = $1 "
                "AND day >= to_char(date_trunc('month', NOW() AT TIME ZONE 'UTC'), 'YYYY-MM-DD')",
                user_id,
            )
        return float(row["total"]) if row else 0.0

    async def get_user_cost_history(
        self,
        user_id: str,
        days: int = 7,
    ) -> list[Row]:
        """Return up to ``days`` of daily cost rows for ``user_id``.

        Output: list of {"day": str (ISO date YYYY-MM-DD), "cost_usd": Decimal,
        "requests": int}, ordered by day ascending. Days with zero activity
        are NOT included — caller fills gaps if needed.
        """
        if days <= 0:
            return []
        # ``day`` is stored as TEXT (YYYY-MM-DD) — compute cutoff in Python.
        from datetime import datetime, timedelta

        cutoff = (datetime.now(UTC).date() - timedelta(days=days - 1)).isoformat()
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT day, cost_usd, requests "
                "FROM user_daily_cost "
                "WHERE user_id = $1 "
                "  AND day >= $2 "
                "ORDER BY day ASC",
                user_id,
                cutoff,
            )
        return [
            {
                "day": r["day"].isoformat() if hasattr(r["day"], "isoformat") else r["day"],
                "cost_usd": r["cost_usd"],
                "requests": r["requests"],
            }
            for r in rows
        ]

    async def get_bulk_user_cost_history(
        self,
        user_ids: list[str],
        days: int = 7,
    ) -> dict[str, list[Row]]:
        """Bulk variant of get_user_cost_history — one query, grouped by user."""
        if not user_ids or days <= 0:
            return {}
        from datetime import datetime, timedelta

        cutoff = (datetime.now(UTC).date() - timedelta(days=days - 1)).isoformat()
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT user_id, day, cost_usd, requests "
                "FROM user_daily_cost "
                "WHERE user_id = ANY($1::text[]) "
                "  AND day >= $2 "
                "ORDER BY user_id, day ASC",
                user_ids,
                cutoff,
            )
        out: dict[str, list[Row]] = {uid: [] for uid in user_ids}
        for r in rows:
            out[r["user_id"]].append(
                {
                    "day": r["day"].isoformat() if hasattr(r["day"], "isoformat") else r["day"],
                    "cost_usd": r["cost_usd"],
                    "requests": r["requests"],
                }
            )
        return out

    async def get_users_summary(
        self,
        *,
        top_n: int = 5,
        anomaly_multiplier: float = 5.0,
        anomaly_min_today: Decimal | None = None,
        anomaly_min_history_days: int = 3,
        near_quota_pct: float = 0.80,
    ) -> Row:
        """Aggregate stats for the 4 dashboard summary cards.

        Returns a dict with keys: pending, top_spenders_today, anomalies,
        near_quota. Each value is {"count": int, "top": [SummaryUserItem-like]}.

        Anomaly rule: status='active' AND days_with_history>=N
            AND today_cost >= floor AND today_cost >= multiplier*avg_prior_7d.

        Near quota: any active user whose today_cost >= near_quota_pct
        of their key's quota_daily_cost_usd. Users without quota set are
        excluded.
        """
        from datetime import datetime, timedelta
        from decimal import Decimal as _Decimal

        if anomaly_min_today is None:
            anomaly_min_today = _Decimal("1.00")

        today_str = datetime.now(UTC).date().isoformat()
        prior_7d_start = (datetime.now(UTC).date() - timedelta(days=7)).isoformat()
        prior_7d_end = (datetime.now(UTC).date() - timedelta(days=1)).isoformat()
        identity = "id, email, login_name, phone, user_name, role"
        joined_identity = "u.id, u.email, u.login_name, u.phone, u.user_name, u.role"

        async with self._pool.acquire() as conn:
            # 1. Pending count + top
            pending_rows = await conn.fetch(
                f"SELECT {identity}, created_at "
                "FROM users WHERE status = 'pending_approval' "
                "ORDER BY created_at DESC LIMIT $1",
                top_n,
            )
            pending_count_row = await conn.fetchrow(
                "SELECT COUNT(*) AS c FROM users WHERE status = 'pending_approval'"
            )

            # 2. Today / 7d-avg per user (active users only).
            #    ``user_daily_cost.day`` is TEXT (YYYY-MM-DD) in this schema.
            usage_rows = await conn.fetch(
                f"""
                WITH today_costs AS (
                    SELECT user_id, COALESCE(SUM(cost_usd), 0) AS today_cost
                    FROM user_daily_cost
                    WHERE day = $1
                    GROUP BY user_id
                ),
                prior_7d AS (
                    SELECT user_id,
                           COALESCE(SUM(cost_usd), 0) AS total,
                           COUNT(DISTINCT day) AS days_with_history
                    FROM user_daily_cost
                    WHERE day BETWEEN $2 AND $3
                    GROUP BY user_id
                )
                SELECT {joined_identity},
                       COALESCE(t.today_cost, 0) AS today_cost,
                       COALESCE(p.total, 0) AS prior_7d_total,
                       COALESCE(p.days_with_history, 0) AS days_with_history,
                       k.quota_daily_cost_usd
                FROM users u
                LEFT JOIN today_costs t ON t.user_id = u.id
                LEFT JOIN prior_7d p ON p.user_id = u.id
                -- One row per user, however many active keys it holds; each
                -- carries the account's quota.
                LEFT JOIN LATERAL (
                    SELECT quota_daily_cost_usd FROM api_keys
                    WHERE account_id = u.id AND status = 'active'
                    ORDER BY created_at, id LIMIT 1
                ) k ON TRUE
                WHERE u.status = 'active'
                """,
                today_str,
                prior_7d_start,
                prior_7d_end,
            )

            top_spenders: list[Row] = []
            anomalies: list[Row] = []
            near_quota: list[Row] = []

            for r in usage_rows:
                today = _Decimal(str(r["today_cost"] or 0))
                prior = _Decimal(str(r["prior_7d_total"] or 0))
                days = int(r["days_with_history"] or 0)
                avg_7d = (prior / days) if days > 0 else _Decimal("0")
                quota = r["quota_daily_cost_usd"]

                base_item = {
                    "id": r["id"],
                    "email": r["email"],
                    "login_name": r.get("login_name"),
                    "phone": r.get("phone"),
                    "user_name": r["user_name"],
                    "role": r["role"] or "free",
                    "today_cost_usd": today,
                    "avg_prior_7d_usd": avg_7d,
                    "quota_daily_usd": float(quota) if quota else None,
                    "multiplier": None,
                }

                # Top spenders today
                if today > 0:
                    top_spenders.append(dict(base_item))

                # Anomaly
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

                # Near / over quota
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
                "count": pending_count_row["c"] if pending_count_row else 0,
                "top": [
                    {
                        "id": r["id"],
                        "email": r["email"],
                        "login_name": r.get("login_name"),
                        "phone": r.get("phone"),
                        "user_name": r["user_name"],
                        "role": r["role"] or "free",
                        "today_cost_usd": _Decimal("0"),
                        "avg_prior_7d_usd": _Decimal("0"),
                        "quota_daily_usd": None,
                        "multiplier": None,
                    }
                    for r in pending_rows
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

    async def get_batch_usage(
        self,
        user_ids: list[str],
        period: Literal["today", "month"],
    ) -> dict[str, float]:
        """Return {user_id: cost_usd} for a batch of users."""
        if not user_ids:
            return {}
        if period == "today":
            date_filter = "day = to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM-DD')"
        else:
            date_filter = (
                "day >= to_char(date_trunc('month', NOW() AT TIME ZONE 'UTC'), 'YYYY-MM-DD')"
            )
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT user_id, COALESCE(SUM(cost_usd), 0) as cost "
                f"FROM user_daily_cost "
                f"WHERE {date_filter} AND user_id = ANY($1::text[]) "
                f"GROUP BY user_id",
                user_ids,
            )
        return {r["user_id"]: float(r["cost"]) for r in rows}

    # -- role quota ----------------------------------------------------------

    async def count_active_keys_for_role(self, role: str) -> tuple[int, int]:
        """Return ``(key_count, user_count)`` of unrevoked api_keys whose owner has this role.

        Counts exactly the rows :meth:`apply_role_quota` would write.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT COUNT(*)::int AS keys,
                       COUNT(DISTINCT k.user_id)::int AS users
                FROM api_keys k
                JOIN users u ON u.id = k.user_id
                WHERE k.status <> 'revoked' AND u.role = $1
                """,
                role,
            )
        if row is None:
            return 0, 0
        return int(row["keys"]), int(row["users"])

    async def apply_role_quota(self, role: str, quota: Decimal) -> int:
        """Bulk-update ``quota_daily_cost_usd`` on every unrevoked key whose owner has *role*.

        Unrevoked rather than only active: the quota is the account's and is
        carried by each of its live keys, so a suspended key left behind would
        come back with an older quota than its siblings when it is resumed.
        Under the all-accounts lock, so a key being created or rotated for
        one of these accounts is written before this reads, never after.

        Returns the number of rows updated.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            await _lock_all_account_keys(conn)
            tag = await conn.execute(
                """
                    UPDATE api_keys
                    SET quota_daily_cost_usd = $1
                    WHERE status <> 'revoked'
                      AND user_id IN (SELECT id FROM users WHERE role = $2)
                    """,
                quota,
                role,
            )
            await conn.execute(
                "UPDATE organizations o SET budget_daily_usd = $1 "
                "FROM users u WHERE u.role = $2 AND o.id = 'org_p_' || u.id",
                quota,
                role,
            )
        return _parse_command_tag_count(tag)

    # -- provider api keys ---------------------------------------------------

    async def add_provider_key(
        self,
        *,
        provider: str,
        api_key: str,
        label: str | None,
        created_by: str | None,
        key_id: str | None = None,
        min_role: str = "free",
    ) -> str:
        """Insert a new provider API key row. Returns the row uuid."""
        import uuid

        if key_id is None:
            key_id = str(uuid.uuid4())
        prefix = _mask_provider_key(api_key)
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO provider_api_keys "
                "(id, provider, api_key, key_prefix, label, created_by, min_role) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7)",
                key_id,
                provider,
                api_key,
                prefix,
                label,
                created_by,
                _validated_min_role(min_role),
            )
        return key_id

    async def list_provider_definitions(self) -> list[ProviderDefinitionRow]:
        """Return provider definitions, overrides, and disabled markers."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT provider, display_name, adapter_kind, default_base_url, "
                "status, created_at, updated_at "
                "FROM provider_definitions "
                "ORDER BY display_name ASC, provider ASC"
            )
        return [
            ProviderDefinitionRow(
                provider=r["provider"],
                display_name=r["display_name"],
                adapter_kind=r["adapter_kind"],
                default_base_url=r["default_base_url"],
                status=r["status"],
                created_at=r["created_at"],
                updated_at=r["updated_at"],
            )
            for r in rows
        ]

    async def get_provider_definition(self, provider: str) -> ProviderDefinitionRow | None:
        """Return one provider definition or override."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT provider, display_name, adapter_kind, default_base_url, "
                "status, created_at, updated_at "
                "FROM provider_definitions "
                "WHERE provider = $1",
                provider,
            )
        if row is None:
            return None
        return ProviderDefinitionRow(
            provider=row["provider"],
            display_name=row["display_name"],
            adapter_kind=row["adapter_kind"],
            default_base_url=row["default_base_url"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def upsert_provider_definition(
        self,
        *,
        provider: str,
        display_name: str,
        adapter_kind: str,
        default_base_url: str,
        created_by: str | None,
        status: str = "active",
    ) -> ProviderDefinitionRow:
        """Create or update a provider definition or override."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO provider_definitions "
                "(provider, display_name, adapter_kind, default_base_url, status, "
                "created_by, updated_by) "
                "VALUES ($1, $2, $3, $4, $6, $5, $5) "
                "ON CONFLICT (provider) DO UPDATE SET "
                "display_name = EXCLUDED.display_name, "
                "adapter_kind = EXCLUDED.adapter_kind, "
                "default_base_url = EXCLUDED.default_base_url, "
                "status = $6, "
                "updated_by = EXCLUDED.updated_by, "
                "updated_at = NOW() "
                "RETURNING provider, display_name, adapter_kind, default_base_url, "
                "status, created_at, updated_at",
                provider,
                display_name,
                adapter_kind,
                default_base_url,
                created_by,
                status,
            )
        return ProviderDefinitionRow(
            provider=row["provider"],
            display_name=row["display_name"],
            adapter_kind=row["adapter_kind"],
            default_base_url=row["default_base_url"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def delete_provider_definition(self, provider: str) -> bool:
        """Delete a custom provider definition."""
        async with self._pool.acquire() as conn:
            tag = await conn.execute(
                "DELETE FROM provider_definitions WHERE provider = $1",
                provider,
            )
        return _parse_command_tag_count(tag) > 0

    async def delete_provider_keys_for_provider(self, provider: str) -> int:
        """Delete DB keys, env-key tombstones and env tier reservations.

        Used when a custom provider is removed. The reservation rows are dropped
        with the rest so re-adding the provider later starts clean rather than
        silently re-reserving keys nobody remembers reserving.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            key_tag = await conn.execute(
                "DELETE FROM provider_api_keys WHERE provider = $1",
                provider,
            )
            tombstone_tag = await conn.execute(
                "DELETE FROM disabled_provider_env_keys WHERE provider = $1",
                provider,
            )
            await conn.execute(
                "DELETE FROM provider_env_key_min_roles WHERE provider = $1",
                provider,
            )
        return _parse_command_tag_count(key_tag) + _parse_command_tag_count(tombstone_tag)

    async def list_provider_keys(self, provider: str | None = None) -> list[ProviderKeyRow]:
        """Return masked provider key rows (active and disabled), newest first.

        Disabled rows are included so the admin UI can show them with an enable
        toggle; ``status`` distinguishes the two.
        """
        async with self._pool.acquire() as conn:
            if provider is None:
                rows = await conn.fetch(
                    "SELECT id, provider, key_prefix, label, status, created_at, min_role "
                    "FROM provider_api_keys "
                    "ORDER BY created_at DESC"
                )
            else:
                rows = await conn.fetch(
                    "SELECT id, provider, key_prefix, label, status, created_at, min_role "
                    "FROM provider_api_keys WHERE provider = $1 "
                    "ORDER BY created_at DESC",
                    provider,
                )
        return [
            ProviderKeyRow(
                id=r["id"],
                provider=r["provider"],
                key_prefix=r["key_prefix"],
                label=r["label"],
                status=r["status"],
                created_at=r["created_at"],
                min_role=r["min_role"] or "free",
            )
            for r in rows
        ]

    async def list_provider_keys_full(
        self,
        provider: str,
        *,
        exclude_ids: set[str] | None = None,
    ) -> list[str]:
        """Return raw active API keys for *provider* (boot-time use only)."""
        excluded = sorted(exclude_ids or set())
        async with self._pool.acquire() as conn:
            if excluded:
                rows = await conn.fetch(
                    "SELECT api_key FROM provider_api_keys "
                    "WHERE provider = $1 AND status = 'active' "
                    "AND NOT (id = ANY($2::text[])) "
                    "ORDER BY created_at ASC",
                    provider,
                    excluded,
                )
            else:
                rows = await conn.fetch(
                    "SELECT api_key FROM provider_api_keys "
                    "WHERE provider = $1 AND status = 'active' "
                    "ORDER BY created_at ASC",
                    provider,
                )
        return [r["api_key"] for r in rows]

    async def list_provider_key_values(self, provider: str) -> dict[str, str]:
        """Return ``{row_id: raw_key}`` for every provider key row of *provider*."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, api_key FROM provider_api_keys WHERE provider = $1",
                provider,
            )
        return {r["id"]: r["api_key"] for r in rows}

    async def list_provider_key_min_roles(
        self,
        provider: str,
        *,
        exclude_ids: set[str] | None = None,
    ) -> dict[str, str]:
        """Return ``{raw_key: min_role}`` for active keys of *provider*.

        Keyed by raw value because that is what a live pool entry is keyed on, and
        two rows may hold the same credential. When they disagree the most
        restrictive tier is returned: the pool can only enforce one, and a
        reservation is a constraint to honor rather than one to average away.
        """
        excluded = sorted(exclude_ids or set())
        async with self._pool.acquire() as conn:
            if excluded:
                rows = await conn.fetch(
                    "SELECT api_key, min_role FROM provider_api_keys "
                    "WHERE provider = $1 AND status = 'active' "
                    "AND NOT (id = ANY($2::text[])) "
                    "ORDER BY created_at ASC",
                    provider,
                    excluded,
                )
            else:
                rows = await conn.fetch(
                    "SELECT api_key, min_role FROM provider_api_keys "
                    "WHERE provider = $1 AND status = 'active' "
                    "ORDER BY created_at ASC",
                    provider,
                )
        strictest: dict[str, str] = {}
        for row in rows:
            raw = row["api_key"]
            role = row["min_role"] or "free"
            current = strictest.get(raw)
            if current is None or ROLE_RANK.get(role, 0) > ROLE_RANK.get(current, 0):
                strictest[raw] = role
        return strictest

    async def get_provider_key_full(self, key_id: str) -> tuple[str, str] | None:
        """Return ``(provider, raw_key)`` for *key_id*, or None if absent."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT provider, api_key FROM provider_api_keys WHERE id = $1",
                key_id,
            )
        if row is None:
            return None
        return (row["provider"], row["api_key"])

    async def get_provider_key_min_role(self, key_id: str) -> str | None:
        """Return the row's ``min_role``, or None when the row is absent."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT min_role FROM provider_api_keys WHERE id = $1",
                key_id,
            )
        if row is None:
            return None
        return row["min_role"] or "free"

    async def set_provider_key_min_role(self, key_id: str, min_role: str) -> bool:
        """Re-tier a provider key row. Returns True when a row was updated."""
        async with self._pool.acquire() as conn:
            tag = await conn.execute(
                "UPDATE provider_api_keys SET min_role = $2 WHERE id = $1",
                key_id,
                _validated_min_role(min_role),
            )
        return _parse_command_tag_count(tag) > 0

    async def set_provider_key_status(self, key_id: str, status: str) -> bool:
        """Set a provider key row's status. Returns True when a row was updated."""
        if status not in ("active", "disabled"):
            raise ValueError(f"invalid provider key status: {status!r}")
        async with self._pool.acquire() as conn:
            tag = await conn.execute(
                "UPDATE provider_api_keys SET status = $2 WHERE id = $1",
                key_id,
                status,
            )
        return _parse_command_tag_count(tag) > 0

    async def delete_provider_key(self, key_id: str) -> bool:
        """Hard-delete the provider key row. Returns True when a row was removed."""
        async with self._pool.acquire() as conn:
            tag = await conn.execute(
                "DELETE FROM provider_api_keys WHERE id = $1",
                key_id,
            )
        return _parse_command_tag_count(tag) > 0

    async def disable_provider_env_key(
        self,
        *,
        provider: str,
        key_hash: str,
        key_prefix: str,
        disabled_by: str | None,
    ) -> None:
        """Persist a tombstone for an env-sourced provider key."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO disabled_provider_env_keys "
                "(provider, key_hash, key_prefix, disabled_by) "
                "VALUES ($1, $2, $3, $4) "
                "ON CONFLICT (provider, key_hash) DO UPDATE SET "
                "key_prefix = EXCLUDED.key_prefix, "
                "disabled_by = EXCLUDED.disabled_by, "
                "disabled_at = NOW()",
                provider,
                key_hash,
                key_prefix,
                disabled_by,
            )

    async def list_disabled_provider_env_key_hashes(self, provider: str) -> set[str]:
        """Return disabled env-sourced provider key hashes for *provider*."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT key_hash FROM disabled_provider_env_keys WHERE provider = $1",
                provider,
            )
        return {r["key_hash"] for r in rows}

    async def list_disabled_provider_env_keys(self, provider: str) -> list[tuple[str, str]]:
        """Return ``(key_hash, key_prefix)`` for disabled env keys of *provider*."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT key_hash, key_prefix FROM disabled_provider_env_keys "
                "WHERE provider = $1 ORDER BY disabled_at DESC",
                provider,
            )
        return [(r["key_hash"], r["key_prefix"]) for r in rows]

    async def enable_provider_env_key(self, provider: str, key_hash: str) -> bool:
        """Delete an env-key tombstone. Returns True when a row was removed."""
        async with self._pool.acquire() as conn:
            tag = await conn.execute(
                "DELETE FROM disabled_provider_env_keys WHERE provider = $1 AND key_hash = $2",
                provider,
                key_hash,
            )
        return _parse_command_tag_count(tag) > 0

    async def set_provider_env_key_min_role(
        self,
        *,
        provider: str,
        key_hash: str,
        key_prefix: str,
        min_role: str,
        updated_by: str | None,
    ) -> None:
        """Persist (or clear) the tier reservation for an env-sourced key.

        ``min_role="free"`` deletes the row: unreserved is the absence of a
        reservation, so the table stays a list of the keys that are held back.
        """
        validated = _validated_min_role(min_role)
        async with self._pool.acquire() as conn:
            if validated == "free":
                await conn.execute(
                    "DELETE FROM provider_env_key_min_roles WHERE provider = $1 AND key_hash = $2",
                    provider,
                    key_hash,
                )
                return
            await conn.execute(
                "INSERT INTO provider_env_key_min_roles "
                "(provider, key_hash, key_prefix, min_role, updated_by) "
                "VALUES ($1, $2, $3, $4, $5) "
                "ON CONFLICT (provider, key_hash) DO UPDATE SET "
                "key_prefix = EXCLUDED.key_prefix, "
                "min_role = EXCLUDED.min_role, "
                "updated_by = EXCLUDED.updated_by, "
                "updated_at = NOW()",
                provider,
                key_hash,
                key_prefix,
                validated,
                updated_by,
            )

    async def list_provider_env_key_reservations(
        self,
        provider: str,
    ) -> list[tuple[str, str, str]]:
        """Return ``(key_hash, key_prefix, min_role)`` for reserved env keys."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT key_hash, key_prefix, min_role FROM provider_env_key_min_roles "
                "WHERE provider = $1 ORDER BY updated_at DESC",
                provider,
            )
        return [(r["key_hash"], r["key_prefix"], r["min_role"]) for r in rows]

    async def list_provider_env_key_min_roles(self, provider: str) -> dict[str, str]:
        """Return ``{key_hash: min_role}`` for reserved env keys of *provider*."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT key_hash, min_role FROM provider_env_key_min_roles WHERE provider = $1",
                provider,
            )
        return {r["key_hash"]: r["min_role"] for r in rows}

    async def list_all_disabled_provider_env_keys(self) -> list[tuple[str, str, str]]:
        """Return ``(provider, key_hash, key_prefix)`` for all disabled env keys."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT provider, key_hash, key_prefix FROM disabled_provider_env_keys "
                "ORDER BY provider ASC, key_hash ASC"
            )
        return [(r["provider"], r["key_hash"], r["key_prefix"]) for r in rows]

    async def list_all_provider_env_key_reservations(self) -> list[tuple[str, str, str, str]]:
        """Return ``(provider, key_hash, key_prefix, min_role)`` for all reserved env keys."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT provider, key_hash, key_prefix, min_role "
                "FROM provider_env_key_min_roles "
                "ORDER BY provider ASC, key_hash ASC"
            )
        return [(r["provider"], r["key_hash"], r["key_prefix"], r["min_role"]) for r in rows]


def _mask_provider_key(api_key: str) -> str:
    """Mask an upstream provider API key for display.

    Returns first 8 + "..." + last 4 when the key is long enough; otherwise
    a generic placeholder so short secrets are never leaked.
    """
    if len(api_key) >= 16:
        return f"{api_key[:8]}...{api_key[-4:]}"
    return "***configured***"


def _validated_min_role(min_role: str | None) -> str:
    """Return a valid provider-key ``min_role``, rejecting unknown roles.

    Fail-closed on writes: persisting a role the gateway cannot interpret would
    silently reserve a key for nobody (``has_role`` never passes an unknown
    requirement), taking upstream capacity offline.
    """
    if min_role is None:
        return "free"
    if min_role not in VALID_ROLES:
        raise ValueError(f"invalid provider key min_role: {min_role!r}")
    return min_role
