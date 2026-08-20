"""The agent model predicate must mirror the inference path's checks.

These pin the properties that made staging offer 15 models of which 13 died at
the first call: visibility is computed for the *owner's* role (the identity the
sandbox's calls now run as), honours the visibility resolver's runtime
override, excludes what the inference path would 404, and never lists an
embedding model an agent cannot chat with.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from serving.model_catalog import (
    agent_model_reasoning_efforts,
    agent_model_resolvable,
    agent_visible_models,
)

pytestmark = pytest.mark.asyncio


def _route(
    canonical: str,
    *,
    published: bool = True,
    required_role: str | None = None,
    admin_only: bool = False,
    model_type: str = "chat",
):
    adapter = SimpleNamespace(config=SimpleNamespace(id=canonical, model_type=model_type))
    return SimpleNamespace(
        published=published,
        required_role=required_role,
        admin_only=admin_only,
        adapters=[(adapter, 1.0)],
    )


def _exec(routes: dict) -> SimpleNamespace:
    return SimpleNamespace(routes=routes)


async def test_role_gates_match_the_inference_path():
    """A model the owner's role cannot call is not offered, and vice versa."""
    router_exec = _exec(
        {
            "open-model": _route("open-model"),
            "internal-model": _route("internal-model", required_role="internal"),
            "admin-model": _route("admin-model", admin_only=True),
        }
    )
    free = await agent_visible_models(router_exec, user_ctx={"role": "free"})
    internal = await agent_visible_models(router_exec, user_ctx={"role": "internal"})
    admin = await agent_visible_models(router_exec, user_ctx={"role": "admin"})

    assert free == ["open-model"]
    assert internal == ["open-model", "internal-model"]
    assert admin == ["open-model", "internal-model", "admin-model"]


async def test_aliases_resolve_but_are_not_listed_twice():
    """The picker shows canonical ids; the validator accepts aliases too."""
    shared = _route("canonical-model")
    router_exec = _exec({"canonical-model": shared, "alias-model": shared})

    assert await agent_visible_models(router_exec, user_ctx={"role": "free"}) == ["canonical-model"]
    assert await agent_model_resolvable("alias-model", router_exec, user_ctx={"role": "free"})
    assert not await agent_model_resolvable("nope", router_exec, user_ctx={"role": "free"})


async def test_unpublished_disabled_and_embedding_models_are_excluded():
    """Everything the first model call would refuse is refused here first."""
    router_exec = _exec(
        {
            "hidden": _route("hidden", published=False),
            "embedder": _route("embedder", model_type="embedding"),
            "banned": _route("banned"),
            "usable": _route("usable"),
        }
    )
    user_ctx = {"role": "internal", "disabled_models": ["banned"]}

    assert await agent_visible_models(router_exec, user_ctx=user_ctx) == ["usable"]
    for model in ("hidden", "embedder", "banned"):
        assert not await agent_model_resolvable(model, router_exec, user_ctx=user_ctx)


async def test_visibility_resolver_override_wins_both_ways():
    """The runtime override must gate agents exactly as it gates completions."""

    class Resolver:
        async def get_effective_required_role(self, canonical: str, required: str) -> str:
            return {"tightened": "admin", "loosened": "free"}.get(canonical, required)

    router_exec = _exec(
        {
            "tightened": _route("tightened"),
            "loosened": _route("loosened", required_role="admin"),
        }
    )
    visible = await agent_visible_models(
        router_exec, visibility_resolver=Resolver(), user_ctx={"role": "internal"}
    )
    assert visible == ["loosened"]


def _effort_route(
    canonical: str,
    *,
    supported_params: list[str] | None = None,
    reasoning_efforts: list[str] | None = None,
):
    adapter = SimpleNamespace(
        config=SimpleNamespace(
            id=canonical,
            model_type="chat",
            supported_params=supported_params or ["max_tokens"],
            reasoning_efforts=reasoning_efforts or [],
        )
    )
    return SimpleNamespace(
        published=True, required_role=None, admin_only=False, adapters=[(adapter, 1.0)]
    )


async def test_reasoning_effort_domains_are_reported_per_model():
    """Only models that both support the parameter and declare values appear."""
    router_exec = _exec(
        {
            "declares": _effort_route(
                "declares",
                supported_params=["max_tokens", "reasoning_effort"],
                reasoning_efforts=["low", "high", "max"],
            ),
            # Supports the parameter but nobody recorded its accepted values.
            # Absent, not empty-listed: a guessed domain is the 400 factory this
            # field exists to prevent, and "no knob" is the honest answer.
            "undeclared": _effort_route("undeclared", supported_params=["reasoning_effort"]),
            "plain": _effort_route("plain"),
        }
    )
    models = await agent_visible_models(router_exec, user_ctx={"role": "free"})

    efforts = agent_model_reasoning_efforts(router_exec, models)

    assert efforts == {"declares": ["low", "high", "max"]}


async def test_reasoning_effort_domains_ignore_unknown_models():
    """A caller's stale model list must not raise, just describe nothing."""
    assert agent_model_reasoning_efforts(_exec({}), ["gone"]) == {}
