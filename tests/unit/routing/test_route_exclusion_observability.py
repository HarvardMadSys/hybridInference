"""A route that selection can never pick must say so, and say why.

Regression for a production RCA: ``deepseek-v4-flash`` had six configured routes
and exactly one live one. One was held at weight 0.0 by a
``provider_weight_overrides`` row, another by a ``disabled_providers`` row, and
neither appeared anywhere the gateway reports -- not in ``/health/deep``, whose
provider map can only describe endpoints traffic has already been sent to, and
not in the boot log. Finding them took a direct query against the operational
store. The two causes have different owners and different remediations, so they
are reported separately rather than as one merged "disabled" flag.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import pytest

from routing.routers import (
    EXCLUSION_CONFIGURED_ZERO,
    EXCLUSION_PROVIDER_DISABLED,
    EXCLUSION_ROUTING_YAML,
    EXCLUSION_WEIGHT_OVERRIDE,
    FixedRouter,
)
from serving.adapters.base import BaseAdapter, ModelConfig

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


def _cfg(model_id: str, provider: str, endpoint_id: str | None = None) -> ModelConfig:
    return ModelConfig(
        id=model_id,
        name=model_id,
        provider=provider,
        base_url=f"http://{provider}.test/v1",
        context_length=8192,
        max_output_length=1024,
        endpoint_id=endpoint_id or f"{provider}:host:443",
    )


class _EchoAdapter(BaseAdapter):
    async def chat_completion(self, messages: list[dict[str, Any]], **params) -> dict[str, Any]:
        return self.format_response(content="ok", model=self.config.id)

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params
    ) -> AsyncGenerator[str, None]:  # pragma: no cover - not exercised here
        yield self.format_stream_chunk(model=self.config.id, content="ok")


class _SnapshotWeightResolver:
    """Weight-override resolver with the sync snapshot shape routing reads."""

    def __init__(self, overrides: dict[str, dict[str, float]]) -> None:
        self.overrides = overrides

    def get_snapshot_for_model(self, model_id: str) -> dict[str, float]:
        return dict(self.overrides.get(model_id, {}))


class _StaticDisabledResolver:
    def __init__(self, disabled: set[str]) -> None:
        self.disabled = disabled

    def is_disabled(self, provider: str) -> bool:
        return provider in self.disabled


def _router_like_production() -> tuple[FixedRouter, dict[str, _EchoAdapter]]:
    """Build the RCA's shape: one live route, one overridden, one disabled."""
    adapters = {
        "live": _EchoAdapter(_cfg("deepseek-v4-flash", "local-8004")),
        "overridden": _EchoAdapter(_cfg("deepseek-v4-flash", "local-8005")),
        "disabled": _EchoAdapter(_cfg("deepseek-v4-flash", "deepseek")),
    }
    router = FixedRouter(
        weight_override_resolver=_SnapshotWeightResolver(
            {"deepseek-v4-flash": {"local-8005:host:443": 0.0}}
        ),
        disabled_provider_resolver=_StaticDisabledResolver({"deepseek"}),
    )
    router.register_route(
        "deepseek-v4-flash",
        [(adapters["live"], 1.0), (adapters["overridden"], 1.0), (adapters["disabled"], 1.0)],
    )
    return router, adapters


@pytest.mark.unit
def test_exclusion_causes_are_reported_separately():
    router, _adapters = _router_like_production()

    exclusions = {fact["endpoint_id"]: fact for fact in router.get_route_exclusions()}

    assert set(exclusions) == {"local-8005:host:443", "deepseek:host:443"}
    # An operator sent to the wrong admin tab is an operator who cannot fix it.
    assert exclusions["local-8005:host:443"]["reasons"] == [EXCLUSION_WEIGHT_OVERRIDE]
    assert exclusions["deepseek:host:443"]["reasons"] == [EXCLUSION_PROVIDER_DISABLED]
    for fact in exclusions.values():
        assert fact["model_id"] == "deepseek-v4-flash"
        assert fact["effective_weight"] == 0.0
        # The configured share is what says this was removed at runtime rather
        # than never having existed.
        assert fact["configured_weight"] == pytest.approx(1 / 3)


@pytest.mark.unit
def test_provider_status_carries_exclusions_for_never_dispatched_endpoints():
    """The endpoints the health snapshot alone can never mention."""
    router, _adapters = _router_like_production()

    status = router.get_provider_status()

    # Nothing has been dispatched, so the health registry is empty; these
    # entries exist only because the exclusions were merged in.
    assert status["local-8005:host:443"]["exclusion_reasons"] == [EXCLUSION_WEIGHT_OVERRIDE]
    assert status["local-8005:host:443"]["excluded_from_models"] == ["deepseek-v4-flash"]
    assert status["deepseek:host:443"]["exclusion_reasons"] == [EXCLUSION_PROVIDER_DISABLED]
    # The live route is not reported as excluded.
    assert "local-8004:host:443" not in status


@pytest.mark.unit
def test_exclusions_merge_onto_an_existing_health_entry():
    router, adapters = _router_like_production()
    router._on_failure("local-8005:host:443", reason="upstream_500")

    entry = router.get_provider_status()["local-8005:host:443"]

    assert entry["circuit_state"] == "closed"
    assert entry["availability"] < 1.0
    assert entry["exclusion_reasons"] == [EXCLUSION_WEIGHT_OVERRIDE]
    assert adapters["overridden"].config.provider == "local-8005"


@pytest.mark.unit
def test_the_registry_never_learns_about_the_exclusions():
    """The merge writes into the snapshot's own dicts, never the registry's.

    ``EndpointHealthRegistry.snapshot()`` builds fresh dicts today, which is the
    only reason the merge is safe. Pinned because the failure mode is silent: a
    snapshot that handed back its internal entries would let a status read
    accumulate ``excluded_from_models`` into the live health state, and nothing
    else in the tree would notice.
    """
    router, _adapters = _router_like_production()
    router._on_failure("local-8005:host:443", reason="upstream_500")

    first = router.get_provider_status()
    first["local-8005:host:443"]["excluded_from_models"].append("not-a-model")

    assert "excluded_from_models" not in router._health_registry.snapshot()["local-8005:host:443"]
    assert router.get_provider_status()["local-8005:host:443"]["excluded_from_models"] == [
        "deepseek-v4-flash"
    ]


@pytest.mark.unit
def test_both_causes_on_one_route_are_both_named():
    adapter = _EchoAdapter(_cfg("m", "zai"))
    other = _EchoAdapter(_cfg("m", "ollama"))
    router = FixedRouter(
        weight_override_resolver=_SnapshotWeightResolver({"m": {"zai:host:443": 0.0}}),
        disabled_provider_resolver=_StaticDisabledResolver({"zai"}),
    )
    router.register_route("m", [(adapter, 1.0), (other, 1.0)])

    (fact,) = router.get_route_exclusions()

    assert fact["reasons"] == [EXCLUSION_WEIGHT_OVERRIDE, EXCLUSION_PROVIDER_DISABLED]


@pytest.mark.unit
def test_a_registry_zero_is_not_blamed_on_the_operational_store():
    adapter = _EchoAdapter(_cfg("m", "ollama"))
    zero = _EchoAdapter(_cfg("m", "chutes"))
    router = FixedRouter()
    router.register_route("m", [(adapter, 1.0), (zero, 0.0)])

    (fact,) = router.get_route_exclusions()

    assert fact["endpoint_id"] == "chutes:host:443"
    assert fact["reasons"] == [EXCLUSION_CONFIGURED_ZERO]


@pytest.mark.unit
def test_aliases_do_not_multiply_the_report():
    """Aliases share a RouteConfig by reference; one route, one set of facts."""
    adapter = _EchoAdapter(_cfg("m", "ollama"))
    zero = _EchoAdapter(_cfg("m", "chutes"))
    router = FixedRouter()
    router.register_route("m", [(adapter, 1.0), (zero, 0.0)], aliases=["m-latest", "m-preview"])

    assert [fact["model_id"] for fact in router.get_route_exclusions()] == ["m"]


@pytest.mark.unit
def test_unpublished_routes_are_not_reported():
    adapter = _EchoAdapter(_cfg("staged", "ollama"))
    zero = _EchoAdapter(_cfg("staged", "chutes"))
    router = FixedRouter()
    router.register_route("staged", [(adapter, 1.0), (zero, 0.0)], published=False)

    assert router.get_route_exclusions() == []


@pytest.mark.unit
@pytest.mark.parametrize("with_resolvers", [False, True])
def test_describe_route_weights_agrees_with_selection(with_resolvers: bool):
    """The report must never disagree with the router about what is routable.

    ``describe_route_weights`` mirrors ``_get_effective_adapters`` rather than
    calling it (the latter can await, and this runs inside a request handler's
    event loop). This is the test that keeps the mirror honest.
    """
    live = _EchoAdapter(_cfg("m", "ollama"))
    overridden = _EchoAdapter(_cfg("m", "zai"))
    disabled = _EchoAdapter(_cfg("m", "chutes"))
    router = FixedRouter(
        weight_override_resolver=(
            _SnapshotWeightResolver({"m": {"zai:host:443": 0.0}}) if with_resolvers else None
        ),
        disabled_provider_resolver=(
            _StaticDisabledResolver({"chutes"} if with_resolvers else set())
        ),
    )
    router.register_route("m", [(live, 3.0), (overridden, 1.0), (disabled, 1.0)])

    effective = router._get_effective_adapters("m", router.routes["m"])
    total = sum(weight for _, weight in effective)
    from_selection = {
        adapter.config.provider: weight / total for adapter, weight in effective if total > 0
    }
    from_report = {
        fact["provider"]: fact["effective_weight"] for fact in router.describe_route_weights()
    }

    assert from_report == pytest.approx(from_selection)


@pytest.mark.unit
def test_the_report_follows_selection_past_a_manager_reweight():
    """The production shape: an operational store *and* a routing.yaml split.

    ``apply()`` rewrites ``route.adapters`` in place and can reorder it, while
    ``_get_effective_adapters`` reads ``raw_adapters`` whenever a resolver is
    attached and so ignores that rewrite entirely. The report has to make the
    same choice; keying its configured shares by adapter rather than by position
    is what stops the reorder from mis-pairing them.
    """
    live = _EchoAdapter(_cfg("m", "local-8004"))
    overridden = _EchoAdapter(_cfg("m", "local-8005"))
    remote = _EchoAdapter(_cfg("m", "zai"))
    router = FixedRouter(
        weight_override_resolver=_SnapshotWeightResolver({"m": {"local-8005:host:443": 0.0}}),
    )
    router.register_route("m", [(live, 1.0), (overridden, 1.0), (remote, 2.0)])
    router.routes["m"].adapters = [(remote, 0.5), (live, 0.5), (overridden, 0.0)]

    effective = router._get_effective_adapters("m", router.routes["m"])
    total = sum(weight for _, weight in effective)
    from_selection = {adapter.config.provider: weight / total for adapter, weight in effective}
    facts = {fact["provider"]: fact for fact in router.describe_route_weights()}

    assert {p: f["effective_weight"] for p, f in facts.items()} == pytest.approx(from_selection)
    # The manager's weights are inert here, so nothing is blamed on routing.yaml.
    assert facts["local-8005"]["reasons"] == [EXCLUSION_WEIGHT_OVERRIDE]
    assert facts["zai"]["reasons"] == []
    assert facts["zai"]["configured_weight"] == pytest.approx(0.5)


@pytest.mark.unit
def test_routing_manager_reweighting_is_attributed_to_routing_yaml():
    """Without a weight resolver, selection reads the weights the manager wrote."""
    local = _EchoAdapter(_cfg("m", "local-8004"))
    remote = _EchoAdapter(_cfg("m", "zai"))
    router = FixedRouter()
    router.register_route("m", [(local, 1.0), (remote, 1.0)])
    # What RoutingManager.apply() does for local_fraction: 1.0 -- it mutates the
    # normalized list in place and leaves raw_adapters (the registry's own
    # weights) untouched.
    router.routes["m"].adapters = [(local, 1.0), (remote, 0.0)]

    (fact,) = router.get_route_exclusions()

    assert fact["provider"] == "zai"
    assert fact["configured_weight"] == pytest.approx(0.5)
    assert fact["reasons"] == [EXCLUSION_ROUTING_YAML]


@pytest.mark.unit
def test_divergence_is_logged_once_and_names_the_mechanism(caplog):
    router, _adapters = _router_like_production()

    with caplog.at_level(logging.INFO, logger="routing.routers"):
        router.log_route_weight_divergence()
        # A reload that changes nothing must not reprint: a weight that has been
        # zero for 71 days would otherwise emit a line every ten seconds.
        router.log_route_weight_divergence()

    records = [r for r in caplog.records if getattr(r, "event", None) == "route_weight_zeroed"]
    assert len(records) == 2, "one line per zeroed route, printed once"
    assert {r.endpoint_id for r in records} == {"local-8005:host:443", "deepseek:host:443"}
    assert all(r.levelno == logging.WARNING for r in records)
    by_endpoint = {r.endpoint_id: r for r in records}
    assert by_endpoint["local-8005:host:443"].reason == EXCLUSION_WEIGHT_OVERRIDE
    assert by_endpoint["deepseek:host:443"].reason == EXCLUSION_PROVIDER_DISABLED
    assert by_endpoint["deepseek:host:443"].effective_weight == 0.0
    assert by_endpoint["deepseek:host:443"].configured_weight == pytest.approx(1 / 3)
    # The rendered line must not name a mechanism in its verb: "overridden" is
    # one of the four causes, so a provider-disabled route announced that way
    # sends the reader to the wrong admin tab. The verb says what happened to
    # the weight; the bracketed reason says who did it.
    message = by_endpoint["deepseek:host:443"].getMessage()
    assert "zeroed at runtime" in message
    assert f"[{EXCLUSION_PROVIDER_DISABLED}]" in message


@pytest.mark.unit
def test_divergence_reprints_when_the_override_set_changes(caplog):
    router, _adapters = _router_like_production()
    resolver = router.weight_override_resolver

    with caplog.at_level(logging.INFO, logger="routing.routers"):
        router.log_route_weight_divergence()
        caplog.clear()
        resolver.overrides["deepseek-v4-flash"] = {}
        router.log_route_weight_divergence()

    # The weight override is gone; the disabled provider is not.
    events = [getattr(r, "event", None) for r in caplog.records]
    assert events == ["route_weight_zeroed"]
    assert caplog.records[0].endpoint_id == "deepseek:host:443"


@pytest.mark.unit
def test_a_gateway_without_overrides_boots_silently(caplog):
    adapter = _EchoAdapter(_cfg("m", "ollama"))
    router = FixedRouter()
    router.register_route("m", [(adapter, 1.0)])

    with caplog.at_level(logging.INFO, logger="routing.routers"):
        router.log_route_weight_divergence()

    assert caplog.records == []


@pytest.mark.unit
def test_a_reweighted_but_still_routable_route_is_not_a_warning(caplog):
    live = _EchoAdapter(_cfg("m", "ollama"))
    quieted = _EchoAdapter(_cfg("m", "zai"))
    router = FixedRouter(
        weight_override_resolver=_SnapshotWeightResolver({"m": {"zai:host:443": 0.1}}),
    )
    router.register_route("m", [(live, 1.0), (quieted, 1.0)])

    with caplog.at_level(logging.INFO, logger="routing.routers"):
        router.log_route_weight_divergence()

    # One line, for the route that was actually re-weighted: the sibling whose
    # share rose as a consequence is not a second operator action.
    assert [r.levelno for r in caplog.records] == [logging.INFO]
    assert caplog.records[0].event == "route_weight_overridden"
    assert caplog.records[0].provider == "zai"
    assert router.get_route_exclusions() == []
