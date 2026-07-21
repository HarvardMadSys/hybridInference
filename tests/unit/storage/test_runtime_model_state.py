"""Tests for atomically deleting persisted runtime-model state."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from serving.storage.cache import CachedOperationalStore, InMemoryCache
from serving.storage.postgres_operational import PostgresOperationalStore


class _RecordingTransaction:
    def __init__(self, connection: _RecordingConnection) -> None:
        self._connection = connection

    async def __aenter__(self) -> None:
        assert not self._connection.transaction_active
        self._connection.transaction_active = True
        self._connection.transaction_count += 1

    async def __aexit__(self, exc_type, _exc, _traceback) -> bool:
        self._connection.transaction_active = False
        self._connection.transaction_exception = exc_type
        return False


class _RecordingConnection:
    def __init__(
        self,
        *,
        candidate_tag: str = "DELETE 1",
        fail_on_table: str | None = None,
    ) -> None:
        self.candidate_tag = candidate_tag
        self.fail_on_table = fail_on_table
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.transaction_active = False
        self.transaction_count = 0
        self.transaction_exception: type[BaseException] | None = None

    def transaction(self) -> _RecordingTransaction:
        return _RecordingTransaction(self)

    async def execute(self, sql: str, *params: object) -> str:
        assert self.transaction_active
        self.calls.append((sql, params))
        if self.fail_on_table is not None and self.fail_on_table in sql:
            raise RuntimeError("injected delete failure")
        if "provider_route_candidates" in sql:
            return self.candidate_tag
        return "DELETE 1"


class _RecordingPool:
    def __init__(self, connection: _RecordingConnection) -> None:
        self._connection = connection

    @asynccontextmanager
    async def acquire(self):
        yield self._connection


async def test_postgres_deletes_all_runtime_model_state_in_one_transaction():
    connection = _RecordingConnection(candidate_tag="DELETE 2")
    store = PostgresOperationalStore(_RecordingPool(connection))
    setting_keys = (
        "model_required_role:runtime-model",
        "model_router_strategy:runtime-model",
    )

    deleted = await store.delete_runtime_model_state("runtime-model", setting_keys)

    assert deleted is True
    assert connection.transaction_count == 1
    assert connection.transaction_exception is None
    assert [
        table
        for table in (
            "provider_route_candidates",
            "provider_route_configs",
            "model_visibility_overrides",
            "model_concurrency_exemptions",
            "provider_weight_overrides",
            "site_settings",
        )
        if any(table in sql for sql, _params in connection.calls)
    ] == [
        "provider_route_candidates",
        "provider_route_configs",
        "model_visibility_overrides",
        "model_concurrency_exemptions",
        "provider_weight_overrides",
        "site_settings",
    ]
    assert len(connection.calls) == 6
    assert all(params == ("runtime-model",) for _sql, params in connection.calls[:-1])
    assert connection.calls[-1][1] == (list(setting_keys),)
    assert "ANY($1::text[])" in connection.calls[-1][0]


async def test_postgres_returns_false_when_runtime_candidate_did_not_exist():
    connection = _RecordingConnection(candidate_tag="DELETE 0")
    store = PostgresOperationalStore(_RecordingPool(connection))

    deleted = await store.delete_runtime_model_state("missing-model", ())

    assert deleted is False
    assert len(connection.calls) == 6
    assert connection.calls[-1][1] == ([],)


async def test_postgres_failure_exits_transaction_with_error():
    connection = _RecordingConnection(fail_on_table="model_concurrency_exemptions")
    store = PostgresOperationalStore(_RecordingPool(connection))

    with pytest.raises(RuntimeError, match="injected delete failure"):
        await store.delete_runtime_model_state(
            "runtime-model",
            ("model_required_role:runtime-model", "model_router_strategy:runtime-model"),
        )

    assert connection.transaction_active is False
    assert connection.transaction_exception is RuntimeError
    assert not any("provider_weight_overrides" in sql for sql, _params in connection.calls)


async def test_cached_store_delegates_runtime_model_state_delete():
    inner = AsyncMock()
    inner.delete_runtime_model_state.return_value = True
    store = CachedOperationalStore(inner, InMemoryCache())
    setting_keys = [
        "model_required_role:runtime-model",
        "model_router_strategy:runtime-model",
    ]

    deleted = await store.delete_runtime_model_state("runtime-model", setting_keys)

    assert deleted is True
    inner.delete_runtime_model_state.assert_awaited_once_with("runtime-model", setting_keys)
