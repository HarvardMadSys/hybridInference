"""Runtime registry for admin-created upstream provider definitions."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

from serving.adapters import dynamic_keys
from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from serving.storage.base import ProviderDefinitionRow

logger = get_logger(__name__)


@dataclass(frozen=True)
class RuntimeProviderDefinition:
    """Custom provider metadata needed outside the storage layer."""

    provider: str
    display_name: str
    adapter_kind: str
    default_base_url: str


_lock = threading.Lock()
_definitions: dict[str, RuntimeProviderDefinition] = {}


def register_provider_definition(row: ProviderDefinitionRow | RuntimeProviderDefinition) -> None:
    """Register a custom provider definition in-process."""
    if getattr(row, "status", "active") != "active":
        unregister_provider_definition(row.provider)
        return
    definition = RuntimeProviderDefinition(
        provider=row.provider,
        display_name=row.display_name,
        adapter_kind=row.adapter_kind,
        default_base_url=row.default_base_url,
    )
    with _lock:
        _definitions[definition.provider] = definition
    dynamic_keys.register_known_provider(definition.provider)


def unregister_provider_definition(provider: str) -> None:
    """Remove a custom provider definition from the in-process registry."""
    with _lock:
        _definitions.pop(provider, None)
    dynamic_keys.unregister_known_provider(provider)


def get_provider_definition(provider: str) -> RuntimeProviderDefinition | None:
    """Return one custom provider definition, if registered."""
    with _lock:
        return _definitions.get(provider)


def list_provider_definitions() -> list[RuntimeProviderDefinition]:
    """Return all custom provider definitions currently registered."""
    with _lock:
        return sorted(_definitions.values(), key=lambda row: (row.display_name, row.provider))


async def apply_provider_definitions_at_boot(
    op_store,
    *,
    reserved_providers: set[str] | None = None,
    config_label_providers: set[str] | None = None,
) -> None:
    """Load custom provider definitions from storage into runtime registries.

    Args:
        op_store: Operational store holding the definition rows.
        reserved_providers: Slugs owned by code or config/models.yaml. Rows
            matching one are skipped, so a stale row can never resurrect or
            shadow a built-in provider.
        config_label_providers: Subset of ``reserved_providers`` claimed by a
            route-level ``provider:`` label rather than by a real provider.
            Creating a custom provider with such a slug is already rejected, so
            a collision here means a label was added to models.yaml after the
            custom provider existed. The definition still wins — routes depend
            on its key pool and route target — but the clash is logged, because
            both then report under one label in analytics.
    """
    reserved = reserved_providers or set()
    label_only = config_label_providers or set()
    for row in await op_store.list_provider_definitions():
        if row.provider in label_only:
            logger.error(
                "Provider %r is both a custom provider and a route provider label in "
                "models.yaml. Keeping the custom provider; rename the route label so "
                "their traffic stops merging under one provider in analytics.",
                row.provider,
            )
            register_provider_definition(row)
            continue
        if row.provider in reserved:
            continue
        register_provider_definition(row)
