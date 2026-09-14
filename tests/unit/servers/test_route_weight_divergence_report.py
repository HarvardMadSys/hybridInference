"""The boot log and the override refresh loops must name zeroed routes.

Discovering that ``deepseek-v4-flash`` was down to one live route took a direct
query against the operational store, because the only places that knew -- the
two resolver refresh loops and bootstrap itself -- said nothing when they
applied an override. These tests cover the wiring; the report's own content is
covered by ``tests/unit/routing/test_route_exclusion_observability.py``.
"""

from __future__ import annotations

import asyncio

import pytest

from serving.servers.bootstrap import (
    _refresh_disabled_provider_snapshots,
    _refresh_weight_override_snapshots,
    _report_route_weight_divergence,
)


class _Reporter:
    def __init__(self, raises: bool = False) -> None:
        self.calls = 0
        self.raises = raises

    def log_route_weight_divergence(self) -> None:
        self.calls += 1
        if self.raises:
            raise RuntimeError("resolver exploded mid-report")


class _Resolver:
    """Resolver whose ``load_all`` reports a change on the first reload only."""

    def __init__(self, changes: list[bool]) -> None:
        self.changes = list(changes)

    async def load_all(self) -> bool:
        return self.changes.pop(0) if self.changes else False


@pytest.mark.unit
def test_a_router_without_the_report_is_a_no_op():
    """RouteWise and any bare stub: nothing to report, nothing to crash on."""
    _report_route_weight_divergence(None)
    _report_route_weight_divergence(object())


@pytest.mark.unit
def test_a_failing_report_never_reaches_the_caller():
    """Losing route weights over a logging fault is worse than losing the line."""
    reporter = _Reporter(raises=True)

    _report_route_weight_divergence(reporter)

    assert reporter.calls == 1


@pytest.mark.unit
@pytest.mark.parametrize(
    "refresh",
    [_refresh_weight_override_snapshots, _refresh_disabled_provider_snapshots],
)
async def test_the_refresh_loop_reports_only_when_the_snapshot_changed(refresh):
    reporter = _Reporter()
    resolver = _Resolver([True, False, False])

    task = asyncio.create_task(refresh(resolver, None, interval_seconds=0.001, router=reporter))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Many reloads ran; only the one that changed something asked for a report.
    assert reporter.calls == 1
    assert resolver.changes == []
