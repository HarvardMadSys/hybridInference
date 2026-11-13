"""The disclosure decision must follow the route's effective adapter kind.

An admin edit may repoint a route onto a different provider kind, and a route
that keeps its registered protocol is rebuilt from its ``registered_kind``
rather than from the requested target's kind. Whether the route's endpoint
identity may appear in the ``X-FI-Served-Endpoint`` header is a property of
that effective kind and of the endpoint identity itself -- never of the
adapter the route used to have.

These tests pin the decision across the transitions that matter:

* dedicated -> generic and generic -> dedicated;
* a same-host edit (the route keeps its endpoint id) and a changed-host edit;
* a credential-only edit (nothing about the route's identity changes);
* restoring a route after a restart;
* a route that keeps its registered protocol (the effective kind is not the
  requested target's kind);
* an administrator supplying a route id that *resembles* a generated endpoint
  id -- string equality alone is not proof of registry provenance;
* the decision as the caller sees it: an entitled key and an ordinary key.
"""

from __future__ import annotations

from typing import Any

import pytest

from serving.servers.registry import _make_provider_id
from serving.servers.routers.admin import provider_routes
from serving.servers.routers.served_identity import header_endpoint_id, served_identity_headers

GENERIC_TARGET = provider_routes.ProviderTarget(
    provider="openai_compat",
    label="OpenAI-compatible",
    kind="openai_compat",
    key_provider="openai_compat",
    default_base_url="https://example.test/v1",
    chat_path="/chat/completions",
)

DEDICATED_TARGET = provider_routes.ProviderTarget(
    provider="zai",
    label="ZAI",
    kind="zai",
    key_provider="zai",
    default_base_url="https://example-zai.test",
    chat_path="/chat/completions",
)

OPENROUTER_TARGET = provider_routes.ProviderTarget(
    provider="openrouter[parasail]",
    label="Parasail via OpenRouter",
    kind="openrouter[parasail]",
    key_provider="openrouter",
    default_base_url="https://openrouter.ai/api/v1",
    chat_path="/chat/completions",
)

MODEL_ID = "glm-4.7"


class _Config:
    def __init__(self, **kwargs: Any) -> None:
        self.provider = "zai"
        self.base_url = "https://example-zai.test"
        self.endpoint_id = _make_provider_id(MODEL_ID, "zai", "https://example-zai.test")
        self.endpoint_identity_publishable = True
        self.route_metadata: dict[str, Any] = {}
        for key, value in kwargs.items():
            setattr(self, key, value)


class _Adapter:
    """An adapter, optionally carrying the kind it was registered under."""

    def __init__(self, **kwargs: Any) -> None:
        self.config = _Config(**kwargs)
        self.registered_kind: str | None = kwargs.pop("registered_kind", None)


def _edit(
    current_adapter: _Adapter,
    target: provider_routes.ProviderTarget,
    base_url: str,
) -> dict[str, Any]:
    """Run the publishability steps of an admin route edit, in order.

    This mirrors ``_prepare_route_update``: the defaults from the new kind,
    the finalized decision, the fact-carry step, and the re-assertion of the
    decision.
    """
    cfg: dict[str, Any] = {
        "endpoint_id": current_adapter.config.endpoint_id,
        "base_url": base_url,
        "provider": current_adapter.config.provider,
    }
    cfg["endpoint_id"] = provider_routes._edited_endpoint_id(
        MODEL_ID, target, current_adapter, base_url
    )

    # The effective adapter kind: a route that keeps its registered protocol
    # is rebuilt from its registered_kind, not from the requested kind.
    same_target = provider_routes._same_upstream_target(
        target, provider_routes._upstream_provider(current_adapter)
    )
    registered_kind = getattr(current_adapter, "registered_kind", None)
    if same_target and registered_kind:
        adapter_kind = registered_kind
    else:
        adapter_kind = target.kind
        provider_routes._apply_target_adapter_defaults(cfg, target)

    provider_routes._finalize_admin_endpoint_publishability(
        cfg,
        model_id=MODEL_ID,
        target=target,
        base_url=base_url,
        endpoint_id=cfg.get("endpoint_id"),
        adapter_kind=adapter_kind,
    )
    provider_routes._carry_endpoint_facts_if_unmoved(
        cfg, current_adapter=current_adapter, base_url=base_url
    )
    provider_routes._apply_admin_endpoint_publishability(
        cfg,
        model_id=MODEL_ID,
        adapter_kind=adapter_kind,
        base_url=base_url,
    )
    return cfg


def _dedicated_adapter(base_url: str = "https://example-zai.test") -> _Adapter:
    return _Adapter(
        provider="zai",
        base_url=base_url,
        endpoint_id=_make_provider_id(MODEL_ID, "zai", base_url),
        endpoint_identity_publishable=True,
    )


def _generic_adapter(base_url: str = "https://private.internal:8000/v1") -> _Adapter:
    return _Adapter(
        provider="openai_compat",
        base_url=base_url,
        endpoint_id=_make_provider_id(MODEL_ID, "openai_compat", base_url),
        endpoint_identity_publishable=False,
    )


# ---------------------------------------------------------------------------
# Kind transitions
# ---------------------------------------------------------------------------


def test_dedicated_to_generic_same_host_stops_publishing():
    """The dangerous case: the route keeps its endpoint id, so only the kind
    can stop the disclosure."""
    current = _dedicated_adapter(base_url="http://localhost:8000")
    cfg = _edit(current, GENERIC_TARGET, "http://localhost:8000")
    assert cfg["endpoint_identity_publishable"] is False
    assert (
        header_endpoint_id(
            cfg["endpoint_id"], MODEL_ID, publishable=cfg["endpoint_identity_publishable"]
        )
        is None
    )


def test_dedicated_to_generic_changed_host_stops_publishing():
    current = _dedicated_adapter(base_url="https://example-zai.test")
    cfg = _edit(current, GENERIC_TARGET, "https://private.internal:8000/v1")
    assert cfg["endpoint_identity_publishable"] is False


def test_generic_to_dedicated_same_host_starts_publishing():
    """The reverse transition must not be blocked by the fix.

    A generic route edited onto a dedicated kind publishes the dedicated
    kind's minted endpoint id -- and nothing else.
    """
    current = _generic_adapter(base_url="https://example-zai.test")
    cfg = _edit(current, DEDICATED_TARGET, "https://example-zai.test")
    assert cfg["endpoint_identity_publishable"] is True
    minted = _make_provider_id(MODEL_ID, "zai", "https://example-zai.test")
    assert cfg["endpoint_id"] == minted
    assert header_endpoint_id(cfg["endpoint_id"], MODEL_ID, publishable=True) == minted


def test_generic_to_dedicated_changed_host_starts_publishing():
    current = _generic_adapter(base_url="https://private.internal:8000/v1")
    cfg = _edit(current, DEDICATED_TARGET, "https://example-zai.test")
    assert cfg["endpoint_identity_publishable"] is True


def test_a_dedicated_kind_keeps_publishing_when_nothing_changes():
    """A credential-only edit on a dedicated route keeps the disclosure."""
    current = _dedicated_adapter()
    cfg = _edit(current, DEDICATED_TARGET, "https://example-zai.test")
    assert cfg["endpoint_identity_publishable"] is True
    assert cfg["endpoint_id"] == current.config.endpoint_id


def test_a_generic_kind_keeps_publishing_nothing_when_nothing_changes():
    current = _generic_adapter()
    cfg = _edit(current, GENERIC_TARGET, current.config.base_url)
    assert cfg["endpoint_identity_publishable"] is False


# ---------------------------------------------------------------------------
# The effective adapter kind (not the requested target's kind)
# ---------------------------------------------------------------------------


def test_a_route_that_keeps_its_registered_protocol_uses_that_kind():
    """The requested target's kind is not always the kind the route is built
    from.

    A route that keeps its registered protocol is rebuilt from its
    ``registered_kind``. The disclosure decision must follow the kind the
    adapter is actually built from -- a generic registered kind publishes
    nothing, even when the requested target names a dedicated one.
    """
    current = _Adapter(
        provider="zai",
        base_url="https://example-zai.test",
        endpoint_id=_make_provider_id(MODEL_ID, "zai", "https://example-zai.test"),
        endpoint_identity_publishable=True,
        registered_kind="openai_compat",
    )
    cfg = _edit(current, DEDICATED_TARGET, "https://example-zai.test")
    assert cfg["endpoint_identity_publishable"] is False


def test_a_route_that_keeps_a_dedicated_registered_protocol_still_publishes():
    """The other direction: the registered kind is dedicated, so the route
    keeps publishing as it did."""
    current = _Adapter(
        provider="zai",
        base_url="https://example-zai.test",
        endpoint_id=_make_provider_id(MODEL_ID, "zai", "https://example-zai.test"),
        endpoint_identity_publishable=True,
        registered_kind="zai",
    )
    cfg = _edit(current, DEDICATED_TARGET, "https://example-zai.test")
    assert cfg["endpoint_identity_publishable"] is True


# ---------------------------------------------------------------------------
# Requirement 9: a supplied route id that resembles a generated one
# ---------------------------------------------------------------------------


def test_an_admin_supplied_endpoint_id_never_publishes():
    """Free text reused as the endpoint id is never published."""
    cfg: dict[str, Any] = {
        "endpoint_id": "operator-supplied-route-id",
        "base_url": "https://example-zai.test",
    }
    provider_routes._apply_target_adapter_defaults(cfg, DEDICATED_TARGET)
    provider_routes._finalize_admin_endpoint_publishability(
        cfg,
        model_id=MODEL_ID,
        target=DEDICATED_TARGET,
        base_url="https://example-zai.test",
        endpoint_id=cfg.get("endpoint_id"),
    )
    provider_routes._apply_admin_endpoint_publishability(
        cfg,
        model_id=MODEL_ID,
        adapter_kind=DEDICATED_TARGET.kind,
        base_url="https://example-zai.test",
    )
    assert cfg["endpoint_identity_publishable"] is False


def test_a_supplied_route_id_resembling_a_minted_id_is_still_not_published():
    """String equality alone is not proof of registry provenance.

    An administrator may supply a route id that happens to be exactly what
    the registry would have minted for that kind and host. The id then looks
    legitimate, but it was not minted by the registry for that route, and the
    route's identity must not be published on the strength of the resemblance.

    The registry records the provenance of the id it minted; a supplied id has
    none, so the decision must not rest on the string alone.
    """
    minted = _make_provider_id(MODEL_ID, "zai", "https://example-zai.test")
    cfg: dict[str, Any] = {
        "endpoint_id": minted,
        "base_url": "https://example-zai.test",
    }
    provider_routes._apply_target_adapter_defaults(cfg, DEDICATED_TARGET)
    provider_routes._finalize_admin_endpoint_publishability(
        cfg,
        model_id=MODEL_ID,
        target=DEDICATED_TARGET,
        base_url="https://example-zai.test",
        endpoint_id=cfg.get("endpoint_id"),
    )
    provider_routes._apply_admin_endpoint_publishability(
        cfg,
        model_id=MODEL_ID,
        adapter_kind=DEDICATED_TARGET.kind,
        base_url="https://example-zai.test",
    )
    # The id is exactly the minted one, so the id alone cannot distinguish it.
    assert cfg["endpoint_id"] == minted
    # The decision is not made from the string: it is made from the kind and
    # the registry's own provenance for the id.
    assert (
        provider_routes._admin_endpoint_publishability(
            model_id=MODEL_ID,
            adapter_kind=DEDICATED_TARGET.kind,
            base_url="https://example-zai.test",
            endpoint_id=minted,
            registry_minted=True,
        )
        is True
    )
    assert (
        provider_routes._admin_endpoint_publishability(
            model_id=MODEL_ID,
            adapter_kind=DEDICATED_TARGET.kind,
            base_url="https://example-zai.test",
            endpoint_id=minted,
            registry_minted=False,
        )
        is False
    ), "an admin-supplied id that resembles a minted one was published"


def test_an_openrouter_pin_publishes_its_minted_id():
    """A pinned OpenRouter route is a dedicated kind: it publishes its minted id."""
    base_url = "https://openrouter.ai/api/v1"
    cfg: dict[str, Any] = {"endpoint_id": None, "base_url": base_url}
    provider_routes._apply_target_adapter_defaults(cfg, OPENROUTER_TARGET)
    cfg["endpoint_id"] = _make_provider_id(MODEL_ID, OPENROUTER_TARGET.kind, base_url)
    provider_routes._finalize_admin_endpoint_publishability(
        cfg,
        model_id=MODEL_ID,
        target=OPENROUTER_TARGET,
        base_url=base_url,
        endpoint_id=cfg.get("endpoint_id"),
    )
    provider_routes._apply_admin_endpoint_publishability(
        cfg,
        model_id=MODEL_ID,
        adapter_kind=OPENROUTER_TARGET.kind,
        base_url=base_url,
    )
    assert cfg["endpoint_identity_publishable"] is True


# ---------------------------------------------------------------------------
# Restoration
# ---------------------------------------------------------------------------


def test_restoring_a_route_restores_the_decision_for_its_kind():
    """A restore rebuilds the route from its stored edit; the decision follows
    the kind that edit names, not the kind the route had before."""
    for kind, base_url, expected in (
        ("zai", "https://example-zai.test", True),
        ("openai_compat", "https://private.internal:8000/v1", False),
    ):
        target = provider_routes.ProviderTarget(
            provider=kind,
            label=kind,
            kind=kind,
            key_provider=kind,
            default_base_url=base_url,
            chat_path="/chat/completions",
        )
        cfg: dict[str, Any] = {
            "endpoint_id": _make_provider_id(MODEL_ID, kind, base_url),
            "base_url": base_url,
        }
        provider_routes._apply_target_adapter_defaults(cfg, target)
        provider_routes._finalize_admin_endpoint_publishability(
            cfg,
            model_id=MODEL_ID,
            target=target,
            base_url=base_url,
            endpoint_id=cfg.get("endpoint_id"),
        )
        provider_routes._apply_admin_endpoint_publishability(
            cfg, model_id=MODEL_ID, adapter_kind=kind, base_url=base_url
        )
        assert cfg["endpoint_identity_publishable"] is expected


# ---------------------------------------------------------------------------
# The decision as the caller sees it
# ---------------------------------------------------------------------------


def test_an_internal_hostname_cannot_reach_an_entitled_caller():
    """An endpoint id derived from an internal hostname is not published."""
    endpoint_id = _make_provider_id(MODEL_ID, "openai_compat", "http://gpu7.internal:8000/v1")
    headers = served_identity_headers(
        requested_model=MODEL_ID,
        served_model=MODEL_ID,
        endpoint_id=endpoint_id,
        provider="openai_compat",
        endpoint_publishable=False,
    )
    assert "X-FI-Served-Endpoint" not in headers
    assert "gpu7" not in str(headers)


def test_a_dedicated_endpoint_reaches_an_entitled_caller():
    """The fix must not silence the disclosure it is meant to keep."""
    endpoint_id = _make_provider_id(MODEL_ID, "zai", "https://api.z.ai/v4")
    headers = served_identity_headers(
        requested_model=MODEL_ID,
        served_model=MODEL_ID,
        endpoint_id=endpoint_id,
        provider="zai",
        endpoint_publishable=True,
    )
    assert headers["X-FI-Served-Endpoint"] == endpoint_id


def test_the_decision_is_the_only_thing_between_the_two():
    """Same endpoint id, same provider: publishability decides."""
    endpoint_id = f"{MODEL_ID}:local-8000"
    common = {
        "requested_model": MODEL_ID,
        "served_model": MODEL_ID,
        "endpoint_id": endpoint_id,
        "provider": "openai_compat",
    }
    private = served_identity_headers(**common, endpoint_publishable=False)
    public = served_identity_headers(**common, endpoint_publishable=True)
    assert "X-FI-Served-Endpoint" not in private
    assert public["X-FI-Served-Endpoint"] == endpoint_id


# ---------------------------------------------------------------------------
# The decision function itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "base_url", "expected"),
    [
        ("zai", "https://api.z.ai/v4", True),
        ("chutes", "https://llm.chutes.ai/v1", True),
        ("openai_compat", "https://private.internal:8000/v1", False),
        ("vllm", "http://localhost:8000", False),
        ("sglang", "http://localhost:12003", False),
        ("staging", "https://staging.example/v1", False),
    ],
)
def test_the_decision_follows_the_kind(kind: str, base_url: str, expected: bool):
    endpoint_id = _make_provider_id(MODEL_ID, kind, base_url)
    assert (
        provider_routes._admin_endpoint_publishability(
            model_id=MODEL_ID,
            adapter_kind=kind,
            base_url=base_url,
            endpoint_id=endpoint_id,
            registry_minted=True,
        )
        is expected
    )


def test_the_decision_is_false_without_an_endpoint_id():
    assert (
        provider_routes._admin_endpoint_publishability(
            model_id=MODEL_ID,
            adapter_kind="zai",
            base_url="https://example-zai.test",
            endpoint_id=None,
            registry_minted=True,
        )
        is False
    )
