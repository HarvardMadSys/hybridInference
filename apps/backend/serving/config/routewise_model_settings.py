"""Per-model RouteWise runtime setting resolution and application."""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from threading import RLock
from typing import TYPE_CHECKING, Any, Literal

from routing.routewise.config import RouteWiseConfig
from serving.config.runtime_settings import RUNTIME_SETTINGS_REGISTRY
from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from routing.model_router_registry import ModelRouterRegistry
    from routing.routewise.router import RouteWiseRouter
    from serving.config.runtime_settings import RuntimeSettings

logger = get_logger(__name__)

ROUTEWISE_SETTING_FIELDS: dict[str, str] = {
    "routewise_budget_alpha": "budget_alpha",
    "routewise_latency_slo_sec": "latency_slo_sec",
    "routewise_latency_min_samples": "latency_min_samples",
    "routewise_probe_enabled": "routewise_probe_enabled",
    "routewise_probe_interval_sec": "routewise_probe_interval_sec",
}
ROUTEWISE_SETTING_KEYS: tuple[str, ...] = tuple(ROUTEWISE_SETTING_FIELDS)
MODEL_ROUTEWISE_SETTING_PREFIX = "model_routewise_setting:"

RouteWiseSettingSource = Literal[
    "runtime_override",
    "model_config",
    "global_default",
]


@dataclass(frozen=True, slots=True)
class ResolvedRouteWiseSetting:
    """One effective model setting plus the value inherited without its override."""

    key: str
    value: bool | int | float
    fallback_value: bool | int | float
    source: RouteWiseSettingSource


def model_routewise_setting_key(routewise_key: str, canonical_model_id: str) -> str:
    """Return the ``site_settings`` key for one canonical model override."""
    if routewise_key not in ROUTEWISE_SETTING_FIELDS:
        raise KeyError(f"unknown RouteWise setting: {routewise_key}")
    if not canonical_model_id:
        raise ValueError("canonical_model_id must not be empty")
    return f"{MODEL_ROUTEWISE_SETTING_PREFIX}{routewise_key}:{canonical_model_id}"


def model_routewise_setting_keys(canonical_model_id: str) -> tuple[str, ...]:
    """Return all persisted override keys owned by one canonical model."""
    return tuple(
        model_routewise_setting_key(routewise_key, canonical_model_id)
        for routewise_key in ROUTEWISE_SETTING_KEYS
    )


def _parse_model_setting_key(setting_key: str) -> tuple[str, str] | None:
    for routewise_key in ROUTEWISE_SETTING_KEYS:
        prefix = f"{MODEL_ROUTEWISE_SETTING_PREFIX}{routewise_key}:"
        if setting_key.startswith(prefix):
            canonical_model_id = setting_key[len(prefix) :]
            if canonical_model_id:
                return routewise_key, canonical_model_id
            return None
    return None


def _coerce_setting_value(key: str, raw: Any) -> bool | int | float:
    entry = RUNTIME_SETTINGS_REGISTRY[key]
    value_type = str(entry["type"])
    if value_type == "bool":
        if isinstance(raw, bool):
            value: bool | int | float = raw
        elif isinstance(raw, str):
            normalized = raw.strip().lower()
            if normalized in {"true", "1", "yes", "on"}:
                value = True
            elif normalized in {"false", "0", "no", "off"}:
                value = False
            else:
                raise ValueError(f"invalid boolean value for {key}: {raw!r}")
        else:
            raise ValueError(f"invalid boolean value for {key}: {raw!r}")
    elif value_type == "int":
        if isinstance(raw, bool):
            raise ValueError(f"invalid integer value for {key}: {raw!r}")
        value = int(raw)
    elif value_type == "float":
        if isinstance(raw, bool):
            raise ValueError(f"invalid numeric value for {key}: {raw!r}")
        value = float(raw)
        if not math.isfinite(value):
            raise ValueError(f"non-finite numeric value for {key}: {raw!r}")
    else:  # pragma: no cover - registry contract for the five curated keys
        raise TypeError(f"unsupported RouteWise setting type: {value_type}")

    minimum = entry.get("min")
    maximum = entry.get("max")
    if minimum is not None and value < minimum:
        raise ValueError(f"{key} value {value!r} is below minimum {minimum!r}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{key} value {value!r} is above maximum {maximum!r}")
    return value


class RouteWiseSettingsResolver:
    """Resolve per-model overrides with generation fencing and LKG fallback."""

    def __init__(
        self,
        store: Any,
        runtime_settings: RuntimeSettings | None,
        registry: ModelRouterRegistry,
    ) -> None:
        self._store = store
        self._runtime_settings = runtime_settings
        self._registry = registry
        self._snapshots: dict[str, dict[str, bool | int | float]] = {}
        self._snapshot_generation = 0
        self._global_lkg: dict[str, bool | int | float] = {}
        self._global_refresh_generation = 0
        self._lock = RLock()

    def canonical_model_id(self, model_id: str) -> str:
        """Resolve an alias and reject unknown models without constructing one."""
        canonical_model_id = self._registry.canonical_model_id(model_id)
        if not self._registry.has_model(canonical_model_id):
            raise KeyError(f"unknown model: {model_id}")
        return canonical_model_id

    async def load_all(self) -> bool:
        """Reload all scoped rows, preserving local writes and last-known-good values."""
        with self._lock:
            # Reserve publication order before the SELECT. A newer load that
            # starts later owns the snapshot even if an older DB read happens
            # to complete first.
            self._snapshot_generation += 1
            snapshot_generation = self._snapshot_generation
            # Reserve the global publication generation before starting the DB
            # read. A newer load that starts later must win even if this older
            # SELECT returns last on another connection.
            self._global_refresh_generation += 1
            global_refresh_generation = self._global_refresh_generation
            previous_snapshots = {
                model_id: dict(settings) for model_id, settings in self._snapshots.items()
            }
            previous_globals = dict(self._global_lkg)

        rows = await self._store.list_settings()
        loaded: dict[str, dict[str, bool | int | float]] = {}
        invalid: dict[str, set[str]] = {}
        for row in rows:
            parsed = _parse_model_setting_key(str(row["key"]))
            if parsed is None:
                continue
            routewise_key, canonical_model_id = parsed
            try:
                value = _coerce_setting_value(routewise_key, row.get("value"))
            except (TypeError, ValueError):
                invalid.setdefault(canonical_model_id, set()).add(routewise_key)
                logger.warning(
                    "Ignoring invalid per-model RouteWise setting key=%s model=%s",
                    routewise_key,
                    canonical_model_id,
                    exc_info=True,
                )
                continue
            loaded.setdefault(canonical_model_id, {})[routewise_key] = value

        await self._refresh_global_defaults(
            force=True,
            rows=rows,
            generation=global_refresh_generation,
        )

        with self._lock:
            # Fence both a local admin write and a newer full reload. Publishing
            # one older query result after either event would resurrect a
            # deleted override or replace a newer value. A single generation is
            # sufficient and avoids retaining one tombstone per deleted model;
            # the next polling cycle picks up unrelated DB changes if this
            # entire stale snapshot is discarded.
            if self._snapshot_generation == snapshot_generation:
                resolved_snapshots: dict[str, dict[str, bool | int | float]] = {}
                for model_id in set(loaded) | set(invalid):
                    model_snapshot = dict(loaded.get(model_id, {}))
                    for key in invalid.get(model_id, set()):
                        previous = previous_snapshots.get(model_id, {})
                        if key in previous:
                            model_snapshot[key] = previous[key]
                    if model_snapshot:
                        resolved_snapshots[model_id] = model_snapshot
                self._snapshots = resolved_snapshots

            changed = previous_snapshots != self._snapshots or previous_globals != self._global_lkg
            return changed

    async def resolve_model(
        self,
        model_id: str,
    ) -> dict[str, ResolvedRouteWiseSetting]:
        """Return all effective settings for one canonical model."""
        canonical_model_id = self.canonical_model_id(model_id)
        configured_params = self._registry.get_configured_routewise_params(canonical_model_id)
        await self._refresh_global_defaults(force=False)
        with self._lock:
            overrides = dict(self._snapshots.get(canonical_model_id, {}))
            global_defaults = dict(self._global_lkg)

        built_in = RouteWiseConfig()
        resolved: dict[str, ResolvedRouteWiseSetting] = {}
        for key, field_name in ROUTEWISE_SETTING_FIELDS.items():
            if field_name in configured_params:
                # These values already passed the strategy's Pydantic schema.
                # Runtime edit limits must not retroactively reject a YAML
                # value accepted by RouteWiseParams at boot.
                fallback_value = configured_params[field_name]
                fallback_source: RouteWiseSettingSource = "model_config"
            else:
                fallback_value = global_defaults.get(
                    key,
                    _coerce_setting_value(key, getattr(built_in, field_name)),
                )
                fallback_source = "global_default"

            if key in overrides:
                value = overrides[key]
                source: RouteWiseSettingSource = "runtime_override"
            else:
                value = fallback_value
                source = fallback_source
            resolved[key] = ResolvedRouteWiseSetting(
                key=key,
                value=value,
                fallback_value=fallback_value,
                source=source,
            )
        return resolved

    async def get_resolved(
        self,
        model_id: str,
        key: str,
    ) -> ResolvedRouteWiseSetting:
        """Return one resolved setting."""
        if key not in ROUTEWISE_SETTING_FIELDS:
            raise KeyError(f"unknown RouteWise setting: {key}")
        return (await self.resolve_model(model_id))[key]

    def get_override_snapshot(self, model_id: str) -> dict[str, bool | int | float]:
        """Return the current non-blocking scoped override snapshot."""
        canonical_model_id = self._registry.canonical_model_id(model_id)
        with self._lock:
            return dict(self._snapshots.get(canonical_model_id, {}))

    def set_override(self, model_id: str, key: str, value: Any) -> None:
        """Publish one local snapshot value after its database upsert succeeds."""
        canonical_model_id = self.canonical_model_id(model_id)
        coerced = _coerce_setting_value(key, value)
        with self._lock:
            model_snapshot = dict(self._snapshots.get(canonical_model_id, {}))
            model_snapshot[key] = coerced
            self._snapshots[canonical_model_id] = model_snapshot
            self._snapshot_generation += 1

    def clear_override(self, model_id: str, key: str) -> None:
        """Remove one local snapshot value after its database delete succeeds."""
        if key not in ROUTEWISE_SETTING_FIELDS:
            raise KeyError(f"unknown RouteWise setting: {key}")
        canonical_model_id = self._registry.canonical_model_id(model_id)
        with self._lock:
            model_snapshot = dict(self._snapshots.get(canonical_model_id, {}))
            model_snapshot.pop(key, None)
            if model_snapshot:
                self._snapshots[canonical_model_id] = model_snapshot
            else:
                self._snapshots.pop(canonical_model_id, None)
            self._snapshot_generation += 1

    def clear_model(self, model_id: str) -> None:
        """Remove all local state for a deleted model and fence in-flight loads."""
        canonical_model_id = self._registry.canonical_model_id(model_id)
        with self._lock:
            self._snapshots.pop(canonical_model_id, None)
            self._snapshot_generation += 1

    async def _refresh_global_defaults(
        self,
        *,
        force: bool,
        rows: list[dict[str, Any]] | None = None,
        generation: int | None = None,
    ) -> None:
        with self._lock:
            if not force and all(key in self._global_lkg for key in ROUTEWISE_SETTING_KEYS):
                return
            if generation is None:
                self._global_refresh_generation += 1
                generation = self._global_refresh_generation

        runtime_settings = self._runtime_settings
        if force and runtime_settings is not None:
            for key in ROUTEWISE_SETTING_KEYS:
                runtime_settings.invalidate_key(key)

        listed_rows = (
            {str(row["key"]): row for row in rows if str(row["key"]) in ROUTEWISE_SETTING_KEYS}
            if rows is not None
            else None
        )

        async def read(key: str) -> tuple[str, bool | int | float | None]:
            entry = RUNTIME_SETTINGS_REGISTRY[key]
            try:
                # load_all() already fetched the complete site_settings table;
                # reuse it so every worker poll is one DB query, not one list
                # plus five point reads. Direct refresh callers retain the
                # point-read path.
                row = listed_rows.get(key) if listed_rows is not None else None
                if listed_rows is None:
                    row = await self._store.get_setting(key)
                if row is not None:
                    raw_value = row.get("value")
                else:
                    # Mirror RuntimeSettings' non-DB fallbacks without sharing
                    # its TTL cache. Concurrent poll/admin reads can otherwise
                    # publish an older cached global value after a newer one.
                    from serving.config.settings import get_settings

                    raw_value = getattr(get_settings(), key, entry["default"])
                return key, _coerce_setting_value(key, raw_value)
            except Exception:
                logger.warning(
                    "Failed to resolve legacy global RouteWise setting key=%s; using LKG",
                    key,
                    exc_info=True,
                )
                return key, None

        values = await asyncio.gather(*(read(key) for key in ROUTEWISE_SETTING_KEYS))
        with self._lock:
            if generation != self._global_refresh_generation:
                return
            for key, value in values:
                if value is not None:
                    self._global_lkg[key] = value


async def apply_routewise_settings_to_router(
    resolver: RouteWiseSettingsResolver,
    registry: ModelRouterRegistry,
    model_id: str,
    router: RouteWiseRouter,
    *,
    refresh_probe_task: bool = False,
) -> dict[str, ResolvedRouteWiseSetting]:
    """Atomically replace the five effective settings on one RouteWise router."""
    canonical_model_id = registry.canonical_model_id(model_id)
    resolved = await resolver.resolve_model(canonical_model_id)
    router.apply_runtime_overrides(
        **{field_name: resolved[key].value for key, field_name in ROUTEWISE_SETTING_FIELDS.items()}
    )
    if refresh_probe_task:
        await router.refresh_probe_task()
    return resolved


__all__ = [
    "MODEL_ROUTEWISE_SETTING_PREFIX",
    "ROUTEWISE_SETTING_FIELDS",
    "ROUTEWISE_SETTING_KEYS",
    "ResolvedRouteWiseSetting",
    "RouteWiseSettingsResolver",
    "apply_routewise_settings_to_router",
    "model_routewise_setting_key",
    "model_routewise_setting_keys",
]
