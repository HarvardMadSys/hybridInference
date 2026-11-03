"""The tables ``PostgresOperationalStore`` built at startup, frozen.

Copied from ``serving.storage.postgres_operational.PostgresOperationalStore._create_tables``,
with the ``agent_grants`` DDL it took from ``serving.grants``. Left out: the
``ADMIN_EMAILS`` role seeding, which reads runtime configuration and stays at
startup.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from migrations.baseline.account_columns import (
    migrate_login_events_account_schema,
    migrate_users_account_schema,
)
from migrations.baseline.audit_log import ensure_admin_audit_log_schema
from migrations.baseline.browser_sessions import ensure_browser_session_tables
from migrations.baseline.ddl import (
    SchemaLockUnavailable,
    apply_column_migrations,
    bounded_ddl,
    column_metadata,
    constraint_admitted_values,
    constraint_definition,
    drop_columns_if_present,
    drop_indexes_if_present,
    execute_ddl,
    parse_command_tag_count as _parse_command_tag_count,
)
from migrations.baseline.phone_auth import initialize_phone_auth

if TYPE_CHECKING:
    import asyncpg

logger = logging.getLogger(__name__)

#: The partial unique indexes that held an account to one active key. Retired
#: when accounts could hold several; startup drops them where they remain.
RETIRED_API_KEY_UNIQUE_INDEXES = ("idx_api_keys_user_unique", "idx_api_keys_account_active_unique")


class RequiredOperationalSchemaUnavailable(SchemaLockUnavailable):
    """A column the store cannot serve without could not be installed at startup.

    Unlike ordinary operational migrations, two are required before the store
    can be exposed. The hard-delete claim marker: resume_user and both
    hard-delete phases select it on every request. The ``admin_audit_log``
    attribution columns (:mod:`serving.storage.audit_schema`): every audit
    write names them, and an audit row without them would lose who acted. A
    lock timeout on either therefore must fail startup rather than defer the
    migration while serving against an unsafe schema. Only a database older
    than the column can hit this; the account columns are deferrable instead
    (see :mod:`serving.storage.account_schema`).
    """


#: DDL for the gateway-owned grants table.
#:
#: It stayed behind when ``agent_job_store`` left with the cloud agent, because
#: minting and verifying a capability for *this* gateway's models is this
#: gateway's job.
#:
#: The unique constraint is what makes minting idempotent — a control plane that
#: retries after a timeout gets its grant back rather than a second capability
#: nobody is tracking.
AGENT_GRANTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS agent_grants (
    grant_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    external_job_id TEXT NOT NULL,
    external_attempt_id TEXT NOT NULL,
    allowed_models JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    UNIQUE (external_job_id, external_attempt_id)
)
"""

AGENT_GRANTS_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_agent_grants_expires ON agent_grants(expires_at)",
    "CREATE INDEX IF NOT EXISTS idx_agent_grants_user ON agent_grants(user_id)",
)


async def create_tables(conn: asyncpg.Connection) -> None:
    """DDL for the operational tables."""
    import asyncpg as _asyncpg

    # --- users ---
    # ``email`` admits NULL for the one account created without an address
    # (the first-run setup administrator), which signs in with
    # ``login_name``; keep in step with the copy in ``database.py``.
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            email TEXT UNIQUE,
            login_name TEXT,
            password_hash TEXT,
            phone TEXT,
            phone_verified BOOLEAN NOT NULL DEFAULT FALSE,
            user_name TEXT,
            preferences JSONB NOT NULL DEFAULT '{}'::jsonb,
            role TEXT NOT NULL DEFAULT 'free'
                CHECK (role IN ('free', 'pro', 'internal', 'admin')),
            email_verified BOOLEAN DEFAULT FALSE,
            status TEXT DEFAULT 'active'
                CHECK (status IN ('active', 'suspended', 'deleted',
                                  'pending_approval', 'rejected')),
            hard_delete_pending BOOLEAN NOT NULL DEFAULT FALSE,
            hard_delete_claim_token TEXT,
            approval_note TEXT,
            reviewed_at TIMESTAMPTZ,
            reviewed_by TEXT,
            signup_reason TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            last_login_at TIMESTAMPTZ,
            max_concurrent_requests INT DEFAULT NULL,
            admin_note TEXT,
            suspension_message TEXT
        )
    """)
    # Tables requests use that came after the users table. Creating a new
    # table waits on no lock another session holds, so they come before
    # any migration below that can be deferred: sign-in (browser sessions)
    # and account deletion (phone codes) need them on the first boot of an
    # upgrade too.
    await ensure_browser_session_tables(conn)
    # A lock timeout below (the account columns, or the phone codes'
    # purpose CHECK) is remembered, the rest of the schema is still
    # built, and the timeout is raised at the end for bootstrap to retry
    # from the background.
    deferred: SchemaLockUnavailable | None = None
    try:
        await initialize_phone_auth(conn)
    except SchemaLockUnavailable as exc:
        deferred = exc
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_users_status ON users(status)")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_users_created_at ON users(created_at DESC)")
    # The hard-delete columns are part of the hard-delete state machine.
    # Install them before any other operational migration. If the upgrade
    # cannot acquire the lock, refuse to expose the store rather than
    # letting bootstrap defer a migration while hard-delete/resume queries
    # would fail with UndefinedColumnError.
    try:
        await apply_column_migrations(
            conn,
            "users",
            [
                (
                    "hard_delete_pending",
                    "ALTER TABLE users ADD COLUMN IF NOT EXISTS "
                    "hard_delete_pending BOOLEAN NOT NULL DEFAULT FALSE",
                ),
                (
                    "hard_delete_claim_token",
                    "ALTER TABLE users ADD COLUMN IF NOT EXISTS hard_delete_claim_token TEXT",
                ),
            ],
        )
    except SchemaLockUnavailable as exc:
        raise RequiredOperationalSchemaUnavailable(
            "hard-delete claim columns are required before the operational store "
            "can serve; refusing deferred startup while its migration is locked"
        ) from exc

    # --- admin_audit_log ---
    # Shared with DatabaseLogger (serving.storage.audit_schema). Every
    # audit write names the attribution columns, so like the hard-delete
    # claim columns they are required: a lock timeout refuses startup
    # rather than serving writes that would fail or lose their actor. Also
    # like them, installed here, before any migration whose lock timeout
    # propagates and lets bootstrap expose the store with the rest
    # deferred (the role default below, the api_keys columns). The table
    # references no other, so nothing it needs is built later.
    try:
        await ensure_admin_audit_log_schema(conn)
    except SchemaLockUnavailable as exc:
        raise RequiredOperationalSchemaUnavailable(
            "admin_audit_log attribution columns are required before the "
            "operational store can serve; refusing deferred startup while "
            "their migration is locked"
        ) from exc

    # The account columns (login names, phone numbers, accounts without an
    # email) can wait for their lock: the store queries only the ones that
    # exist (serving.storage.account_schema). The first boot after an
    # upgrade often meets the nightly pg_dump, so a lock timeout here is
    # remembered, the rest of the schema is still built, and the timeout
    # is raised at the end for bootstrap to retry from the background.
    try:
        await migrate_users_account_schema(conn)
    except SchemaLockUnavailable as exc:
        deferred = deferred or exc

    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_users_pending_approval "
        "ON users(created_at DESC) WHERE status = 'pending_approval'"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_users_last_login_at ON users(last_login_at DESC NULLS LAST)"
    )

    # Migrations — catalog-gated (serving.storage.log_schema): the settled
    # startup path issues no strong-lock DDL at all. ALTER TABLE takes its
    # ACCESS EXCLUSIVE lock BEFORE evaluating IF NOT EXISTS, and users is
    # on auth's read path — a bare no-op ALTER queued behind the nightly
    # pg_dump took production down on 2026-08-21. A lock timeout here is
    # deferred like the account columns: the rest of the schema is still
    # built, and the reads that need a column it adds tolerate its absence.
    try:
        await apply_column_migrations(
            conn,
            "users",
            [
                (
                    "approval_note",
                    "ALTER TABLE users ADD COLUMN IF NOT EXISTS approval_note TEXT",
                ),
                (
                    "reviewed_at",
                    "ALTER TABLE users ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMPTZ",
                ),
                ("reviewed_by", "ALTER TABLE users ADD COLUMN IF NOT EXISTS reviewed_by TEXT"),
                (
                    "signup_reason",
                    "ALTER TABLE users ADD COLUMN IF NOT EXISTS signup_reason TEXT",
                ),
                (
                    "preferences",
                    "ALTER TABLE users ADD COLUMN IF NOT EXISTS "
                    "preferences JSONB NOT NULL DEFAULT '{}'::jsonb",
                ),
                (
                    "max_concurrent_requests",
                    "ALTER TABLE users ADD COLUMN IF NOT EXISTS "
                    "max_concurrent_requests INT DEFAULT NULL",
                ),
                # Free-text admin-only annotation about a user (any status).
                ("admin_note", "ALTER TABLE users ADD COLUMN IF NOT EXISTS admin_note TEXT"),
                # Admin-authored message shown to the user on the login page when
                # their account is suspended (user-facing, unlike admin_note).
                (
                    "suspension_message",
                    "ALTER TABLE users ADD COLUMN IF NOT EXISTS suspension_message TEXT",
                ),
                # The user's portrait, a small image data URL. A column of its
                # own rather than a preference: preferences ride along on every
                # API-key authentication, and a portrait has no business there.
                (
                    "avatar_data_url",
                    "ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_data_url TEXT",
                ),
            ],
        )
    except SchemaLockUnavailable as exc:
        deferred = deferred or exc

    # Status constraint rebuild — only when the CHECK does not admit
    # exactly the current status set (see constraint_admitted_values for
    # why exact, not membership). The rebuild takes ACCESS EXCLUSIVE, so
    # the settled path must read the catalog and walk away.
    status_def = await constraint_definition(conn, "users", "users_status_check")
    status_settled = constraint_admitted_values(status_def) == {
        "active",
        "suspended",
        "deleted",
        "pending_approval",
        "rejected",
    }
    try:
        if not status_settled:
            async with bounded_ddl(conn), conn.transaction():
                await execute_ddl(
                    conn, "ALTER TABLE users DROP CONSTRAINT IF EXISTS users_status_check"
                )
                await execute_ddl(
                    conn,
                    """
                ALTER TABLE users ADD CONSTRAINT users_status_check
                CHECK (status IN ('active', 'suspended', 'deleted',
                                  'pending_approval', 'rejected'))
            """,
                )
    except _asyncpg.PostgresError as exc:
        invalid_rows = await conn.fetch(
            "SELECT id, email, status FROM users "
            "WHERE status NOT IN ('active','suspended','deleted',"
            "'pending_approval','rejected') "
            "ORDER BY created_at DESC LIMIT 10"
        )
        logger.error(
            "Failed to rebuild users_status_check; transaction rolled back. "
            "Sample invalid rows=%s error=%s",
            [dict(row) for row in invalid_rows],
            exc,
        )
        raise

    # Role column & migration — every strong-lock step gated on the catalog.
    await apply_column_migrations(
        conn, "users", [("role", "ALTER TABLE users ADD COLUMN IF NOT EXISTS role TEXT")]
    )
    role_meta = await column_metadata(conn, "users", "role")
    role_default = str(role_meta["default_expr"] or "") if role_meta else ""
    if "'free'" not in role_default:
        async with bounded_ddl(conn):
            await execute_ddl(conn, "ALTER TABLE users ALTER COLUMN role SET DEFAULT 'free'")
    tag = await conn.execute("UPDATE users SET role = 'free' WHERE role IS NULL")
    backfilled = _parse_command_tag_count(tag)
    if backfilled:
        logger.info("Backfilled default user role for %d existing rows.", backfilled)

    if role_meta is None or not role_meta["not_null"]:
        async with bounded_ddl(conn):
            await execute_ddl(conn, "ALTER TABLE users ALTER COLUMN role SET NOT NULL")

    # Settled requires the admitted set to match exactly: the historical
    # five-member constraint ('trial', 'free', 'pro', 'internal', 'admin')
    # contains all four current roles, and a membership test would skip
    # the trial->free row migration below while trial rows still exist.
    role_def = await constraint_definition(conn, "users", "users_role_check")
    role_settled = constraint_admitted_values(role_def) == {
        "free",
        "pro",
        "internal",
        "admin",
    }
    try:
        if not role_settled:
            async with bounded_ddl(conn), conn.transaction():
                await execute_ddl(
                    conn, "ALTER TABLE users DROP CONSTRAINT IF EXISTS users_role_check"
                )
                tag = await conn.execute(
                    "UPDATE users SET role = 'internal' "
                    "WHERE role IN ('internal_group', 'developer')"
                )
                migrated = _parse_command_tag_count(tag)
                if migrated:
                    logger.info(
                        "Migrated %d users from internal_group/developer to internal.",
                        migrated,
                    )
                trial_tag = await conn.execute(
                    "UPDATE users SET role = 'free' WHERE role = 'trial'"
                )
                migrated_trial = _parse_command_tag_count(trial_tag)
                if migrated_trial:
                    logger.info(
                        "Migrated %d users from trial to free.",
                        migrated_trial,
                    )
                await execute_ddl(
                    conn,
                    """
                ALTER TABLE users ADD CONSTRAINT users_role_check
                CHECK (role IN ('free', 'pro', 'internal', 'admin'))
            """,
                )
    except _asyncpg.PostgresError as exc:
        invalid_rows = await conn.fetch(
            "SELECT id, email, role FROM users "
            "WHERE role NOT IN ('free','pro','internal','admin') "
            "ORDER BY created_at DESC LIMIT 10"
        )
        logger.error(
            "Failed to rebuild users_role_check; transaction rolled back. "
            "Sample invalid rows=%s error=%s",
            [dict(row) for row in invalid_rows],
            exc,
        )
        raise

    # --- api_keys ---
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS api_keys (
            id BIGSERIAL PRIMARY KEY,
            key_hash TEXT NOT NULL UNIQUE,
            api_key_encrypted TEXT,
            key_prefix TEXT NOT NULL,
            user_id TEXT NOT NULL,
            user_name TEXT,
            name TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            quota_daily_cost_usd DECIMAL(10, 4) DEFAULT 1000.00,
            quota_monthly_cost_usd DECIMAL(10, 4),
            created_at TIMESTAMPTZ DEFAULT NOW(),
            expires_at TIMESTAMPTZ,
            last_used_at TIMESTAMPTZ,
            notes TEXT,
            metadata JSONB
        )
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_api_keys_user ON api_keys(user_id)")
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_api_keys_status ON api_keys(status, expires_at)"
    )
    await conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_api_keys_prefix_unique ON api_keys(key_prefix)"
    )
    # Migrations — catalog-gated; this exact cluster (bare, on the auth
    # read path) is what the 2026-08-21 production outage queued behind
    # the nightly pg_dump.
    await apply_column_migrations(
        conn,
        "api_keys",
        [
            (
                "quota_daily_cost_usd",
                "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS "
                "quota_daily_cost_usd DECIMAL(10, 4) DEFAULT 1000.00",
            ),
            (
                "quota_monthly_cost_usd",
                "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS "
                "quota_monthly_cost_usd DECIMAL(10, 4)",
            ),
            (
                "api_key_encrypted",
                "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS api_key_encrypted TEXT",
            ),
            ("account_id", "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS account_id TEXT"),
        ],
    )
    # A label the owner gives each key; NULL for keys made before an
    # account could hold more than one. Deferrable on its own: nothing on
    # the auth path reads it, the key routes read it only once the catalog
    # shows it (:meth:`_has_api_key_names`), and a lock timeout here must
    # not hold back the tables built below.
    try:
        await apply_column_migrations(
            conn,
            "api_keys",
            [("name", "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS name TEXT")],
        )
    except SchemaLockUnavailable as exc:
        deferred = deferred or exc
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_api_keys_account ON api_keys(account_id)")
    # An account may hold several active keys (capped per account by the
    # create path, under a per-user advisory lock), so the partial unique
    # indexes that allowed one are retired. Catalog-gated: a settled
    # database issues no DDL; a held table defers the drop, and until it
    # lands a second key is refused with 409 as before -- the rest of the
    # schema is still built meanwhile.
    try:
        await drop_indexes_if_present(
            conn,
            "api_keys",
            [
                (index, f"DROP INDEX IF EXISTS {index}")
                for index in RETIRED_API_KEY_UNIQUE_INDEXES
            ],
        )
    except SchemaLockUnavailable as exc:
        deferred = deferred or exc
    # Backfill account_id = user_id for legacy self-registered keys created
    # before the account_id column existed (account_id IS NULL). Makes those
    # keys visible/manageable in the dashboard, which lists by account_id.
    # Idempotent: only NULL rows whose user still exists are touched.
    tag = await conn.execute(
        "UPDATE api_keys k SET account_id = k.user_id "
        "WHERE k.account_id IS NULL "
        "AND EXISTS (SELECT 1 FROM users u WHERE u.id = k.user_id)"
    )
    backfilled = _parse_command_tag_count(tag)
    if backfilled:
        logger.info("Backfilled account_id for %d legacy api_keys rows.", backfilled)
    # Drop legacy 'tier' column — tier/role unified on users.role
    # (spec: docs/agents/specs/2026-05-02-unify-tier-role-design.md).
    await drop_columns_if_present(
        conn, "api_keys", [("tier", "ALTER TABLE api_keys DROP COLUMN IF EXISTS tier")]
    )

    # --- auth_sessions ---
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS auth_sessions (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            refresh_token_hash TEXT NOT NULL UNIQUE,
            jti TEXT,
            sid TEXT NOT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            last_used_at TIMESTAMPTZ,
            expires_at TIMESTAMPTZ NOT NULL,
            revoked BOOLEAN DEFAULT FALSE,
            user_agent TEXT,
            ip_address TEXT
        )
    """)
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_auth_sessions_user ON auth_sessions(user_id, expires_at)"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_auth_sessions_token "
        "ON auth_sessions(refresh_token_hash) WHERE NOT revoked"
    )
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_auth_sessions_jti ON auth_sessions(jti)")

    # --- login_events ---
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS login_events (
            id BIGSERIAL PRIMARY KEY,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            user_id TEXT,
            email TEXT,
            phone TEXT,
            outcome TEXT NOT NULL
                CHECK (outcome IN ('success', 'failure')),
            failure_reason TEXT,
            ip TEXT,
            user_agent TEXT
        )
        """
    )
    # Phone sign-in's part of the account migration. After a lock timeout
    # on users the holder is most likely pg_dump, which holds this table
    # too: leave it to the same retry rather than queue a second
    # ACCESS EXCLUSIVE request behind it.
    if deferred is None:
        try:
            await migrate_login_events_account_schema(conn)
        except SchemaLockUnavailable as exc:
            deferred = exc
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_login_events_user "
        "ON login_events (user_id, created_at DESC) WHERE user_id IS NOT NULL"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_login_events_email ON login_events (email, created_at DESC)"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_login_events_created ON login_events (created_at DESC)"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_login_events_failures "
        "ON login_events (created_at DESC) WHERE outcome = 'failure'"
    )

    # --- email_verification_tokens ---
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS email_verification_tokens (
            token TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            expires_at TIMESTAMPTZ NOT NULL,
            used_at TIMESTAMPTZ,
            pending_email TEXT
        )
    """)
    # Deferred on a lock timeout like the users columns; a verification
    # token for an email binding cannot be issued until it lands.
    try:
        await apply_column_migrations(
            conn,
            "email_verification_tokens",
            [
                (
                    "pending_email",
                    "ALTER TABLE email_verification_tokens "
                    "ADD COLUMN IF NOT EXISTS pending_email TEXT",
                ),
            ],
        )
    except SchemaLockUnavailable as exc:
        deferred = deferred or exc
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_email_verification_user "
        "ON email_verification_tokens(user_id)"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_email_verification_expires "
        "ON email_verification_tokens(expires_at)"
    )

    # --- password_reset_tokens ---
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS password_reset_tokens (
            token TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            expires_at TIMESTAMPTZ NOT NULL,
            used_at TIMESTAMPTZ
        )
    """)
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_password_reset_user ON password_reset_tokens(user_id)"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_password_reset_expires ON password_reset_tokens(expires_at)"
    )

    # --- agent_grants ---
    # DDL lives in serving/grants.py, the module that owns this table. It
    # stayed behind when agent_job_store left with the cloud agent:
    # minting capabilities for this gateway's models is the gateway's job.
    await conn.execute(AGENT_GRANTS_TABLE_SQL)
    for statement in AGENT_GRANTS_INDEX_SQL:
        await conn.execute(statement)

    # --- identity_auth_codes ---
    # Cross-service SSO codes. Keyed by the hash, never the code itself.
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS identity_auth_codes (
            code_hash TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            client_id TEXT NOT NULL,
            redirect_uri TEXT NOT NULL,
            code_challenge TEXT NOT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            expires_at TIMESTAMPTZ NOT NULL,
            used_at TIMESTAMPTZ
        )
    """)
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_identity_auth_codes_expires "
        "ON identity_auth_codes(expires_at)"
    )

    # --- user_daily_cost ---
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS user_daily_cost (
            user_id TEXT NOT NULL,
            day TEXT NOT NULL,
            cost_usd DECIMAL(12, 6) NOT NULL DEFAULT 0,
            requests INTEGER NOT NULL DEFAULT 0,
            last_request_at TIMESTAMPTZ,
            PRIMARY KEY (user_id, day)
        )
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_user_daily_cost_day ON user_daily_cost(day)")

    # --- signup_allowed_domains ---
    # Admin-editable allowlist of email domains whose signups auto-approve.
    # Empty table = all signups auto-approve; non-empty = only listed
    # domains (exact or *.suffix) auto-approve, others go to pending_approval.
    # ``created_by`` uses ON DELETE SET NULL so hard-deleting a user who
    # once added an entry doesn't fail with a FK violation; the field is
    # informational/audit only.
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS signup_allowed_domains (
            domain TEXT NOT NULL,
            is_wildcard BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            created_by TEXT REFERENCES users(id) ON DELETE SET NULL,
            PRIMARY KEY (domain, is_wildcard)
        )
    """)

    # Migration: existing databases created the FK without
    # ON DELETE SET NULL, which blocks hard_delete_user. Rebuild the
    # constraint in place. Auto-generated name is
    # ``signup_allowed_domains_created_by_fkey``.
    fk_def = await constraint_definition(
        conn, "signup_allowed_domains", "signup_allowed_domains_created_by_fkey"
    )
    try:
        if fk_def is None or "ON DELETE SET NULL" not in fk_def:
            async with bounded_ddl(conn), conn.transaction():
                await execute_ddl(
                    conn,
                    "ALTER TABLE signup_allowed_domains "
                    "DROP CONSTRAINT IF EXISTS signup_allowed_domains_created_by_fkey",
                )
                await execute_ddl(
                    conn,
                    "ALTER TABLE signup_allowed_domains "
                    "ADD CONSTRAINT signup_allowed_domains_created_by_fkey "
                    "FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL",
                )
    except _asyncpg.PostgresError as exc:
        logger.error(
            "Failed to rebuild signup_allowed_domains_created_by_fkey "
            "with ON DELETE SET NULL; transaction rolled back. error=%s",
            exc,
        )
        raise

    # --- site_settings ---
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS site_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            value_type TEXT NOT NULL DEFAULT 'str',
            updated_at TIMESTAMPTZ DEFAULT NOW(),
            updated_by TEXT
        )
    """)

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS model_visibility_overrides (
            model_id TEXT PRIMARY KEY,
            required_role TEXT NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_by TEXT
        )
    """)

    # Migrate legacy 'trial' visibility overrides to 'free'. Trial was
    # removed from VALID_ROLES, so an override left at 'trial' would be
    # treated as invalid and fail closed to admin, hiding the model.
    tag = await conn.execute(
        "UPDATE model_visibility_overrides SET required_role = 'free' WHERE required_role = 'trial'"
    )
    migrated_overrides = _parse_command_tag_count(tag)
    if migrated_overrides:
        logger.info(
            "Migrated %d model visibility overrides from trial to free.",
            migrated_overrides,
        )

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS model_concurrency_exemptions (
            model_id TEXT PRIMARY KEY,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_by TEXT
        )
    """)

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS provider_weight_overrides (
            model_id TEXT NOT NULL,
            endpoint_id TEXT NOT NULL,
            weight DOUBLE PRECISION NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_by TEXT,
            PRIMARY KEY (model_id, endpoint_id)
        )
    """)
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pwo_model ON provider_weight_overrides(model_id)"
    )

    # Admin-set capabilities for one route (context window, output cap, input
    # modalities, tool support). A row overrides the route's models.yaml values
    # or the values it was created with; no row means the route keeps those.
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS provider_route_capabilities (
            model_id TEXT NOT NULL,
            route_id TEXT NOT NULL,
            capabilities JSONB NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_by TEXT,
            PRIMARY KEY (model_id, route_id)
        )
    """)

    # Admin-controlled kill switch: a row here means the provider label is
    # excluded from routing (all its endpoints treated as weight 0). Empty
    # table = every provider enabled, so the default state needs no seeding.
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS disabled_providers (
            provider TEXT PRIMARY KEY,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_by TEXT
        )
    """)

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS provider_route_configs (
            model_id TEXT NOT NULL,
            route_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            openrouter_sort TEXT,
            deployment_type TEXT,
            base_url TEXT NOT NULL,
            api_key_id TEXT,
            provider_model_id TEXT NOT NULL,
            quota_limit INTEGER,
            concurrency_limit INTEGER,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_by TEXT,
            PRIMARY KEY (model_id, route_id)
        )
    """)
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_prc_model ON provider_route_configs(model_id)"
    )
    await apply_column_migrations(
        conn,
        "provider_route_configs",
        [
            (
                "quota_limit",
                "ALTER TABLE provider_route_configs ADD COLUMN IF NOT EXISTS quota_limit INTEGER",
            ),
            (
                "concurrency_limit",
                "ALTER TABLE provider_route_configs "
                "ADD COLUMN IF NOT EXISTS concurrency_limit INTEGER",
            ),
            (
                "openrouter_sort",
                "ALTER TABLE provider_route_configs ADD COLUMN IF NOT EXISTS openrouter_sort TEXT",
            ),
            (
                "deployment_type",
                "ALTER TABLE provider_route_configs ADD COLUMN IF NOT EXISTS deployment_type TEXT",
            ),
        ],
    )

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS provider_route_candidates (
            model_id TEXT NOT NULL,
            route_id TEXT NOT NULL,
            route_type TEXT NOT NULL,
            provider TEXT NOT NULL,
            openrouter_sort TEXT,
            deployment_type TEXT NOT NULL DEFAULT 'auto',
            base_url TEXT NOT NULL,
            api_key_id TEXT,
            provider_model_id TEXT NOT NULL,
            quota_limit INTEGER,
            concurrency_limit INTEGER,
            weight DOUBLE PRECISION NOT NULL DEFAULT 1.0,
            pricing JSONB,
            capabilities JSONB,
            endpoint_id TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_by TEXT,
            PRIMARY KEY (model_id, route_id)
        )
    """)
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_prcand_model ON provider_route_candidates(model_id)"
    )
    await apply_column_migrations(
        conn,
        "provider_route_candidates",
        [
            (
                "openrouter_sort",
                "ALTER TABLE provider_route_candidates "
                "ADD COLUMN IF NOT EXISTS openrouter_sort TEXT",
            ),
            (
                "pricing",
                "ALTER TABLE provider_route_candidates ADD COLUMN IF NOT EXISTS pricing JSONB",
            ),
            (
                "capabilities",
                "ALTER TABLE provider_route_candidates ADD COLUMN IF NOT EXISTS capabilities JSONB",
            ),
            (
                "deployment_type",
                "ALTER TABLE provider_route_candidates "
                "ADD COLUMN IF NOT EXISTS deployment_type TEXT NOT NULL DEFAULT 'auto'",
            ),
            # Set only after an edit moved the route to another endpoint;
            # NULL means the endpoint id is the route id.
            (
                "endpoint_id",
                "ALTER TABLE provider_route_candidates ADD COLUMN IF NOT EXISTS endpoint_id TEXT",
            ),
        ],
    )

    # Admin state of one route, by its stable route id: disabled (kept,
    # selected by nothing) and/or removed (a config route taken out of this
    # deployment). No row means enabled and present.
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS provider_route_states (
            model_id TEXT NOT NULL,
            route_id TEXT NOT NULL,
            disabled BOOLEAN NOT NULL DEFAULT FALSE,
            removed BOOLEAN NOT NULL DEFAULT FALSE,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_by TEXT,
            PRIMARY KEY (model_id, route_id)
        )
    """)

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS routewise_probe_samples (
            id BIGSERIAL PRIMARY KEY,
            model_id TEXT NOT NULL,
            endpoint_id TEXT NOT NULL,
            ttft_ms DOUBLE PRECISION,
            ok BOOLEAN NOT NULL,
            error TEXT,
            cost_usd DOUBLE PRECISION,
            checked_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_rw_probe_endpoint_time "
        "ON routewise_probe_samples(endpoint_id, checked_at DESC)"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_rw_probe_model_time "
        "ON routewise_probe_samples(model_id, checked_at DESC)"
    )
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS routewise_probe_leases (
            lease_key TEXT PRIMARY KEY,
            holder_id TEXT NOT NULL,
            expires_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)

    # --- provider_api_keys ---
    # Runtime-managed upstream provider credentials added by admins
    # through the dashboard. Augments env-var-sourced keys at boot.
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS provider_api_keys (
            id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            api_key TEXT NOT NULL,
            key_prefix TEXT NOT NULL,
            label TEXT,
            status TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'disabled')),
            created_by TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            min_role TEXT NOT NULL DEFAULT 'free'
        )
    """)
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_provider_api_keys_provider_status "
        "ON provider_api_keys(provider, status)"
    )
    # Tier reservation: the lowest role allowed to spend the key. Added
    # after the table shipped, so migrate rather than assume. No CHECK
    # constraint — the role vocabulary lives in ``VALID_ROLES`` and is
    # enforced at the admin API; a future role must not require a DDL lock
    # on a table the request path reads at boot.
    await apply_column_migrations(
        conn,
        "provider_api_keys",
        [
            (
                "min_role",
                "ALTER TABLE provider_api_keys "
                "ADD COLUMN IF NOT EXISTS min_role TEXT NOT NULL DEFAULT 'free'",
            ),
        ],
    )
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS provider_definitions (
            provider TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            adapter_kind TEXT NOT NULL,
            default_base_url TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'disabled')),
            created_by TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_by TEXT,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_provider_definitions_status ON provider_definitions(status)"
    )
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS disabled_provider_env_keys (
            provider TEXT NOT NULL,
            key_hash TEXT NOT NULL,
            key_prefix TEXT NOT NULL,
            disabled_by TEXT,
            disabled_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (provider, key_hash)
        )
    """)
    await conn.execute("DROP INDEX IF EXISTS idx_disabled_provider_env_keys_provider")

    # --- provider_env_key_min_roles ---
    # Tier reservation for env-sourced provider keys. An env credential has
    # no row of its own (only the var it came from), so — exactly like the
    # disable tombstone above — the reservation is keyed by the key's hash.
    # Absent row means unreserved; a row is deleted rather than set to
    # 'free', so the table reads as "the env keys that are reserved".
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS provider_env_key_min_roles (
            provider TEXT NOT NULL,
            key_hash TEXT NOT NULL,
            key_prefix TEXT NOT NULL,
            min_role TEXT NOT NULL,
            updated_by TEXT,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (provider, key_hash)
        )
    """)

    if deferred is not None:
        raise SchemaLockUnavailable(
            "the account migrations (login names, phone numbers, accounts without an "
            "email, phone code purposes) are not all applied yet; until they are, "
            "first-run setup and phone sign-in answer 503 and the rest works on the "
            f"current tables: {deferred}"
        ) from deferred
