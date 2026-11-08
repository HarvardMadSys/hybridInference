"""The lookups every request makes never carry an account's whole preferences.

An account holder could store a layout of any size (31 MiB was measured), and
every API call with their keys -- and every console request -- then moved it
from the database and decoded it on the event loop, and every cache hit deep-
copied it. The auth and user-row lookups now select only the entries a request
acts on, a write the account holder makes is bounded, and a whole-document read
of an oversized document written before that bound takes the same entries only.

Mocked connections, no database. ``tests/servers/test_user_preferences_atomic.py``
runs the same statements against PostgreSQL.
"""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from serving.exceptions import PreferencesTooLargeError
from serving.storage.base import USER_PREFERENCES_MAX_BYTES
from serving.storage.postgres_operational import PostgresOperationalStore

pytestmark = pytest.mark.asyncio

# A select-list item that is the bare column: not an expression over it, nor its alias.
_WHOLE_PREFERENCES = re.compile(r"(?<![\w.(>])(?<!AS )(u\.)?preferences\s*(,|\s+FROM)")


@pytest.fixture
def pg_conn() -> MagicMock:
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock(return_value="UPDATE 1")
    return conn


@pytest.fixture
def store(pg_conn: MagicMock) -> PostgresOperationalStore:
    pool = MagicMock()

    @asynccontextmanager
    async def _acquire(*_args, **_kwargs):
        yield pg_conn

    pool.acquire = _acquire
    return PostgresOperationalStore(pool, request_pool=pool)


@pytest.mark.parametrize(
    "lookup",
    ["get_auth_context_by_key_hash", "get_auth_context_with_spend", "get_auth_context_lightweight"],
)
async def test_an_api_key_lookup_selects_only_the_disabled_models(store, pg_conn, lookup):
    pg_conn.fetchrow.return_value = {
        "user_id": "u1",
        "preferences": '{"disabled_models": ["m1"]}',
    }

    row = await getattr(store, lookup)("hash")

    sql = pg_conn.fetchrow.await_args.args[0]
    assert not _WHOLE_PREFERENCES.search(sql), sql
    assert "u.preferences -> 'disabled_models'" in sql
    assert "'locale'" not in sql and "llm_prober_layout" not in sql
    assert row["preferences"] == {"disabled_models": ["m1"]}


@pytest.mark.parametrize(
    ("lookup", "argument"),
    [
        ("get_user_by_id", "u1"),
        ("get_user_by_email", "a@example.org"),
        ("get_user_by_login_name", "admin"),
    ],
)
async def test_a_user_row_carries_only_the_entries_a_request_reads(
    store, pg_conn, lookup, argument
):
    await getattr(store, lookup)(argument)

    sql = pg_conn.fetchrow.await_args.args[0]
    assert not _WHOLE_PREFERENCES.search(sql), sql
    assert "preferences -> 'disabled_models'" in sql
    assert "preferences -> 'locale'" in sql
    assert "AS preferences" in sql


async def test_a_whole_document_read_of_an_oversized_one_takes_the_request_entries(store, pg_conn):
    pg_conn.fetchrow.return_value = {"preferences": '{"llm_prober_layout": {}, "locale": null}'}

    preferences = await store.get_user_preferences("u1")

    sql, user_id, bound = pg_conn.fetchrow.await_args.args
    assert user_id == "u1"
    # The stored size is tried before the text is measured: CASE keeps the order.
    assert sql.index("pg_column_size(preferences) > $2") < sql.index("octet_length(")
    assert "ELSE preferences END" in sql
    # Well above what an account holder may write, so admin entries never tip it.
    assert bound >= 2 * USER_PREFERENCES_MAX_BYTES
    assert preferences == {"llm_prober_layout": {}, "locale": None}


async def test_a_bounded_write_is_refused_in_the_statement_that_would_make_it(store, pg_conn):
    pg_conn.fetchval.side_effect = [None, True]  # not written; the user exists

    with pytest.raises(PreferencesTooLargeError) as refused:
        await store.set_user_preference("u1", "llm_prober_layout", {"x": 1}, max_total_bytes=100)

    update_sql, *params = pg_conn.fetchval.await_args_list[0].args
    assert update_sql.startswith("UPDATE users SET preferences = jsonb_set(")
    # The bound is on the whole document as written, checked in the same UPDATE.
    assert "AND octet_length(jsonb_set(" in update_sql
    assert "::text) <= $4" in update_sql
    assert params == ["u1", "llm_prober_layout", '{"x": 1}', 100]
    assert refused.value.limit_bytes == 100
    pg_conn.execute.assert_not_awaited()


async def test_a_bounded_write_that_fits_is_made(store, pg_conn):
    pg_conn.fetchval.side_effect = [True]

    assert await store.set_user_preference("u1", "k", [1], max_total_bytes=100) is True
    assert pg_conn.fetchval.await_count == 1


async def test_a_bounded_write_for_no_user_reports_none(store, pg_conn):
    pg_conn.fetchval.side_effect = [None, None]

    assert await store.set_user_preference("gone", "k", [1], max_total_bytes=100) is False


async def test_an_unbounded_write_is_the_plain_update(store, pg_conn):
    """An administrator's write: the account holder cannot block it by filling the document."""
    assert await store.set_user_preference("u1", "disabled_models", ["m"]) is True

    sql = pg_conn.execute.await_args.args[0]
    assert "octet_length" not in sql
    pg_conn.fetchval.assert_not_awaited()
