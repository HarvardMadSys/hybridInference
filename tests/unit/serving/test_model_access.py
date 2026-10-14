"""The one access check behind every surface that serves or lists models.

Before it existed the same rules were written out six times across the two
inference handlers, the three model listings and the agent catalog. The copies
already disagreed on one point, which id to check, and the handler-level tests
in ``tests/servers/test_model_access_aliases.py`` hold the surfaces to it. These
tests pin the check itself: its reasons, their order, and the id each reads.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from serving.model_access import (
    ModelAccessDenial,
    baseline_required_role,
    model_access_denial,
    route_canonical_model_id,
)

CANONICAL = "canonical-model"


def _route(*, published: bool = True, required_role: str = "free", admin_only: bool = False):
    adapter = SimpleNamespace(config=SimpleNamespace(id=CANONICAL))
    return SimpleNamespace(
        adapters=[(adapter, 1.0)],
        published=published,
        required_role=required_role,
        admin_only=admin_only,
    )


class _RecordingResolver:
    """A visibility resolver that remembers which ids it was asked about."""

    def __init__(self, overrides: dict[str, str]):
        self._overrides = overrides
        self.asked: list[str] = []

    async def get_effective_required_role(self, model_id: str, default_role: str) -> str:
        self.asked.append(model_id)
        return self._overrides.get(model_id, default_role)


def _ctx(**extra) -> dict:
    return {"user_id": "user-1", "role": "pro", **extra}


@pytest.mark.asyncio
async def test_an_open_model_is_allowed():
    assert await model_access_denial(_route(), CANONICAL, _ctx()) is None


@pytest.mark.asyncio
async def test_each_reason_is_reported():
    assert (
        await model_access_denial(_route(published=False), CANONICAL, _ctx())
        is ModelAccessDenial.UNPUBLISHED
    )
    assert (
        await model_access_denial(_route(required_role="admin"), CANONICAL, _ctx())
        is ModelAccessDenial.ROLE
    )
    assert (
        await model_access_denial(_route(), CANONICAL, _ctx(disabled_models=[CANONICAL]))
        is ModelAccessDenial.DISABLED
    )
    assert (
        await model_access_denial(_route(), CANONICAL, _ctx(agent_allowed_models=["other"]))
        is ModelAccessDenial.OUTSIDE_GRANT
    )


@pytest.mark.asyncio
async def test_an_unpublished_route_is_refused_before_anything_is_read():
    """A staged route is refused even to an admin, without a visibility lookup."""
    resolver = _RecordingResolver({})

    denial = await model_access_denial(
        _route(published=False), CANONICAL, _ctx(role="admin"), visibility_resolver=resolver
    )

    assert denial is ModelAccessDenial.UNPUBLISHED
    assert resolver.asked == []


@pytest.mark.asyncio
async def test_every_check_reads_the_canonical_id_for_an_alias():
    """Overrides, denylists and grants are all keyed by canonical id."""
    resolver = _RecordingResolver({CANONICAL: "admin"})

    denial = await model_access_denial(
        _route(), "model-alias", _ctx(), visibility_resolver=resolver
    )

    assert denial is ModelAccessDenial.ROLE
    assert resolver.asked == [CANONICAL]
    assert (
        await model_access_denial(_route(), "model-alias", _ctx(disabled_models=[CANONICAL]))
        is ModelAccessDenial.DISABLED
    )
    assert (
        await model_access_denial(_route(), "model-alias", _ctx(agent_allowed_models=[CANONICAL]))
        is None
    )


@pytest.mark.asyncio
async def test_the_override_can_open_a_model_as_well_as_close_it():
    resolver = _RecordingResolver({CANONICAL: "free"})

    denial = await model_access_denial(
        _route(required_role="internal"), CANONICAL, _ctx(), visibility_resolver=resolver
    )

    assert denial is None


@pytest.mark.parametrize("user_ctx", [None, {}, {"role": None}, {"role": ""}])
@pytest.mark.asyncio
async def test_a_caller_without_a_role_is_free(user_ctx):
    assert await model_access_denial(_route(), CANONICAL, user_ctx) is None
    assert (
        await model_access_denial(_route(required_role="pro"), CANONICAL, user_ctx)
        is ModelAccessDenial.ROLE
    )


@pytest.mark.asyncio
async def test_grant_scope_can_be_left_out():
    """The listings and the agent catalog have never applied a grant's list."""
    ctx = _ctx(agent_allowed_models=[])

    assert await model_access_denial(_route(), CANONICAL, ctx) is ModelAccessDenial.OUTSIDE_GRANT
    assert await model_access_denial(_route(), CANONICAL, ctx, grant_scope=False) is None


def test_a_route_without_adapters_falls_back_to_the_name_it_was_found_by():
    route = SimpleNamespace(adapters=[])

    assert route_canonical_model_id(route, "looked-up") == "looked-up"
    assert route_canonical_model_id(_route(), "model-alias") == CANONICAL


def test_baseline_required_role():
    assert baseline_required_role(_route(required_role="pro")) == "pro"
    assert baseline_required_role(_route(required_role="", admin_only=True)) == "admin"
    assert baseline_required_role(_route(required_role="")) == "free"
