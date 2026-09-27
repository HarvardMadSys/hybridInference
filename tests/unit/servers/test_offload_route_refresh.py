"""The offload-route refresh loop keeps every worker converging on the stored policies."""

from __future__ import annotations

import asyncio

import pytest

from serving.servers.bootstrap import _refresh_offload_route_snapshots


class _FlakyResolver:
    """Resolver whose first reload fails, as a database blip would."""

    def __init__(self) -> None:
        self.loads = 0

    async def load_all(self) -> bool:
        self.loads += 1
        if self.loads == 1:
            raise RuntimeError("database unavailable")
        return False


@pytest.mark.unit
async def test_the_refresh_loop_survives_a_failed_reload_and_keeps_polling():
    resolver = _FlakyResolver()

    task = asyncio.create_task(_refresh_offload_route_snapshots(resolver, interval_seconds=0.001))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # A policy stored while the first read failed is still picked up by a later one.
    assert resolver.loads > 1
