"""The account's and the key's cost counters are written together, exactly once.

The key's counter feeds the key's budgets, so a write that leaves it behind
lets the key spend past them: both counters go out in one statement, in one
transaction, and a failure applies neither. A COMMIT whose answer is lost is
settled by asking the database how the transaction ended, never by guessing.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from serving.storage import postgres_operational
from serving.storage.base import CostIncrement, CostWriteOutcomeUnknown
from serving.storage.postgres_operational import COST_INCREMENTS_SQL, PostgresOperationalStore

pytestmark = pytest.mark.asyncio


class _Tx:
    """``conn.transaction()``: commits on a clean exit, unless told to fail there."""

    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            if self._conn.commit_error is not None:
                raise self._conn.commit_error
            self._conn.committed += 1
        else:
            self._conn.rolled_back += 1


class _Conn:
    def __init__(self) -> None:
        self.fetchval = AsyncMock(return_value=12345)
        self.execute = AsyncMock()
        self.commit_error: Exception | None = None
        self.committed = 0
        self.rolled_back = 0

    def transaction(self) -> _Tx:
        return _Tx(self)


def _store(conn: _Conn, status_conn: AsyncMock | None = None) -> PostgresOperationalStore:
    conns = iter([conn, *([status_conn] * 10 if status_conn is not None else [])])

    def _acquire():
        acquire = MagicMock()
        acquire.__aenter__ = AsyncMock(return_value=next(conns))
        acquire.__aexit__ = AsyncMock(return_value=None)
        return acquire

    pool = MagicMock()
    pool.acquire = MagicMock(side_effect=_acquire)
    return PostgresOperationalStore(pool)


async def test_one_statement_writes_the_account_and_the_key_counter():
    conn = _Conn()

    await _store(conn).increment_user_cost("u1", 0.5, day="2026-11-02", api_key_id=7)

    [call] = conn.fetchval.await_args_list
    sql, users, days, keys, costs, requests, _orgs, _projects = call.args
    assert sql is COST_INCREMENTS_SQL
    assert "INSERT INTO user_daily_cost" in sql
    assert "INSERT INTO api_key_daily_cost" in sql
    assert "SELECT txid_current()" in sql
    assert (users, days, keys, costs, requests) == (["u1"], ["2026-11-02"], [7], [0.5], [1])
    assert conn.committed == 1


async def test_a_request_without_a_key_passes_no_key():
    conn = _Conn()

    await _store(conn).increment_user_cost("u1", 0.5, day="2026-11-02")

    [call] = conn.fetchval.await_args_list
    assert call.args[3] == [None]


async def test_a_batch_goes_out_in_one_statement_with_its_request_counts():
    conn = _Conn()
    increments = [
        CostIncrement("u1", "2026-11-02", 7, Decimal("0.000300"), 3),
        CostIncrement("u2", "2026-11-02", None, Decimal("1.250000"), 1),
    ]

    await _store(conn).increment_user_costs(increments)

    [call] = conn.fetchval.await_args_list
    assert call.args[1:] == (
        ["u1", "u2"],
        ["2026-11-02", "2026-11-02"],
        [7, None],
        [Decimal("0.000300"), Decimal("1.250000")],
        [3, 1],
        [None, None],
        [None, None],
    )


async def test_a_failed_statement_applies_nothing_and_raises():
    """Neither counter is left written: the caller may write the batch again."""
    conn = _Conn()
    conn.fetchval.side_effect = RuntimeError("relation does not exist")

    with pytest.raises(RuntimeError):
        await _store(conn).increment_user_cost("u1", 0.5, day="2026-11-02", api_key_id=7)

    assert conn.committed == 0
    assert conn.rolled_back == 1


async def test_a_lost_commit_answer_that_committed_counts_once(monkeypatch):
    monkeypatch.setattr(postgres_operational, "_COMMIT_STATUS_RETRY_DELAYS", (0.0,))
    conn = _Conn()
    conn.commit_error = ConnectionResetError("connection lost")
    status = AsyncMock()
    status.fetchval = AsyncMock(return_value="committed")

    await _store(conn, status).increment_user_cost("u1", 0.5, day="2026-11-02")

    status.fetchval.assert_awaited_once_with("SELECT txid_status($1)", 12345)


async def test_a_lost_commit_answer_that_aborted_raises_the_failure(monkeypatch):
    monkeypatch.setattr(postgres_operational, "_COMMIT_STATUS_RETRY_DELAYS", (0.0,))
    conn = _Conn()
    conn.commit_error = ConnectionResetError("connection lost")
    status = AsyncMock()
    status.fetchval = AsyncMock(return_value="aborted")

    with pytest.raises(ConnectionResetError):
        await _store(conn, status).increment_user_cost("u1", 0.5, day="2026-11-02")


async def test_an_untold_outcome_is_reported_as_unknown(monkeypatch):
    monkeypatch.setattr(postgres_operational, "_COMMIT_STATUS_RETRY_DELAYS", (0.0, 0.0))
    conn = _Conn()
    conn.commit_error = ConnectionResetError("connection lost")
    status = AsyncMock()
    status.fetchval = AsyncMock(side_effect=[OSError("down"), "in progress"])

    with pytest.raises(CostWriteOutcomeUnknown) as caught:
        await _store(conn, status).increment_user_cost("u1", 0.5, day="2026-11-02")

    assert caught.value.xid == 12345
    assert status.fetchval.await_count == 2


async def test_the_store_declares_atomic_batches():
    assert PostgresOperationalStore(MagicMock()).atomic_cost_increments is True
