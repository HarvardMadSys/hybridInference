"""Tests for synchronizing cached strategies with effective route state."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from serving.servers.routewise_rebuild import rebuild_cached_routewise_routers


class _RecordingLock:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.depth = 0

    def __enter__(self) -> _RecordingLock:
        self._lock.acquire()
        self.depth += 1
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.depth -= 1
        self._lock.release()


@pytest.mark.unit
def test_rebuild_cached_routers_deduplicates_aliases_and_prefers_route_table_hook():
    calls: list[str] = []
    commit_lock = _RecordingLock()

    class _Strategy:
        _route_commit_lock = commit_lock

        def _rebuild_from_route_table(self) -> None:
            assert commit_lock.depth == 1
            calls.append("route-table")

        def _rebuild_from_fixed_router(self) -> None:
            calls.append("legacy")

    strategy = _Strategy()
    registry = SimpleNamespace(cached_routers=lambda: [strategy, strategy])

    rebuild_cached_routewise_routers(registry)

    assert calls == ["route-table"]
    assert commit_lock.depth == 0
