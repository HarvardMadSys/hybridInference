"""The operational store on a users table its upgrade has not migrated yet.

The first boot after an upgrade can meet the nightly ``pg_dump`` holding every
table, so the migration that adds login names and phone numbers gives up after
its lock timeout and is retried from the background. Meanwhile the store reads
only the columns that exist. Mocked connections over a small model of the
catalog, no database: ``tests/integration/storage/test_users_schema_lock_postgres.py``
runs the same upgrade against PostgreSQL with a reader holding the table.
"""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from typing import Any

import pytest

from serving.exceptions import AccountSchemaPending
from serving.servers import bootstrap
from serving.servers.routers.admin.metrics import _build_recent_requests_filters
from serving.storage import account_schema, postgres_operational
from serving.storage.account_schema import (
    COMPLETE,
    AccountSchema,
    migrate_login_events_account_schema,
    migrate_users_account_schema,
    read_account_schema,
)
from serving.storage.log_schema import SchemaLockUnavailable
from serving.storage.postgres_operational import (
    PostgresOperationalStore,
    RequiredOperationalSchemaUnavailable,
)

PENDING_IDENTITY = "NULL::text AS login_name, NULL::text AS phone, FALSE AS phone_verified"


class _LockTimeout(Exception):
    """What asyncpg raises when ``lock_timeout`` expires."""

    sqlstate = "55P03"


def _users_before_login_names() -> dict[str, bool]:
    """``users`` as the release before #1515 created it: column -> NOT NULL."""
    return {
        "id": True,
        "email": True,
        "password_hash": True,
        "user_name": False,
        "preferences": True,
        "role": True,
        "email_verified": False,
        "status": False,
        "hard_delete_pending": True,
        "hard_delete_claim_token": False,
        "approval_note": False,
        "reviewed_at": False,
        "reviewed_by": False,
        "signup_reason": False,
        "created_at": False,
        "last_login_at": False,
        "max_concurrent_requests": False,
        "admin_note": False,
        "suspension_message": False,
    }


def _login_events_before_phones() -> dict[str, bool]:
    return {
        "id": True,
        "created_at": False,
        "user_id": False,
        "email": True,
        "outcome": True,
        "failure_reason": False,
        "ip": False,
        "user_agent": False,
    }


# Constraints as the current release defines them, so the store leaves them be.
_SETTLED_CONSTRAINTS = {
    "users_status_check": (
        "CHECK ((status = ANY (ARRAY['active'::text, 'suspended'::text, 'deleted'::text, "
        "'pending_approval'::text, 'rejected'::text])))"
    ),
    "users_role_check": (
        "CHECK ((role = ANY (ARRAY['free'::text, 'pro'::text, 'internal'::text, 'admin'::text])))"
    ),
    "signup_allowed_domains_created_by_fkey": (
        "FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL"
    ),
}

_ADD_COLUMN = re.compile(r"ALTER TABLE (\w+) ADD COLUMN IF NOT EXISTS (\w+) (.*)")
_DROP_NOT_NULL = re.compile(r"ALTER TABLE (\w+) ALTER COLUMN (\w+) DROP NOT NULL")
_CREATE_INDEX = re.compile(r"CREATE (?:UNIQUE )?INDEX IF NOT EXISTS (\w+) ON (\w+)")
_ALTER_TABLE = re.compile(r"ALTER TABLE (\w+) ")


class _Database:
    """The catalog the store's DDL acts on, and a connection to it.

    DDL takes effect at COMMIT (at once outside a transaction) and not at all
    after ROLLBACK. An ``ALTER TABLE`` on a table in ``held`` times out, as it
    does while ``pg_dump`` holds ACCESS SHARE on it; other statements do not
    conflict with that lock. ``statements`` records what ran, transaction
    boundaries included; ``queries`` the reads that are not catalog lookups.
    """

    def __init__(
        self,
        *,
        users: dict[str, bool] | None = None,
        login_events: dict[str, bool] | None = None,
        users_indexes: set[str] | None = None,
    ) -> None:
        self.tables: dict[str, dict[str, bool]] = {
            "users": _users_before_login_names() if users is None else users,
            "login_events": _login_events_before_phones() if login_events is None else login_events,
        }
        self.indexes: dict[str, set[str]] = {"users": set(users_indexes or ())}
        self.held: set[str] = set()
        self.statements: list[str] = []
        self.queries: list[str] = []
        self._pending: list[Any] | None = None

    @classmethod
    def migrated(cls) -> _Database:
        """A database the upgrade has finished: the current release's account schema."""
        users = _users_before_login_names()
        users.update(email=False, password_hash=False)
        users.update(login_name=False, phone=False, phone_verified=True)
        events = _login_events_before_phones()
        events.update(email=False, phone=False)
        return cls(
            users=users,
            login_events=events,
            users_indexes={account_schema.LOGIN_NAME_INDEX, account_schema.PHONE_INDEX},
        )

    def _effect(self, statement: str) -> Any:
        if match := _ADD_COLUMN.match(statement):
            table, column, definition = match.groups()
            return lambda: self.tables.setdefault(table, {}).setdefault(
                column, "NOT NULL" in definition
            )
        if match := _DROP_NOT_NULL.match(statement):
            table, column = match.groups()
            return lambda: self.tables[table].__setitem__(column, False)
        if match := _CREATE_INDEX.match(statement):
            index, table = match.groups()
            return lambda: self.indexes.setdefault(table, set()).add(index)
        return None

    async def execute(self, sql: str, *args: Any) -> str:
        statement = " ".join(sql.split())
        self.statements.append(statement)
        altered = _ALTER_TABLE.match(statement)
        if altered and altered.group(1) in self.held:
            raise _LockTimeout(f"canceling statement due to lock timeout: {statement}")
        effect = self._effect(statement)
        if effect is not None:
            if self._pending is None:
                effect()
            else:
                self._pending.append(effect)
        return "UPDATE 0"

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        if "FROM pg_attribute" in sql:
            return [{"attname": column} for column in self.tables.get(args[0], {})]
        if "FROM pg_index" in sql:
            return [{"relname": index} for index in self.indexes.get(args[0], set())]
        self.queries.append(" ".join(sql.split()))
        return []

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        if "pg_attribute a" in sql:  # column_metadata
            table, column = args
            columns = self.tables.get(table, {})
            if column not in columns:
                return None
            default = "'free'::text" if (table, column) == ("users", "role") else None
            return {"not_null": columns[column], "default_expr": default}
        self.queries.append(" ".join(sql.split()))
        return None

    async def fetchval(self, sql: str, *args: Any) -> Any:
        if "pg_get_constraintdef" in sql:
            return _SETTLED_CONSTRAINTS.get(args[1])
        self.queries.append(" ".join(sql.split()))
        return None

    def transaction(self) -> _Transaction:
        return _Transaction(self)

    @asynccontextmanager
    async def acquire(self):
        yield self

    def ddl(self) -> list[str]:
        """The statements that ran, without the bootstrap's plain reads and writes."""
        return [s for s in self.statements if s.startswith(("ALTER", "CREATE UNIQUE", "BEGIN"))]


class _Transaction:
    def __init__(self, database: _Database) -> None:
        self._database = database

    async def __aenter__(self) -> None:
        self._database.statements.append("BEGIN")
        self._database._pending = []

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        pending, self._database._pending = self._database._pending or [], None
        if exc_type is None:
            self._database.statements.append("COMMIT")
            for effect in pending:
                effect()
        else:
            self._database.statements.append("ROLLBACK")
        return False


@pytest.fixture(autouse=True)
def _process_view(monkeypatch):
    """Each test starts, and leaves the process, with the complete account schema."""
    monkeypatch.setattr(account_schema, "_current", COMPLETE)


@pytest.fixture
async def deferred() -> tuple[PostgresOperationalStore, _Database]:
    """A store that started while ``pg_dump`` held a database from before #1515."""
    database = _Database()
    database.held = {"users", "login_events"}
    store = PostgresOperationalStore(database)
    with pytest.raises(SchemaLockUnavailable) as excinfo:
        await store.initialize()
    assert not isinstance(excinfo.value, RequiredOperationalSchemaUnavailable)
    database.statements.clear()
    database.queries.clear()
    return store, database


# -- the schema view -----------------------------------------------------------


def test_a_missing_column_reads_as_the_value_every_row_would_hold():
    pending = AccountSchema(columns=frozenset())
    columns = ("id", "login_name", "phone", "phone_verified", "user_name")

    assert pending.select(columns, "u") == f"u.id, {PENDING_IDENTITY}, u.user_name"
    assert pending.select(columns) == f"id, {PENDING_IDENTITY}, user_name"
    assert pending.column("login_name", "u") == "NULL::text"
    assert pending.column("email", "u") == "u.email"


def test_the_complete_schema_selects_every_column():
    assert COMPLETE.select(("id", "login_name", "phone", "phone_verified"), "u") == (
        "u.id, u.login_name, u.phone, u.phone_verified"
    )
    assert COMPLETE.column("login_name", "u") == "u.login_name"


@pytest.mark.parametrize(
    ("schema", "login_names", "phone_accounts"),
    [
        (COMPLETE, True, True),
        # A column without the index that keeps it unique is not writable yet.
        (AccountSchema(login_name_index=False), False, True),
        (AccountSchema(phone_index=False), True, False),
        # An administrator without an email needs a nullable email.
        (AccountSchema(email_nullable=False), False, False),
        (AccountSchema(password_hash_nullable=False), True, False),
        (AccountSchema(login_event_phones=False), True, False),
        (AccountSchema(columns=frozenset({"login_name", "phone"})), True, False),
    ],
)
def test_a_write_needs_every_object_it_relies_on(schema, login_names, phone_accounts):
    assert (schema.login_names, schema.phone_accounts) == (login_names, phone_accounts)


async def test_the_catalog_of_a_database_before_the_upgrade():
    schema = await read_account_schema(_Database())

    assert schema == AccountSchema(
        columns=frozenset(),
        email_nullable=False,
        password_hash_nullable=False,
        login_name_index=False,
        phone_index=False,
        login_event_phones=False,
    )


async def test_the_catalog_of_a_migrated_database_is_complete():
    assert await read_account_schema(_Database.migrated()) == COMPLETE


# -- the migration -------------------------------------------------------------


async def test_the_migration_is_one_transaction_in_dependency_order():
    database = _Database()

    await migrate_users_account_schema(database)

    assert database.statements == [
        "SET lock_timeout = '3s'",
        "BEGIN",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS login_name TEXT",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS phone TEXT",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS phone_verified BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE users ALTER COLUMN email DROP NOT NULL",
        "ALTER TABLE users ALTER COLUMN password_hash DROP NOT NULL",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_login_name "
        "ON users (lower(login_name)) WHERE login_name IS NOT NULL",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_phone_unique "
        "ON users(phone) WHERE phone IS NOT NULL",
        "COMMIT",
        "SET lock_timeout = DEFAULT",
    ]
    await migrate_login_events_account_schema(database)
    assert await read_account_schema(database) == COMPLETE


async def test_a_held_table_rolls_the_whole_migration_back():
    database = _Database()
    database.held = {"users"}

    with pytest.raises(SchemaLockUnavailable):
        await migrate_users_account_schema(database)

    assert database.statements[1:3] == [
        "BEGIN",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS login_name TEXT",
    ]
    assert database.statements[-2:] == ["ROLLBACK", "SET lock_timeout = DEFAULT"]
    # Nothing landed, so the retry starts from the table as it was.
    assert await read_account_schema(database) == await read_account_schema(_Database())


async def test_a_partly_migrated_table_gets_only_what_it_lacks():
    """As the boot-time logger's copy of the users DDL can leave it."""
    users = _users_before_login_names()
    users.update(login_name=False, email=False)
    database = _Database(users=users)

    await migrate_users_account_schema(database)

    assert [s for s in database.ddl() if s != "BEGIN"] == [
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS phone TEXT",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS phone_verified BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE users ALTER COLUMN password_hash DROP NOT NULL",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_login_name "
        "ON users (lower(login_name)) WHERE login_name IS NOT NULL",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_phone_unique "
        "ON users(phone) WHERE phone IS NOT NULL",
    ]


async def test_a_settled_database_gets_no_ddl_and_takes_no_lock():
    database = _Database.migrated()

    await migrate_users_account_schema(database)
    await migrate_login_events_account_schema(database)

    assert database.statements == []


# -- the store while the migration waits -----------------------------------------


async def test_startup_defers_after_building_everything_else():
    database = _Database()
    database.held = {"users", "login_events"}

    with pytest.raises(SchemaLockUnavailable) as excinfo:
        await PostgresOperationalStore(database).initialize()

    assert not isinstance(excinfo.value, RequiredOperationalSchemaUnavailable)
    statements = database.statements
    first_alter = next(i for i, s in enumerate(statements) if s.startswith("ALTER TABLE users"))
    # Tables sign-in and deletion need come before anything that can be deferred.
    for table in ("browser_sessions", "browser_session_accounts", "phone_auth_codes"):
        created = statements.index(next(s for s in statements if f"EXISTS {table} (" in s))
        assert created < first_alter
    assert any("CREATE TABLE IF NOT EXISTS provider_env_key_min_roles" in s for s in statements)
    # After users timed out, login_events waits for the same retry instead of
    # queueing a second ACCESS EXCLUSIVE request behind the same reader.
    assert not any(s.startswith("ALTER TABLE login_events") for s in statements)


async def test_bootstrap_serves_and_retries_in_the_background(monkeypatch):
    """The startup path: the deferral is logged and retried, not fatal."""
    scheduled: list[tuple[Any, str]] = []
    monkeypatch.setattr(
        bootstrap,
        "_schedule_deferred_schema_migration",
        lambda ensure_schema, subject: scheduled.append((ensure_schema, subject)),
    )
    database = _Database()
    database.held = {"users", "login_events"}
    store = PostgresOperationalStore(database)

    await bootstrap._initialize_operational_store(store)

    assert scheduled == [(store.initialize, "operational schema")]
    assert not store.account_schema.login_names
    assert not store.account_schema.phone_accounts


async def test_single_user_lookups_read_missing_columns_as_empty(deferred):
    store, database = deferred

    await store.get_user_by_id("u1")
    await store.get_user_by_email("Ada@Example.com")

    for query in database.queries:
        assert PENDING_IDENTITY in query
        assert "FROM users" in query


async def test_a_login_name_lookup_finds_nobody_without_querying(deferred):
    store, database = deferred

    assert await store.get_user_by_login_name("admin") is None
    assert database.queries == []


async def test_api_key_auth_and_browser_sessions_read_missing_columns_as_empty(deferred):
    store, database = deferred

    await store.get_auth_context_by_key_hash("hash")
    await store.get_auth_context_lightweight("hash")
    await store.list_browser_session_accounts("browser")

    assert len(database.queries) == 3
    for query in database.queries:
        assert "u.email, " + PENDING_IDENTITY in query
        assert "u.login_name" not in query and "u.phone" not in query


async def test_admin_lists_name_no_missing_column(deferred):
    store, database = deferred

    await store.list_users(search="ada")
    await store.get_users_summary()

    assert database.queries
    for query in database.queries:
        assert "login_name ILIKE" not in query and "phone ILIKE" not in query
        assert "u.login_name" not in query and "u.phone" not in query
    assert any("u.email ILIKE $1 OR u.user_name ILIKE $1" in q for q in database.queries)


async def test_a_signup_names_only_columns_every_release_has(deferred):
    store, database = deferred

    await store.create_user(user_id="u1", email="Bob@Example.com", password_hash="h")

    [insert] = [s for s in database.statements if s.startswith("INSERT INTO users")]
    assert "login_name" not in insert


@pytest.mark.parametrize(
    ("method", "kwargs"),
    [
        (
            "create_first_admin",
            {
                "user_id": "u-admin",
                "login_name": "admin",
                "password_hash": "h",
                "user_name": "Ops",
                "marker_key": "setup_completed_at",
                "marker_value": "now",
                "code_key": "setup_code",
                "admin_ip": "203.0.113.7",
            },
        ),
        (
            "create_user",
            {"user_id": "u1", "email": None, "password_hash": "h", "login_name": "admin"},
        ),
        ("require_phone_accounts", {}),
        ("get_user_by_phone", {"phone": "+12025550123"}),
        (
            "create_phone_user",
            {
                "user_id": "u1",
                "phone": "+12025550123",
                "user_name": "P",
                "status": "active",
                "signup_reason": None,
            },
        ),
        ("bind_user_phone", {"user_id": "u1", "phone": "+12025550123"}),
    ],
)
async def test_a_write_that_needs_the_new_columns_is_refused_untouched(deferred, method, kwargs):
    store, database = deferred

    with pytest.raises(AccountSchemaPending) as excinfo:
        await getattr(store, method)(**kwargs)

    assert str(excinfo.value) == AccountSchemaPending.MESSAGE
    # Refused before anything ran: no setup lock taken, no row written.
    assert database.statements == []
    assert database.queries == []


async def test_deleting_an_account_skips_the_phone_number_it_cannot_have(deferred):
    store, database = deferred

    await store.delete_user("u1", admin_ip="203.0.113.7", admin_id="admin")

    phone_deletes = [s for s in database.statements if "phone_auth" in s]
    assert phone_deletes == ["DELETE FROM phone_auth_codes WHERE user_id = $1"]


async def test_erasing_an_account_with_a_phone_column_clears_its_number():
    database = _Database.migrated()

    await PostgresOperationalStore._forget_phone_challenges(database, "u1", erase_attempts=True)

    assert database.statements == [
        "DELETE FROM phone_auth_codes WHERE user_id = $1 OR "
        "phone = (SELECT phone FROM users WHERE id = $1)",
        "DELETE FROM phone_auth_attempts WHERE phone = (SELECT phone FROM users WHERE id = $1)",
    ]


async def test_admin_request_search_skips_a_missing_login_name(deferred):
    """Raw SQL outside the store reads the view the store last published."""
    clauses, _params, _join = _build_recent_requests_filters(days=7, user_id="ada")

    [user_clause] = [c for c in clauses if "u.email ILIKE" in c]
    assert "u.login_name" not in user_clause
    assert "OR NULL::text ILIKE" in user_clause


# -- once the lock is free ---------------------------------------------------------


async def test_the_retry_lands_the_columns_and_the_store_queries_them(deferred):
    store, database = deferred
    database.held.clear()

    await store.initialize()  # what bootstrap's background retry calls

    assert store.account_schema == COMPLETE
    assert account_schema.current() == COMPLETE
    database.queries.clear()
    await store.get_user_by_login_name(" Admin ")
    [query] = database.queries
    assert "login_name, phone, phone_verified" in query
    assert "lower(login_name) = $1" in query
    clauses, _params, _join = _build_recent_requests_filters(days=7, user_id="ada")
    assert any("OR u.login_name ILIKE" in clause for clause in clauses)


async def test_a_migration_landed_elsewhere_is_noticed_by_the_next_write(deferred, monkeypatch):
    """Another process, or the logging schema's own retry, may land it first."""
    store, database = deferred
    database.held.clear()
    await migrate_users_account_schema(database)
    await migrate_login_events_account_schema(database)
    database.statements.clear()

    # Within the re-check interval the store answers from what it last read.
    with pytest.raises(AccountSchemaPending):
        await store.require_phone_accounts()
    assert database.statements == []

    monkeypatch.setattr(postgres_operational, "_ACCOUNT_SCHEMA_RECHECK_SECONDS", 0.0)
    await store.require_phone_accounts()
    assert store.account_schema == COMPLETE
    assert await store.create_first_admin(
        user_id="u-admin",
        login_name="admin",
        password_hash="h",
        user_name="Ops",
        marker_key="setup_completed_at",
        marker_value="now",
        code_key="setup_code",
        admin_ip="203.0.113.7",
    )
