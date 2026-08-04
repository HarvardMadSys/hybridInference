"""Loader tests for per-route provider relabelling (`provider:` on a route).

Two endpoints of the same kind — e.g. two local GPU boxes both served by vLLM
— collapse into one cohort in every provider-scoped dashboard view because the
label they report is the route kind. A route-level `provider:` splits them,
while everything that must keep following the *real* upstream (API-key pools,
admin route targets) stays bound to the kind.
"""

from __future__ import annotations

import textwrap
from typing import TYPE_CHECKING

import pytest

from routing.executor import RouteExecutor
from serving.adapters import dynamic_keys
from serving.config.provider_labels import (
    display_names_from_router,
    resolve_display_names,
)
from serving.servers.registry import (
    RESERVED_PROVIDER_LABELS,
    parse_route_provider_label,
    register_from_models_yaml,
)

if TYPE_CHECKING:
    from pathlib import Path


def _write_yaml(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "models.yaml"
    path.write_text(textwrap.dedent(body))
    return path


def _two_local_boxes_yaml(tmp_path: Path, extra: str = "") -> Path:
    return _write_yaml(
        tmp_path,
        f"""
        models:
          - id: qwen-local
            name: qwen-local
            provider: vllm
            base_url: http://localhost:8002/v1
            route:
              - kind: vllm
                weight: 1.0
                provider: local-a
                provider_display_name: "Local box A"
                base_url: http://localhost:8002/v1
                api_key: ${{LOCAL_API_KEY}}
              - kind: vllm
                weight: 1.0
                provider: local-b
                provider_display_name: "Local box B"
                base_url: http://localhost:8003/v1
                api_key: ${{LOCAL_API_KEY}}
        {extra}
        """,
    )


def test_same_kind_routes_get_distinct_provider_labels(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_API_KEY", "local-key")
    router = RouteExecutor()
    register_from_models_yaml(router, _two_local_boxes_yaml(tmp_path))

    adapters = router.routes["qwen-local"].adapters
    assert [a.config.provider for a, _w in adapters] == ["local-a", "local-b"]
    # endpoint_id still keys off the kind + port, so latency profiling and the
    # circuit breaker are unaffected by the relabelling.
    assert [a.config.endpoint_id for a, _w in adapters] == [
        "qwen-local:local-8002",
        "qwen-local:local-8003",
    ]


def test_label_records_upstream_identity_in_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_API_KEY", "local-key")
    router = RouteExecutor()
    register_from_models_yaml(router, _two_local_boxes_yaml(tmp_path))

    adapter = router.routes["qwen-local"].adapters[0][0]
    metadata = adapter.config.route_metadata
    # The admin Routing tab resolves route targets through these keys; without
    # them a label like "local-a" would fall through to the OpenRouter-pin
    # branch of _target_for_provider and misrepresent a local vLLM route.
    assert metadata["key_provider"] == "vllm"
    assert metadata["route_provider"] == "vllm"
    assert metadata["upstream_provider"] == "vllm"
    # route_provider == upstream_provider, so RouteWise does not read the pair
    # as an override provider and switch the route to local quota state.
    assert metadata["route_provider"] == metadata["upstream_provider"]


def test_relabelled_routes_share_the_kind_key_pool(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_API_KEY", "local-key")
    dynamic_keys.reset()
    router = RouteExecutor()
    register_from_models_yaml(router, _two_local_boxes_yaml(tmp_path))

    # Keys live under the provider actually being talked to, not the label, so
    # one LOCAL_API_KEY still serves both boxes.
    assert "vllm" in dynamic_keys.get_known_providers()
    assert "local-a" not in dynamic_keys.get_known_providers()
    dynamic_keys.reset()


def test_explicit_route_metadata_wins_over_the_pins(tmp_path, monkeypatch):
    """An explicit route_metadata is the operator declaring the upstream, which
    is how the override-provider quota pattern is configured. The label's pins
    must not overwrite it."""
    monkeypatch.setenv("LOCAL_API_KEY", "local-key")
    yaml_path = _write_yaml(
        tmp_path,
        """
        models:
          - id: qwen-local
            name: qwen-local
            provider: vllm
            base_url: http://localhost:8002/v1
            route:
              - kind: vllm
                weight: 1.0
                provider: local-a
                base_url: http://localhost:8002/v1
                api_key: ${LOCAL_API_KEY}
                route_metadata:
                  upstream_provider: chutes
        """,
    )
    router = RouteExecutor()
    register_from_models_yaml(router, yaml_path)

    metadata = router.routes["qwen-local"].adapters[0][0].config.route_metadata
    assert metadata["upstream_provider"] == "chutes"
    # The keys the config did not set still get pinned.
    assert metadata["key_provider"] == "vllm"


def test_display_names_are_exposed_for_the_dashboard(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_API_KEY", "local-key")
    router = RouteExecutor()
    register_from_models_yaml(router, _two_local_boxes_yaml(tmp_path))

    assert display_names_from_router(router) == {"local-a": "Local box A", "local-b": "Local box B"}
    resolved = resolve_display_names(router)
    assert resolved["local-a"] == "Local box A"
    # Built-in labels keep their names alongside the config-declared ones.
    assert resolved["vllm"] == "vLLM"


def test_route_without_label_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_API_KEY", "local-key")
    yaml_path = _write_yaml(
        tmp_path,
        """
        models:
          - id: plain
            name: plain
            provider: vllm
            base_url: http://localhost:8002/v1
            route:
              - kind: vllm
                weight: 1.0
                base_url: http://localhost:8002/v1
                api_key: ${LOCAL_API_KEY}
        """,
    )
    router = RouteExecutor()
    register_from_models_yaml(router, yaml_path)

    adapter = router.routes["plain"].adapters[0][0]
    assert adapter.config.provider == "vllm"
    # No label means no metadata pins: existing configs are byte-for-byte
    # equivalent to before the feature.
    assert "key_provider" not in adapter.config.route_metadata
    assert "route_provider" not in adapter.config.route_metadata
    assert "upstream_provider" not in adapter.config.route_metadata


def test_openai_compat_openai_model_still_labels_as_openai(tmp_path, monkeypatch):
    """The openai_compat + `provider: openai` special case survives."""
    monkeypatch.setenv("OPENAI_KEY", "k")
    yaml_path = _write_yaml(
        tmp_path,
        """
        models:
          - id: gpt-x
            name: gpt-x
            provider: openai
            base_url: https://api.openai.com/v1
            route:
              - kind: openai_compat
                weight: 1.0
                base_url: https://api.openai.com/v1
                api_key: ${OPENAI_KEY}
        """,
    )
    router = RouteExecutor()
    register_from_models_yaml(router, yaml_path)
    assert router.routes["gpt-x"].adapters[0][0].config.provider == "openai"


# --- parse_route_provider_label ------------------------------------------


def test_parse_defaults_to_canonical_provider():
    assert parse_route_provider_label({}, "vllm", "m") == ("vllm", None)


def test_parse_returns_label_and_display_name():
    route = {"provider": "local-a", "provider_display_name": "  Local box A  "}
    assert parse_route_provider_label(route, "vllm", "m") == ("local-a", "Local box A")


def test_parse_allows_display_name_without_a_label():
    """Naming a provider without splitting it is a valid, useful config."""
    assert parse_route_provider_label({"provider_display_name": "vLLM (local)"}, "vllm", "m") == (
        "vllm",
        "vLLM (local)",
    )


def test_parse_accepts_label_equal_to_its_own_reserved_canonical():
    """Spelling the label out explicitly is a no-op, not a collision."""
    assert parse_route_provider_label({"provider": "vllm"}, "vllm", "m") == ("vllm", None)


@pytest.mark.parametrize(
    "label",
    ["Local-A", "local a", "-local", "", "local.a", "x" * 65, 7, True],
)
def test_parse_rejects_malformed_labels(label):
    with pytest.raises(ValueError, match="must use lowercase"):
        parse_route_provider_label({"provider": label}, "vllm", "m")


@pytest.mark.parametrize("label", ["zai", "openrouter", "router", "minimax"])
def test_parse_rejects_labels_reserved_by_other_providers(label):
    """Borrowing another provider's label would fold this route's traffic into
    that provider's quota reporting, disable switch, and stats cohort."""
    with pytest.raises(ValueError, match="reserved"):
        parse_route_provider_label({"provider": label}, "vllm", "m")


@pytest.mark.parametrize("display_name", ["", "   ", 5])
def test_parse_rejects_blank_display_name(display_name):
    with pytest.raises(ValueError, match="provider_display_name"):
        parse_route_provider_label(
            {"provider": "local-a", "provider_display_name": display_name}, "vllm", "m"
        )


def test_bad_label_fails_the_load(tmp_path, monkeypatch):
    """A malformed label aborts registration rather than silently dropping the
    model, matching how the loader treats other static route errors (e.g.
    `api_keys` that is not a list). Only missing env-backed values degrade."""
    monkeypatch.setenv("LOCAL_API_KEY", "local-key")
    yaml_path = _write_yaml(
        tmp_path,
        """
        models:
          - id: bad
            name: bad
            provider: vllm
            base_url: http://localhost:8003/v1
            route:
              - kind: vllm
                weight: 1.0
                provider: "Not A Slug"
                base_url: http://localhost:8003/v1
                api_key: ${LOCAL_API_KEY}
        """,
    )
    router = RouteExecutor()
    with pytest.raises(ValueError, match="must use lowercase"):
        register_from_models_yaml(router, yaml_path)


def test_reserved_labels_cover_every_dispatchable_kind():
    """Guard against drift: a new adapter kind must be added to
    RESERVED_PROVIDER_LABELS, or a route could relabel itself onto it."""
    import inspect

    from serving.servers import registry

    source = inspect.getsource(registry._make_adapter)
    quoted = set(__import__("re").findall(r'"([a-z0-9_]+)"', source))
    # Names in _make_adapter that are config keys or profile values, not kinds.
    non_kinds = {
        "provider_profile",
        "openrouter_pinned_provider",
        "chat_path",
        "include_usage_in_stream",
    }
    kinds = quoted - non_kinds
    assert kinds <= RESERVED_PROVIDER_LABELS, sorted(kinds - RESERVED_PROVIDER_LABELS)
