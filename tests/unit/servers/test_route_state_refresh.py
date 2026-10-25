"""The route-state refresh loop finishes what a failed boot-time read left undone."""

from __future__ import annotations

import asyncio

import pytest

from serving.servers.bootstrap import _refresh_route_state_snapshots


class _FlakyStates:
    """Route-state resolver whose first reads fail, as a database blip would."""

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.loads = 0

    async def load_all(self) -> bool:
        self.loads += 1
        if self.loads <= self.failures:
            raise RuntimeError("database unavailable")
        return self.loads == self.failures + 1


async def _run_loop(resolver, apply_removals) -> None:
    task = asyncio.create_task(
        _refresh_route_state_snapshots(
            resolver,
            None,
            interval_seconds=0.001,
            apply_removals=apply_removals,
        )
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.unit
async def test_removals_run_once_after_the_first_read_that_succeeds():
    resolver = _FlakyStates(failures=2)
    applied: list[int] = []

    async def apply_removals() -> None:
        applied.append(resolver.loads)

    await _run_loop(resolver, apply_removals)

    # Not on a guessed (empty) snapshot, and not again on later reloads.
    assert applied == [3]
    assert resolver.loads > 3


@pytest.mark.unit
async def test_a_failed_removal_pass_is_retried_on_the_next_reload():
    resolver = _FlakyStates(failures=0)
    attempts: list[int] = []

    async def apply_removals() -> None:
        attempts.append(resolver.loads)
        if len(attempts) == 1:
            raise RuntimeError("route table busy")

    await _run_loop(resolver, apply_removals)

    assert attempts == [1, 2]
