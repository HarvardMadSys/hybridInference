"""The ``api_logs`` write under the erasure fence, without a database.

What each write sends: a row naming no account is one INSERT; a row naming
accounts is a transaction of two statements, the shared locks and then the fence
check with the INSERT. ``tests/integration/storage/test_erasure_fence_write_path.py``
runs the same writes against PostgreSQL.
"""

from __future__ import annotations

import contextlib
from typing import Any

from serving.storage.erasure_fence import (
    api_logs_fence_keys,
    fence_account_advisory_key,
    fence_account_digest,
    insert_api_logs_row,
)
from tests.fixtures import postgres

_SECRET = postgres.FENCE_SECRET


class _Connection:
    """Records what is sent, and answers the fenced INSERT with *hit*."""

    def __init__(self, hit: bool = False) -> None:
        self.hit = hit
        self.sent: list[tuple[str, tuple[Any, ...]]] = []

    @contextlib.asynccontextmanager
    async def _transaction(self) -> Any:
        self.sent.append(("BEGIN", ()))
        yield
        self.sent.append(("COMMIT", ()))

    def transaction(self) -> Any:
        return self._transaction()

    async def execute(self, sql: str, *args: Any) -> str:
        self.sent.append((" ".join(sql.split()), args))
        return "INSERT 0 1"

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self.sent.append((" ".join(sql.split()), args))
        return self.hit


_COLUMNS = "request_id, model_id, user_id"
_VALUES = "$1, $2, $3"
_ARGS = ("req-1", "m", "u-1")


def test_a_row_naming_no_account_or_a_store_with_no_secret_has_no_keys():
    assert api_logs_fence_keys(fence_secret=_SECRET, user_id=None, credential_owner_id=None) == []
    assert api_logs_fence_keys(fence_secret=None, user_id="u-1", credential_owner_id="u-2") == []


def test_each_distinct_account_is_one_key_and_the_keys_are_sorted():
    user, owner = (fence_account_digest(a, _SECRET) for a in ("u-1", "u-2"))

    assert api_logs_fence_keys(fence_secret=_SECRET, user_id="u-1", credential_owner_id="u-1") == [
        user
    ]
    both = sorted([user, owner])
    for user_id, owner_id in (("u-1", "u-2"), ("u-2", "u-1")):
        assert (
            api_logs_fence_keys(fence_secret=_SECRET, user_id=user_id, credential_owner_id=owner_id)
            == both
        )


async def test_a_row_naming_no_account_is_one_insert_outside_a_transaction():
    conn = _Connection()

    fenced = await insert_api_logs_row(
        conn, columns=_COLUMNS, values=_VALUES, args=_ARGS, fence_keys=[]
    )

    assert fenced is False
    assert conn.sent == [
        (
            "INSERT INTO api_logs (request_id, model_id, user_id) VALUES ($1, $2, $3) "
            "ON CONFLICT (request_id) DO NOTHING",
            _ARGS,
        )
    ]


async def test_a_row_naming_accounts_locks_them_then_checks_and_inserts_in_one_statement():
    keys = api_logs_fence_keys(fence_secret=_SECRET, user_id="u-1", credential_owner_id="u-2")
    conn = _Connection()

    fenced = await insert_api_logs_row(
        conn, columns=_COLUMNS, values=_VALUES, args=_ARGS, fence_keys=keys
    )

    assert fenced is False
    starts = ("BEGIN", "SELECT pg_advisory_xact_lock_shared(", "WITH fence AS", "COMMIT")
    assert len(conn.sent) == len(starts)
    assert all(sql.startswith(start) for (sql, _), start in zip(conn.sent, starts, strict=True))
    (_, lock_args), (insert_sql, insert_args) = conn.sent[1], conn.sent[2]
    # The locks, in the keys' order, in one statement.
    assert lock_args == ([fence_account_advisory_key(k) for k in keys],)
    # The keys are the parameter after the row's own.
    assert "account_digest = ANY($4::text[])" in insert_sql
    assert "INSERT INTO api_logs (request_id, model_id, user_id) SELECT $1, $2, $3" in insert_sql
    assert "WHERE NOT fence.hit ON CONFLICT (request_id) DO NOTHING" in insert_sql
    assert insert_args == (*_ARGS, keys)


async def test_a_tombstone_is_reported_as_fenced():
    keys = api_logs_fence_keys(fence_secret=_SECRET, user_id="u-1", credential_owner_id=None)

    assert await insert_api_logs_row(
        _Connection(hit=True), columns=_COLUMNS, values=_VALUES, args=_ARGS, fence_keys=keys
    )
