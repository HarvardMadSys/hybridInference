"""Account columns an upgraded ``users`` table may not have yet.

Later releases gave ``users`` a ``login_name`` (first-run setup) and ``phone`` /
``phone_verified`` (phone sign-in), made its ``email`` and ``password_hash``
nullable, and gave ``login_events`` a ``phone`` and a nullable ``email``. A fresh
database's ``CREATE TABLE`` has all of it; an existing one needs a migration,
and that needs ``ACCESS EXCLUSIVE`` on tables the nightly ``pg_dump`` holds
``ACCESS SHARE`` on for hours. The first boot after an upgrade can land inside
that window, so the migration is deferrable like the others in this layer
(:mod:`serving.storage.log_schema`): it gives up after the bounded lock wait,
and bootstrap serves and retries it in the background.

Deferring it is safe because nothing reads a column that is not there.
:class:`AccountSchema` is what the catalog holds; the operational store reads it
whenever it runs its migrations and builds its queries from it. A missing column
reads as the value every row would hold, since no account can have a value in a
column that does not exist yet; a write that needs a missing column or index
raises :class:`~serving.exceptions.AccountSchemaPending`, which routes answer
with 503.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from serving.storage.log_schema import bounded_ddl, column_metadata, execute_ddl, existing_columns

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    import asyncpg

# The users columns the migration adds, each with the value a row reads as
# while the column is missing.
DEFERRABLE_USER_COLUMNS: dict[str, str] = {
    "login_name": "NULL::text",
    "phone": "NULL::text",
    "phone_verified": "FALSE",
}

LOGIN_NAME_INDEX = "idx_users_login_name"
PHONE_INDEX = "idx_users_phone_unique"

# In the order the migration issues them: columns, then nullability, then the
# unique indexes over the new columns.
_USERS_COLUMN_DDL = (
    ("login_name", "ALTER TABLE users ADD COLUMN IF NOT EXISTS login_name TEXT"),
    ("phone", "ALTER TABLE users ADD COLUMN IF NOT EXISTS phone TEXT"),
    (
        "phone_verified",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS phone_verified BOOLEAN NOT NULL DEFAULT FALSE",
    ),
)
# The first-run administrator has no email; a phone account has neither an
# email nor a password.
_USERS_NULLABLE_COLUMNS = ("email", "password_hash")
_USERS_INDEX_DDL = (
    (
        LOGIN_NAME_INDEX,
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_login_name "
        "ON users (lower(login_name)) WHERE login_name IS NOT NULL",
    ),
    (
        PHONE_INDEX,
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_phone_unique "
        "ON users(phone) WHERE phone IS NOT NULL",
    ),
)


@dataclass(frozen=True)
class AccountSchema:
    """What the account migration has installed, as of one catalog read.

    The defaults are the schema this release creates, so a store that has not
    read the catalog yet queries exactly as before.

    Attributes:
        columns: Which of :data:`DEFERRABLE_USER_COLUMNS` ``users`` has.
        email_nullable: ``users.email`` admits NULL.
        password_hash_nullable: ``users.password_hash`` admits NULL.
        login_name_index: The unique index on login names exists.
        phone_index: The unique index on phone numbers exists.
        login_event_phones: ``login_events`` has ``phone`` and admits a row
            without an email.
    """

    columns: frozenset[str] = frozenset(DEFERRABLE_USER_COLUMNS)
    email_nullable: bool = True
    password_hash_nullable: bool = True
    login_name_index: bool = True
    phone_index: bool = True
    login_event_phones: bool = True

    def has(self, column: str) -> bool:
        """Whether ``users`` has *column*; a column the migration does not add always is."""
        return column not in DEFERRABLE_USER_COLUMNS or column in self.columns

    def column(self, column: str, alias: str | None = None) -> str:
        """*column* as an expression: the column, or the value it would hold for every row."""
        if self.has(column):
            return f"{alias}.{column}" if alias else column
        return DEFERRABLE_USER_COLUMNS[column]

    def select(self, columns: Iterable[str], alias: str | None = None) -> str:
        """A select list of *columns*, each missing one replaced by that value under its name."""
        return ", ".join(
            self.column(name, alias) if self.has(name) else f"{self.column(name)} AS {name}"
            for name in columns
        )

    @property
    def login_names(self) -> bool:
        """Whether an account can be created with a login name and no email address."""
        return self.has("login_name") and self.login_name_index and self.email_nullable

    @property
    def phone_accounts(self) -> bool:
        """Whether phone registration, sign-in and binding can run."""
        return (
            self.has("phone")
            and self.has("phone_verified")
            and self.phone_index
            and self.email_nullable
            and self.password_hash_nullable
            and self.login_event_phones
        )


COMPLETE = AccountSchema()


async def _nullable(conn: asyncpg.Connection, table: str, column: str) -> bool:
    """Whether *column* admits NULL; a column that is not there has nothing to relax."""
    meta = await column_metadata(conn, table, column)
    return meta is None or not meta["not_null"]


async def _index_names(conn: asyncpg.Connection, table: str) -> set[str]:
    """Names of the indexes on *table*, as resolved through the search path."""
    rows = await conn.fetch(
        "SELECT c.relname FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
        "WHERE i.indrelid = to_regclass($1)",
        table,
    )
    return {row["relname"] for row in rows}


async def read_account_schema(conn: asyncpg.Connection) -> AccountSchema:
    """Read from the catalog what the account migration has installed.

    Only catalog reads, so it answers at once while another session holds
    ``users``.
    """
    users = await existing_columns(conn, "users")
    if not users:
        # No users table to describe: the CREATE TABLE that makes one gives
        # it every column.
        return COMPLETE
    events = await existing_columns(conn, "login_events")
    indexes = await _index_names(conn, "users")
    return AccountSchema(
        columns=frozenset(DEFERRABLE_USER_COLUMNS).intersection(users),
        email_nullable=await _nullable(conn, "users", "email"),
        password_hash_nullable=await _nullable(conn, "users", "password_hash"),
        login_name_index=LOGIN_NAME_INDEX in indexes,
        phone_index=PHONE_INDEX in indexes,
        login_event_phones=not events
        or ("phone" in events and await _nullable(conn, "login_events", "email")),
    )


async def _apply_atomically(conn: asyncpg.Connection, statements: Sequence[str]) -> None:
    """Run *statements* in one transaction under the bounded lock wait, if there are any.

    A lock timeout rolls all of them back, so a retry starts from the table as
    it was rather than from a partial migration.
    """
    if not statements:
        return
    async with bounded_ddl(conn), conn.transaction():
        for ddl in statements:
            await execute_ddl(conn, ddl)


async def migrate_users_account_schema(conn: asyncpg.Connection) -> None:
    """Add what ``users`` lacks of the account columns and their unique indexes.

    Catalog-gated, so a settled database gets no DDL. Whatever is missing is
    issued as one transaction: the new columns, then the relaxed NOT NULLs,
    then the unique indexes over the new columns. It lands whole or not at
    all, and the index guarding a column's uniqueness never lags the column.

    Raises:
        SchemaLockUnavailable: Another session holds ``users`` (usually
            ``pg_dump``); nothing changed, so retry later.
    """
    present = await existing_columns(conn, "users")
    statements = [ddl for column, ddl in _USERS_COLUMN_DDL if column not in present]
    for column in _USERS_NULLABLE_COLUMNS:
        if not await _nullable(conn, "users", column):
            statements.append(f"ALTER TABLE users ALTER COLUMN {column} DROP NOT NULL")
    indexes = await _index_names(conn, "users")
    statements.extend(ddl for index, ddl in _USERS_INDEX_DDL if index not in indexes)
    await _apply_atomically(conn, statements)


async def migrate_login_events_account_schema(conn: asyncpg.Connection) -> None:
    """Give ``login_events`` its ``phone`` column and a nullable ``email``, as one transaction.

    A phone sign-in records its number and may have no email to record.

    Raises:
        SchemaLockUnavailable: Another session holds ``login_events``;
            nothing changed, so retry later.
    """
    statements = []
    if "phone" not in await existing_columns(conn, "login_events"):
        statements.append("ALTER TABLE login_events ADD COLUMN IF NOT EXISTS phone TEXT")
    if not await _nullable(conn, "login_events", "email"):
        statements.append("ALTER TABLE login_events ALTER COLUMN email DROP NOT NULL")
    await _apply_atomically(conn, statements)


# The account schema as the serving process's operational store last read it,
# for SQL written outside the store (the admin request and analytics views).
_current = COMPLETE


def current() -> AccountSchema:
    """Return the account schema the operational store last read; complete until it reads one."""
    return _current


def publish(schema: AccountSchema) -> None:
    """Record *schema* as what :func:`current` returns."""
    global _current
    _current = schema
