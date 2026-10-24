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
    default_required_role_for_model,
    model_access_denial,
    model_cost_is_unknown_remote,
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


class TestDefaultRequiredRoleForModel:
    """The loader's default tier for a models.yaml entry (issue #1576)."""

    def test_an_omitted_role_on_a_paid_model_defaults_to_pro(self):
        model = {"id": "claude-opus-4.8", "pricing": {"prompt": "1.2", "completion": "4.8"}}
        assert default_required_role_for_model(model) == "pro"

    def test_an_omitted_role_on_a_zero_priced_model_stays_free(self):
        model = {"id": "free-chat", "pricing": {"prompt": "0", "completion": "0"}}
        assert default_required_role_for_model(model) == "free"

    def test_an_omitted_role_on_a_model_without_pricing_stays_free(self):
        # No pricing block at all (local GPU default): the historical default.
        assert default_required_role_for_model({"id": "local-only"}) == "free"

    def test_an_unknown_cost_remote_route_defaults_to_pro(self):
        # The #1605 gap: an externally billed upstream configured with no
        # pricing metadata and no role. The config cannot prove it is free,
        # and the route reaches a non-local host, so it defaults closed.
        model = {
            "id": "ext-silent",
            "route": [
                {
                    "kind": "openrouter",
                    "base_url": "https://openrouter.ai/api/v1",
                    "api_keys": ["${OPENROUTER_API_KEY}"],
                    "provider_model_id": "meta-llama/llama-3.3-70b-instruct",
                }
            ],
        }
        assert model_cost_is_unknown_remote(model) is True
        assert default_required_role_for_model(model) == "pro"

    def test_a_local_inference_kind_on_a_local_host_stays_free(self):
        # The one free-by-construction exemption the gate still grants: a
        # route whose kind names a self-hosted engine API (sglang, vllm,
        # ollama) on a local host. The kind is the deployment asserting
        # "this box runs the engine itself", which is what makes the
        # absence of a pricing block meaningful — an engine API is not a
        # forwarding dialect.
        for kind in ("sglang", "vllm", "ollama"):
            model = {
                "id": "local-gpu",
                "route": [{"kind": kind, "base_url": "http://localhost:12003/v1"}],
            }
            assert model_cost_is_unknown_remote(model) is False, kind
            assert default_required_role_for_model(model) == "free", kind

    def test_a_route_inherits_the_model_level_base_url_like_the_registry(self):
        # The registry fills an omitted route base_url from the top-level
        # config; the classifier mirrors that, so a locally-served engine
        # declared with a model-level base_url stays free.
        model = {
            "id": "local-gpu",
            "provider": "sglang",
            "base_url": "http://localhost:12003/v1",
            "route": [{"kind": "sglang", "weight": 1.0}],
        }
        assert model_cost_is_unknown_remote(model) is False
        assert default_required_role_for_model(model) == "free"

    def test_an_env_template_base_url_fails_closed(self):
        # A ${VAR} base_url cannot be classified from the raw config: the
        # registry expands it, this gate must not guess. An engine kind on
        # an unclassifiable host still defaults pro; an explicit role or a
        # zero pricing block is the remedy.
        model = {
            "id": "local-gpu",
            "route": [{"kind": "sglang", "base_url": "${LOCAL_ENGINE_URL}"}],
        }
        assert model_cost_is_unknown_remote(model) is True
        assert default_required_role_for_model(model) == "pro"

    def test_a_local_compat_proxy_defaults_to_pro(self):
        # The gap the local-host rule used to leave open (audit, 2026-10-20):
        # an openai_compat route on localhost is a forwarding dialect, not a
        # local engine — the proxy can relay to a paid remote provider, and
        # local network placement proves nothing about who invoices. The
        # config cannot prove this route is free, so the default closes.
        model = {
            "id": "local-proxy",
            "route": [{"kind": "openai_compat", "base_url": "http://localhost:8080/v1"}],
        }
        assert model_cost_is_unknown_remote(model) is True
        assert default_required_role_for_model(model) == "pro"

    def test_an_unpriced_route_without_a_local_inference_kind_defaults_to_pro(self):
        # Any kind the config does not explicitly pin to a self-hosted engine
        # API stays unknown-cost even on a local host: remote API kinds,
        # canary kinds, and an omitted kind (which inherits the model's
        # provider field) all fail closed.
        for kind in (None, "openrouter", "staging", "kimi"):
            route = {"base_url": "http://127.0.0.1:9000/v1"}
            if kind is not None:
                route["kind"] = kind
            model = {"id": "m", "provider": kind or "zai", "route": [route]}
            assert model_cost_is_unknown_remote(model) is True, kind
            assert default_required_role_for_model(model) == "pro", kind

    def test_an_explicit_role_still_wins_on_a_local_proxy(self):
        # Deliberate free-tier access stays a configuration decision: an
        # explicit required_role is never overridden by the cost default.
        model = {
            "id": "local-proxy",
            "required_role": "free",
            "route": [{"kind": "openai_compat", "base_url": "http://localhost:8080/v1"}],
        }
        assert default_required_role_for_model(model) == "free"

    def test_an_explicit_zero_price_still_frees_a_local_proxy(self):
        # Explicit pricing at zero is the other deliberate free declaration.
        model = {
            "id": "local-proxy",
            "pricing": {"prompt": "0", "completion": "0"},
            "route": [{"kind": "openai_compat", "base_url": "http://localhost:8080/v1"}],
        }
        assert default_required_role_for_model(model) == "free"

    def test_a_synthesized_single_route_inherits_the_model_provider_kind(self):
        # A model with no route list synthesizes one from the top-level
        # config; its kind is the model's provider field, so the same rule
        # applies: a compat provider on localhost is a proxy, not an engine.
        model = {"id": "m", "provider": "openai_compat", "base_url": "http://localhost:8080/v1"}
        assert model_cost_is_unknown_remote(model) is True
        assert default_required_role_for_model(model) == "pro"
        engine = {"id": "m", "provider": "sglang", "base_url": "http://localhost:12003/v1"}
        assert model_cost_is_unknown_remote(engine) is False
        assert default_required_role_for_model(engine) == "free"

    def test_an_unparseable_top_level_base_url_defaults_closed(self):
        # A single-route model without a route list falls back to the
        # top-level base_url; if it cannot be parsed as local, default closed.
        model = {"id": "top-level-only", "base_url": "https://api.example.io/v1"}
        assert model_cost_is_unknown_remote(model) is True
        assert default_required_role_for_model(model) == "pro"

    def test_a_mixed_local_and_unknown_cost_remote_model_defaults_to_pro(self):
        # One remote route with no pricing metadata is enough: the model as a
        # whole can cost money.
        model = {
            "id": "hybrid-silent",
            "route": [
                {"kind": "openai_compat", "base_url": "http://localhost:11434/v1"},
                {"kind": "openrouter", "base_url": "https://openrouter.ai/api/v1"},
            ],
        }
        assert model_cost_is_unknown_remote(model) is True
        assert default_required_role_for_model(model) == "pro"

    def test_a_route_level_price_counts_as_paid(self):
        model = {
            "id": "hybrid",
            "pricing": {"prompt": "0", "completion": "0"},
            "route": [
                {"kind": "openai_compat", "base_url": "http://local", "pricing": {"prompt": "0.3"}},
                {"kind": "openai_compat", "base_url": "http://cloud"},
            ],
        }
        assert default_required_role_for_model(model) == "pro"

    def test_a_pricing_schedule_counts_as_paid(self):
        model = {"id": "sched", "pricing_schedule": {"effective_at": "2026-01-01T00:00:00Z"}}
        assert default_required_role_for_model(model) == "pro"

    def test_a_pricing_source_counts_as_paid(self):
        model = {"id": "sourced", "pricing_source": {"provider": "deepseek", "model": "x"}}
        assert default_required_role_for_model(model) == "pro"

    def test_an_unparseable_price_counts_as_paid(self):
        model = {"id": "odd", "pricing": {"prompt": "market"}}
        assert default_required_role_for_model(model) == "pro"

    def test_an_explicit_role_always_wins(self):
        model = {"id": "sponsored", "required_role": "free", "pricing": {"prompt": "2.0"}}
        assert default_required_role_for_model(model) == "free"

    def test_an_invalid_explicit_role_is_returned_untouched(self):
        # The registry validates and fails closed to admin; the default
        # helper must not swallow the typo.
        model = {"id": "typo", "required_role": "freee"}
        assert default_required_role_for_model(model) == "freee"


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
