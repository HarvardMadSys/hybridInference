"""``api_logs.batch_job_id``: attribution of a row to the batch that issued it.

Both insert paths (``PostgresLogStore`` and the boot-time ``DatabaseLogger``)
write ``metadata["batch_job_id"]`` into its own column, exactly as they write
``agent_job_id``. Unlike ``agent_job_id`` the column has no index: nothing reads
``api_logs`` by batch.

``conn.execute`` is called as ``(sql, $1, $2, ...)``, so placeholder ``$N`` is
``args[N]``: ``agent_job_id`` is ``$36`` and ``batch_job_id`` ``$37``. Native
accounting appends two more parameters, so batch attribution is no longer last:
``cost_amount`` is ``$38`` and ``accounting_currency`` ``$39``. Under the
USD policy ``cost_usd`` is the computed float itself and the native amount is
left to the trigger (``None``); the currency tag is ``"USD"``.

The SQL is compared after whitespace normalization because the column list is
split across source lines; the raw string contains a newline between
``batch_job_id`` and ``cost_amount``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from serving.accounting import (
    AccountingPolicy,
    bind_accounting_policy,
    get_accounting_policy,
)
from serving.storage.database import DatabaseLogger
from serving.storage.postgres_log import PostgresLogStore

AGENT_JOB_ARG = 36
BATCH_JOB_ARG = 37
COST_USD_ARG = 17


def _native_positions(make_writer: object) -> tuple[str, str, int, int]:
    """Where each writer appends the native amount and currency, after its own columns.

    ``(sql, $1, ...)``; the SQL text is args[0]. The log store writes the key
    and project attribution (``$38``, ``$39``) before them; the database
    logger does not.
    """
    if make_writer is _postgres_log_store:
        return "key_id, project_id, ", "$40, $41", 40, 41
    return "", "$38, $39", 38, 39


@pytest.fixture(autouse=True)
def _usd_accounting_policy():
    """Bind the identity USD policy for these tests and restore the prior one.

    The insert arguments are asserted against the identity-rate expectation, so
    the module must not inherit a CNY policy (or any other) left bound by an
    earlier test; restoring it afterwards keeps this file from changing them too.
    """
    previous = get_accounting_policy()
    bind_accounting_policy(AccountingPolicy())
    try:
        yield
    finally:
        bind_accounting_policy(previous)


def _capturing_pool() -> tuple[MagicMock, MagicMock]:
    conn = MagicMock()
    conn.execute = AsyncMock(return_value="INSERT 0 1")
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire.return_value = acquire
    return pool, conn


def _postgres_log_store() -> tuple[object, MagicMock]:
    pool, conn = _capturing_pool()
    return PostgresLogStore(pool, store_full_prompts=False), conn


def _database_logger() -> tuple[object, MagicMock]:
    pool, conn = _capturing_pool()
    logger = DatabaseLogger({}, store_full_prompts=False)
    logger.pool = pool
    return logger, conn


async def _log(writer: object, metadata: dict[str, str]) -> None:
    await writer.log_request(  # type: ignore[attr-defined]
        request_id="req-1",
        model_id="glm-5.1",
        provider="zhipu",
        prompt=[{"role": "user", "content": "hi"}],
        response={"message": "ok"},
        usage={"prompt_tokens": 1, "completion_tokens": 1},
        latency_ms=10,
        status_code=200,
        metadata=metadata,
        # Priced traffic, so the appended native amount is a real value rather
        # than the ``None`` an unpriced request produces.
        pricing={"prompt": "1.0", "completion": "1.0"},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("make_writer", [_postgres_log_store, _database_logger])
async def test_a_batch_rows_job_lands_in_its_own_column(make_writer):
    writer, conn = make_writer()

    await _log(writer, {"user_id": "u-1", "agent_job_id": "job-1", "batch_job_id": "batch_1"})

    sql, *_ = args = conn.execute.await_args.args
    normalized_sql = " ".join(sql.split())
    columns, values, amount_arg, currency_arg = _native_positions(make_writer)
    assert f"agent_job_id, batch_job_id, {columns}cost_amount, accounting_currency" in (
        normalized_sql
    )
    assert values in normalized_sql
    assert len(args) == currency_arg + 1
    assert args[AGENT_JOB_ARG] == "job-1"
    assert args[BATCH_JOB_ARG] == "batch_1"
    # Native accounting follows batch attribution. A USD ledger writes the
    # computed float as ``cost_usd``, exactly as before, and leaves
    # ``cost_amount`` to the trigger; the row is tagged USD.
    assert args[amount_arg] is None
    assert type(args[COST_USD_ARG]) is float and args[COST_USD_ARG] == pytest.approx(2e-6)
    assert args[currency_arg] == "USD"


@pytest.mark.asyncio
@pytest.mark.parametrize("make_writer", [_postgres_log_store, _database_logger])
async def test_a_usd_cost_is_written_as_the_float_it_was_computed_as(make_writer):
    """``1.5e-8`` is just under half of 1e-8 in binary: ``DECIMAL(12, 8)`` keeps it at 1e-8.

    Through ``Decimal(str(...))`` it would become exactly half and round up to
    2e-8, changing the logged cost of a USD deployment.
    """
    from serving.storage.utils import calculate_cost

    writer, conn = make_writer()
    usage = {"prompt_tokens": 1, "completion_tokens": 0}
    pricing = {"prompt": "0.015", "completion": "0"}
    await writer.log_request(  # type: ignore[attr-defined]
        request_id="req-half",
        model_id="m",
        provider="p",
        prompt=[{"role": "user", "content": "hi"}],
        response={"message": "ok"},
        usage=usage,
        latency_ms=10,
        status_code=200,
        metadata={"user_id": "u-1"},
        pricing=pricing,
    )
    args = conn.execute.await_args.args
    _columns, _values, amount_arg, _currency_arg = _native_positions(make_writer)
    expected = calculate_cost(usage, pricing)
    assert expected == 1.5e-8
    assert type(args[COST_USD_ARG]) is float and args[COST_USD_ARG] == expected
    assert args[amount_arg] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("make_writer", [_postgres_log_store, _database_logger])
async def test_ordinary_traffic_leaves_the_column_null(make_writer):
    writer, conn = make_writer()

    await _log(writer, {"user_id": "u-1"})

    _sql, *_ = args = conn.execute.await_args.args
    _columns, _values, amount_arg, currency_arg = _native_positions(make_writer)
    assert len(args) == currency_arg + 1
    assert args[BATCH_JOB_ARG] is None
    # Ordinary traffic is tagged too; on USD the trigger fills ``cost_amount``.
    assert args[amount_arg] is None
    assert args[COST_USD_ARG] is not None
    assert args[currency_arg] == "USD"
