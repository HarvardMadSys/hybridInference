"""Served-identity disclosure on an admin-edited route (issue #1504 follow-up).

An admin edit may repoint a route onto a different provider kind. The route's
publishability must follow the NEW kind: a route that ends up on a generic kind
(``openai_compat``, ``vllm``, ...) points at whatever host the deployment
chose, so its endpoint id must never reach a response header.

``_carry_endpoint_facts_if_unmoved`` re-applies facts from the route's
*previous* adapter after the publishability decision has been made, so a
dedicated-kind route that keeps its endpoint id can hand its
``endpoint_identity_publishable=True`` to the generic-kind route it was
edited into.
"""

from __future__ import annotations

from typing import Any

from serving.servers.registry import _make_provider_id
from serving.servers.routers.admin import provider_routes
from serving.servers.routers.served_identity import header_endpoint_id

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


class _Config:
    def __init__(self, **kwargs: Any) -> None:
        self.provider = "zai"
        self.base_url = "https://example-zai.test"
        self.endpoint_id = "glm-4.7:zai-api"
        self.endpoint_identity_publishable = True
        self.route_metadata: dict[str, Any] = {}
        for key, value in kwargs.items():
            setattr(self, key, value)


class _Adapter:
    def __init__(self, **kwargs: Any) -> None:
        self.config = _Config(**kwargs)


def test_editing_a_dedicated_route_onto_a_generic_kind_drops_publishability():
    """The publishability decision must survive the fact-carry step.

    ``_prepare_route_update`` runs, in order:
      1. ``_apply_target_adapter_defaults``   -> publishability from the NEW kind
      2. ``_finalize_admin_endpoint_publishability`` -> an admin-supplied id is never publishable
      3. ``_carry_endpoint_facts_if_unmoved``  -> re-applies the OLD adapter's facts

    Step 3 must not resurrect the old adapter's ``True`` for a route whose kind
    has changed to a generic one.
    """
    model_id = "glm-4.7"
    base_url = "https://example-zai.test"

    current_adapter = _Adapter(
        provider="zai",
        base_url=base_url,
        endpoint_id=_make_provider_id(model_id, "zai", base_url),
        endpoint_identity_publishable=True,
    )

    cfg: dict[str, Any] = {
        "endpoint_id": current_adapter.config.endpoint_id,
        "base_url": base_url,
        "provider": "zai",
    }

    # Step 1: the edit moves the route onto a generic kind.
    cfg["endpoint_id"] = provider_routes._edited_endpoint_id(
        model_id, GENERIC_TARGET, current_adapter, base_url
    )
    provider_routes._apply_target_adapter_defaults(cfg, GENERIC_TARGET)
    assert cfg["endpoint_identity_publishable"] is False

    # Step 2: an admin-supplied endpoint id is never publishable.
    provider_routes._finalize_admin_endpoint_publishability(
        cfg,
        model_id=model_id,
        target=GENERIC_TARGET,
        base_url=base_url,
        endpoint_id=cfg.get("endpoint_id"),
    )
    assert cfg["endpoint_identity_publishable"] is False

    # Step 3: the fact-carry step must not re-enable it.
    provider_routes._carry_endpoint_facts_if_unmoved(
        cfg, current_adapter=current_adapter, base_url=base_url
    )

    assert cfg["endpoint_identity_publishable"] is False, (
        "the previous adapter's publishability was carried onto a generic-kind route"
    )
    # And nothing private reaches the header.
    assert (
        header_endpoint_id(
            cfg["endpoint_id"], model_id, publishable=cfg["endpoint_identity_publishable"]
        )
        is None
    )


def test_carrying_endpoint_facts_does_not_resurrect_publishability_for_a_new_kind():
    """A route edited onto a generic kind keeps its endpoint id, not its facts.

    The endpoint id survives the edit (the route still points at the same
    host), so the fact-carry step's own guard does not stop it. Only the
    publishability decision may stop it.
    """
    model_id = "glm-4.7"
    base_url = "http://localhost:8000"

    # A dedicated-kind route at a local host: publishable, and its id names
    # the local port.
    current_adapter = _Adapter(
        provider="zai",
        base_url=base_url,
        endpoint_id=_make_provider_id(model_id, "zai", base_url),
        endpoint_identity_publishable=True,
    )

    cfg: dict[str, Any] = {
        "endpoint_id": current_adapter.config.endpoint_id,
        "base_url": base_url,
        "provider": "zai",
    }
    cfg["endpoint_id"] = provider_routes._edited_endpoint_id(
        model_id, GENERIC_TARGET, current_adapter, base_url
    )
    provider_routes._apply_target_adapter_defaults(cfg, GENERIC_TARGET)
    provider_routes._finalize_admin_endpoint_publishability(
        cfg,
        model_id=model_id,
        target=GENERIC_TARGET,
        base_url=base_url,
        endpoint_id=cfg.get("endpoint_id"),
    )
    provider_routes._carry_endpoint_facts_if_unmoved(
        cfg, current_adapter=current_adapter, base_url=base_url
    )

    assert cfg["endpoint_identity_publishable"] is False
    assert (
        header_endpoint_id(
            cfg["endpoint_id"], model_id, publishable=cfg["endpoint_identity_publishable"]
        )
        is None
    ), "a local endpoint id reached the diagnostic header"
