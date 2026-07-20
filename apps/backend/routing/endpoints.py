"""Canonical endpoint identity helpers for routing."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from serving.adapters.base import BaseAdapter


def endpoint_id_for_adapter(adapter: BaseAdapter) -> str:
    """Return the endpoint id used for routing state and health tracking."""
    return getattr(adapter.config, "endpoint_id", None) or adapter.config.provider
