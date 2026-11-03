"""The account columns an upgraded ``users`` table gained, frozen.

``login_name`` (first-run setup) and ``phone`` / ``phone_verified`` (phone
sign-in), a nullable ``email`` and ``password_hash``, and a ``phone`` with a
nullable ``email`` on ``login_events``. Copied from
``serving.storage.account_schema``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from migrations.baseline.ddl import bounded_ddl, column_metadata, execute_ddl, existing_columns

if TYPE_CHECKING:
    from collections.abc import Sequence

    import asyncpg


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
