"""Canonical endpoint identity helpers for routing."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from serving.adapters.base import BaseAdapter


def endpoint_id_for_config(config: Any) -> str | None:
    """Return the endpoint id declared on a model config, or ``None`` if unset.

    Unlike :func:`endpoint_id_for_adapter`, this deliberately does *not* fall
    back to the provider label. Attribution callers want the absence to stay
    visible: ``LogStore.log_request`` runs its own fallback chain
    (``endpoint_id`` -> routewise primary -> ``base_url`` -> ``provider``) when
    recovering ``api_logs.served_endpoint_id``, and handing it a provider label
    here would short-circuit that chain instead of recording a real
    per-endpoint identity.

    Takes the config rather than the adapter so a caller holding the config of
    the route that actually served (e.g.
    ``FallbackEmbeddingAdapter.serving_config``) can attribute to that backend
    rather than to the primary route.
    """
    endpoint_id = getattr(config, "endpoint_id", None)
    if isinstance(endpoint_id, str) and endpoint_id.strip():
        return endpoint_id.strip()
    return None


def endpoint_id_for_adapter(adapter: BaseAdapter) -> str:
    """Return the endpoint id used for routing state and health tracking."""
    return getattr(adapter.config, "endpoint_id", None) or adapter.config.provider


def route_id_for_adapter(adapter: BaseAdapter) -> str:
    """Return the admin-facing route id: the one identity that survives a retarget.

    The admin provider-routes API names a route by ``route_metadata.route_id`` and
    stamps it on every adapter it rebuilds, so the id stays put while an edit
    that moves the route to a new host renames its ``endpoint_id``. A route the
    API has never touched carries no such id and is named by its endpoint id.
    """
    route_metadata = getattr(adapter.config, "route_metadata", None)
    if isinstance(route_metadata, dict):
        route_id = route_metadata.get("route_id")
        if route_id:
            return str(route_id)
    return endpoint_id_for_adapter(adapter)
