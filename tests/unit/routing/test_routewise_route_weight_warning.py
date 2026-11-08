"""RouteWise says so when a model's route weights would be taken as a split.

RouteWise does not split traffic by weight: every route weighted above 0 is a
full candidate. The admin switch to RouteWise refuses a model whose routes are
weighted unequally; a weighting that reaches a RouteWise model another way --
``models.yaml``, a weight override, an added route -- is logged instead.
"""

from __future__ import annotations

import logging

import pytest

from routing.routewise.config import RouteWiseConfig
from routing.routewise.router import RouteWiseRouter
from tests.unit.routing.test_routewise_router import _FakeRouteTable, _make_adapter


def _warnings(caplog) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "routewise_route_weights_ignored"
    ]


@pytest.mark.unit
def test_a_canary_weight_is_logged_once_per_weighting(caplog):
    prod = _make_adapter(endpoint_id="m:prod")
    staging = _make_adapter(endpoint_id="m:staging")
    table = _FakeRouteTable()
    table.add("m", [(prod, 1.0), (staging, 0.01)])

    with caplog.at_level(logging.WARNING):
        router = RouteWiseRouter(route_table=table, config=RouteWiseConfig())
        router.refresh_route_table()
    [record] = _warnings(caplog)
    assert "m:staging=0.01" in record.getMessage()
    assert "m:prod" not in record.getMessage()

    # A new weighting is a new warning.
    table.weight_overrides["m:staging"] = 0.5
    with caplog.at_level(logging.WARNING):
        router.refresh_route_table()
    assert len(_warnings(caplog)) == 2


@pytest.mark.unit
def test_equal_weights_and_a_zeroed_route_are_not_logged(caplog):
    a = _make_adapter(endpoint_id="m:a")
    b = _make_adapter(endpoint_id="m:b")
    staging = _make_adapter(endpoint_id="m:staging")
    table = _FakeRouteTable()
    table.add("m", [(a, 1.0), (b, 1.0), (staging, 0.0)])

    with caplog.at_level(logging.WARNING):
        RouteWiseRouter(route_table=table, config=RouteWiseConfig())

    assert _warnings(caplog) == []
