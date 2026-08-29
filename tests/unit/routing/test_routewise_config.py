"""Tests for RouteWise configuration defaults and validation."""

from __future__ import annotations

import re
import time
from dataclasses import fields as dataclass_fields
from pathlib import Path

import pytest
import yaml

from routing.routers import FixedRouter
from routing.routewise.candidates import (
    ConcurrencyPolicy,
    QuotaPolicy,
    QuotaSource,
    build_provider_candidates,
)
from routing.routewise.config import RouteWiseConfig
from routing.routewise.quota import ProviderQuotaSnapshotStore
from routing.routewise.router import RouteWiseRouter
from routing.strategies.routewise import RouteWiseParams
from serving.servers.registry import register_from_models_yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.unit
class TestRouteWiseConfigDefaults:
    """Verify all default values are sane."""

    def test_default_config(self):
        cfg = RouteWiseConfig()
        assert cfg.random_seed is None
        assert cfg.reference_api_price is None
        assert cfg.db_bootstrap_enabled is True
        assert cfg.db_bootstrap_max_rows == 50_000
        assert cfg.stateful_providers_single_worker_only is True
        assert cfg.quota_snapshot_refresh_interval_sec == 60.0
        assert cfg.envelope_window_hours == 24
        assert cfg.envelope_lower_percentile == 10.0
        assert cfg.envelope_upper_percentile == 90.0
        # Latency-layer defaults
        assert cfg.latency_slo_sec == 3.0
        assert cfg.latency_window_sec == 900.0
        assert cfg.latency_max_samples_per_profile == 5000
        assert cfg.latency_min_samples == 10
        assert cfg.latency_hedge_mode == "disabled"

    def test_invalid_latency_hedge_mode_rejected(self):
        with pytest.raises(ValueError, match="Unsupported latency_hedge_mode"):
            RouteWiseConfig(latency_hedge_mode="economic")

    def test_resource_fields_are_gone(self):
        """Resource limits are route-level config, not RouteWiseConfig fields."""
        cfg = RouteWiseConfig()
        for moved in (
            "daily_quota",
            "quota_monthly_fee",
            "reset_timezone",
            "concurrency_enabled",
            "concurrency_limit",
            "concurrency_monthly_fee",
        ):
            assert not hasattr(cfg, moved)


_EXAMPLE = Path(__file__).resolve().parents[3] / "config" / "examples" / "models.routewise.yaml"


def _example_model() -> dict:
    models = yaml.safe_load(_EXAMPLE.read_text())["models"]
    assert len(models) == 1, "the example is meant to stay a single-model teaching file"
    return models[0]


def _commented_reference_routes() -> list[dict]:
    """Uncomment the `route:` entries the example ships as documentation.

    They are the only record of what a quota or concurrency provider looks
    like. Commented out they are invisible to every loader, so this pulls them
    back into structured form and the tests below run them through the real
    parsers -- the previous string-search guard happily passed a `quota_source`
    block that no schema would accept.
    """
    lines = _EXAMPLE.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == "route:")
    body: list[str] = []
    for line in lines[start + 1 :]:
        match = re.match(r"^      # ?(.*)$", line)
        if not match:
            continue
        entry = match.group(1)
        # Route YAML, not the prose around it: a list item, or any indented
        # continuation of one (nested blocks such as `quota_source:` indent
        # further). Prose lines sit flush against the comment marker.
        if entry.startswith("- kind:") or (entry.startswith("  ") and entry.strip()):
            body.append(entry)
    routes = yaml.safe_load("\n".join(body))
    assert routes, "the example no longer carries commented reference routes"
    return routes


@pytest.mark.unit
class TestRouteWiseExampleRegistry:
    """The shipped RouteWise example must load, build, and stay complete.

    `config/examples/models.routewise.yaml` is the reference an operator reads
    to learn what RouteWise can be tuned to do, and the runnable demo the
    README points at. The FreeInference overlay used to carry that block and a
    guard for it; both left with the overlay, so the guard now protects the
    public example instead.
    """

    def test_example_registers_and_builds_routewise_candidates(self, monkeypatch):
        """Load it the way the gateway does, then build what RouteWise routes on."""
        monkeypatch.delenv("MODELS_CONFIG", raising=False)
        router = FixedRouter()
        count, _infos = register_from_models_yaml(router, _EXAMPLE)
        assert count >= 1, "the example model was skipped at registration"

        adapters = router.routes["routewise-demo"].adapters
        candidates = build_provider_candidates("routewise-demo", adapters)

        assert len(candidates) == 2, "the demo needs two providers to have a choice"
        # Distinct endpoint_ids are what give the two providers separate
        # latency profiles; build_provider_candidates rejects duplicates.
        assert len({c.endpoint_id for c in candidates}) == 2
        # The contrast the example documents: one route dearer than the other.
        prices = sorted(c.pricing.prompt for c in candidates)
        assert prices[0] < prices[1], "both routes price the same; alpha would do nothing"

    def test_router_params_validate_against_the_schema(self):
        params = RouteWiseParams(**_example_model()["router_params"])
        # The demo turns the prober on: without it neither endpoint the policy
        # is avoiding ever gets measured, and budget_alpha stops mattering.
        assert params.routewise_probe_enabled is True

    def test_commented_quota_and_concurrency_routes_parse(self):
        """A reference block that cannot be copied is worse than none."""
        by_type = {route["provider_type"]: route for route in _commented_reference_routes()}
        assert set(by_type) == {"quota", "concurrency"}

        quota = by_type["quota"]
        source = QuotaSource.from_raw(quota["quota_source"])
        QuotaPolicy.from_raw(quota["quota"], context="quota")
        assert quota["quota_pool"]
        # `local` counts one per request, so a token unit would misreport it.
        if source.provider == "local":
            assert source.unit == "requests"

        concurrency = by_type["concurrency"]
        ConcurrencyPolicy.from_raw(concurrency["concurrency"], context="concurrency")
        assert concurrency["concurrency_pool"]

    def test_every_config_option_is_documented(self):
        text = _EXAMPLE.read_text()
        for field in dataclass_fields(RouteWiseConfig):
            assert field.name in text, (
                f"config/examples/models.routewise.yaml no longer mentions "
                f"RouteWise option {field.name!r}"
            )

    def test_example_does_not_promise_a_static_route_id(self):
        """`route_id:` is an admin-API field; registry.py ignores it in YAML."""
        for route in _example_model()["route"]:
            assert "route_id" not in route


def _router_for_alpha(alpha: float) -> tuple[RouteWiseRouter, str, str]:
    """Build the shipped example's RouteWise router at a given cost budget."""
    fixed = FixedRouter()
    register_from_models_yaml(fixed, _EXAMPLE)
    router = RouteWiseRouter(
        route_table=fixed,
        config=RouteWiseConfig(budget_alpha=alpha, latency_min_samples=5),
    )
    for _ in range(25):
        router.predictor.update("routewise-demo", 500)
    endpoints = sorted(router._latency_profiles)
    assert len(endpoints) == 2, endpoints
    premium, budget = endpoints  # local-18351 sorts before local-18352
    return router, premium, budget


def _endpoints_selected(router: RouteWiseRouter, samples: int = 30) -> set[str]:
    """Selection samples the LP weights, so ask more than once."""
    return {
        router._select_decision(
            "routewise-demo", {"prompt_tokens": 1000}
        ).adapter.config.endpoint_id
        for _ in range(samples)
    }


@pytest.mark.unit
class TestRouteWiseExampleBehaviour:
    """How the LP responds to latency evidence, with the evidence handed to it.

    Samples go straight into `_latency_profiles`, so this proves the policy
    half of the README's claim and nothing about how the measurements get
    there. The probe that actually produces them, the app that starts it, and
    the restart in between are covered end to end by
    `tests/servers/test_routewise_example_runtime.py` -- which is where a
    broken probe lifecycle turns something red.
    """

    def test_alpha_moves_the_route_once_latency_is_known(self):
        for alpha, expected in ((0.0, "budget"), (1.0, "premium")):
            router, premium, budget = _router_for_alpha(alpha)
            now = time.time()
            for _ in range(10):
                router._latency_profiles[premium].record(now, 50.0)
                router._latency_profiles[budget].record(now, 450.0)
            wanted = budget if expected == "budget" else premium
            assert _endpoints_selected(router) == {wanted}, (
                f"budget_alpha={alpha} should serve from the {expected} endpoint"
            )

    def test_without_latency_evidence_alpha_does_nothing(self):
        """Why the example turns the prober on, asserted rather than asserted-in-prose."""
        selections = []
        for alpha in (0.0, 1.0):
            router, _premium, budget = _router_for_alpha(alpha)
            selections.append((_endpoints_selected(router), budget))
        (cold_zero, budget), (cold_one, _) = selections
        assert cold_zero == cold_one == {budget}, (
            "unprofiled endpoints should tie on latency and lose the tiebreak on "
            "price at every alpha; if this changes, the example's prober rationale "
            "needs rewriting"
        )


@pytest.mark.unit
class TestRouteWiseExampleQuotaContract:
    """The cheap half of the quota contract: is the provider even wired up?

    Whether the documented source actually resolves to a ready pool is decided
    by driving the real fetcher, in
    `tests/servers/test_routewise_example_runtime.py`.
    """

    def test_quota_provider_has_a_registered_fetcher(self):
        quota = next(
            route for route in _commented_reference_routes() if route["provider_type"] == "quota"
        )
        registered = set(ProviderQuotaSnapshotStore()._fetchers)
        provider = quota["quota_source"]["provider"]
        assert provider in registered, (
            f"quota_source names {provider!r}, but RouteWise only registers "
            f"{sorted(registered)}; the route would never become ready"
        )
