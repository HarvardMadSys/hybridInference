"""The phone sign-in tables and the purposes a code may be spent on, frozen.

Copied from ``serving.storage.phone_auth``, with the purposes as they stood.
"""

from __future__ import annotations

from typing import Any

from migrations.baseline.ddl import (
    SchemaLockUnavailable,
    bounded_ddl,
    constraint_admitted_values,
    constraint_definition,
    execute_ddl,
)

#: What a code may be spent on. Every purpose but ``signup`` and ``login``
#: belongs to a signed-in account, which must still be active when the code is
#: sent and when it is spent. ``OWNER_PURPOSES`` confirm the account's owner
#: for a sensitive change, so their codes go only to its own verified number.
OWNER_PURPOSES = ("delete_account", "bind_email", "set_password")
PHONE_CODE_PURPOSES = ("signup", "login", "bind", *OWNER_PURPOSES)
_PURPOSE_CHECK = "CHECK (purpose IN ({}))".format(
    ", ".join(f"'{purpose}'" for purpose in PHONE_CODE_PURPOSES)
)


async def initialize_phone_auth(conn: Any) -> None:
    """Install durable OTP storage idempotently.

    New tables, plus a rebuild of the code table's purpose CHECK when the
    purposes have changed. The phone columns of ``users`` (and of
    ``login_events``) are part of the deferrable account migration in
    :mod:`serving.storage.account_schema`.

    Raises:
        SchemaLockUnavailable: The CHECK rebuild could not take its lock (a
            pg_dump holds the table, say). Every table has still been
            created; the caller defers the rebuild like any other
            migration that can wait.
    """
    await conn.execute(f"""
        CREATE TABLE IF NOT EXISTS phone_auth_codes (
            phone TEXT NOT NULL,
            purpose TEXT NOT NULL {_PURPOSE_CHECK},
            user_id TEXT NOT NULL DEFAULT '',
            challenge_id TEXT NOT NULL,
            code_hash TEXT NOT NULL,
            expires_at TIMESTAMPTZ NOT NULL,
            attempts INT NOT NULL DEFAULT 0,
            active BOOLEAN NOT NULL DEFAULT FALSE,
            consumed_at TIMESTAMPTZ,
            PRIMARY KEY (phone, purpose, user_id)
        )
    """)
    # A table created before ``delete_account`` existed keeps its old CHECK.
    # Rebuild it only when it does not admit exactly the current purposes, so
    # a settled restart takes no ACCESS EXCLUSIVE lock.
    definition = await constraint_definition(
        conn, "phone_auth_codes", "phone_auth_codes_purpose_check"
    )
    # The rebuild needs ACCESS EXCLUSIVE on a table pg_dump also holds, so a
    # lock timeout is remembered and raised once the other tables exist:
    # sign-in and account deletion use them on the first boot of an upgrade.
    deferred: SchemaLockUnavailable | None = None
    if constraint_admitted_values(definition) != set(PHONE_CODE_PURPOSES):
        try:
            async with bounded_ddl(conn), conn.transaction():
                await execute_ddl(
                    conn,
                    "ALTER TABLE phone_auth_codes "
                    "DROP CONSTRAINT IF EXISTS phone_auth_codes_purpose_check",
                )
                await execute_ddl(
                    conn,
                    "ALTER TABLE phone_auth_codes "
                    f"ADD CONSTRAINT phone_auth_codes_purpose_check {_PURPOSE_CHECK}",
                )
        except SchemaLockUnavailable as exc:
            deferred = exc
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS phone_auth_attempts (
            id BIGSERIAL PRIMARY KEY,
            phone TEXT NOT NULL,
            ip TEXT NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('send', 'verify')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_phone_attempts_phone ON phone_auth_attempts(phone, kind, created_at)"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_phone_attempts_ip ON phone_auth_attempts(ip, kind, created_at)"
    )
    if deferred is not None:
        raise deferred
