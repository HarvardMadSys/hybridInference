"""A running RouteWise router refreshes the quota a later route edit gives it.

``start`` scheduled the quota refresh loop only for the quota sources the
route table held at that moment. A quota route added afterwards -- an admin
edit, or a config restore re-applying persisted routes once the router was
running -- never got a provider snapshot, and an unready quota pool is
skipped: its traffic went to paid legs until a restart.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from routing.routewise.config import RouteWiseConfig
from routing.routewise.router import RouteWiseRouter
from tests.unit.routing.test_routewise_router import _FakeRouteTable, _make_adapter


def _router() -> tuple[RouteWiseRouter, _FakeRouteTable, object, object]:
    api = _make_adapter(endpoint_id="test-model:api")
    quota = _make_adapter(provider_type="quota", endpoint_id="test-model:quota")
    table = _FakeRouteTable()
    table.add("test-model", [(api, 1.0)])
    router = RouteWiseRouter(route_table=table, config=RouteWiseConfig())
    router.refresh_quota_snapshots_once = AsyncMock()
    return router, table, api, quota


@pytest.mark.unit
async def test_a_quota_route_added_to_a_running_router_is_refreshed():
    router, table, api, quota = _router()
    await router.start()
    assert router._quota_refresh_task is None

    table.add("test-model", [(api, 1.0), (quota, 1.0)])
    router.refresh_route_table()

    task = router._quota_refresh_task
    assert task is not None and not task.done()
    # Refreshing again does not start a second loop.
    router.refresh_route_table()
    assert router._quota_refresh_task is task
    await router.stop()
    assert router._quota_refresh_task is None


@pytest.mark.unit
async def test_a_router_that_is_not_running_leaves_the_loop_to_start():
    router, table, api, quota = _router()

    table.add("test-model", [(api, 1.0), (quota, 1.0)])
    router.refresh_route_table()
    assert router._quota_refresh_task is None

    await router.start()
    assert router._quota_refresh_task is not None
    await router.stop()


@pytest.mark.unit
async def test_a_stopped_router_starts_no_loop():
    router, table, api, quota = _router()
    await router.start()
    await router.stop()

    table.add("test-model", [(api, 1.0), (quota, 1.0)])
    router.refresh_route_table()

    assert router._quota_refresh_task is None
