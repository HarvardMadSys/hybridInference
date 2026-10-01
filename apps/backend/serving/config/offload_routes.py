"""Runtime resolver for per-model queue-offload routes (see ``routing.offload``).

Each policy is one ``site_settings`` row keyed by canonical model id and holding
``{"route_id": ..., "wait_seconds": ...}`` as JSON, plus ``"engine_queue_limit"``
and ``"max_input_tokens"`` when the policy sets them. Routing reads the policy on
every request, synchronously, so the resolver keeps an in-process snapshot: warmed
at boot, updated by the admin endpoints after each successful write, and reloaded
periodically so a change made elsewhere converges.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from threading import RLock
from typing import TYPE_CHECKING, Any

from routing.offload import OffloadPolicy
from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from datetime import datetime

logger = get_logger(__name__)

MODEL_OFFLOAD_ROUTE_SETTING_PREFIX = "model_offload_route:"
OFFLOAD_ROUTE_VALUE_TYPE = "json"


def offload_route_setting_key(model_id: str) -> str:
    """Return the ``site_settings`` key holding one canonical model's policy."""
    if not model_id:
        raise ValueError("model_id must not be empty")
    return f"{MODEL_OFFLOAD_ROUTE_SETTING_PREFIX}{model_id}"


def model_id_from_offload_route_setting_key(key: str) -> str | None:
    """Return the model id a setting key belongs to, or None for other keys."""
    if not key.startswith(MODEL_OFFLOAD_ROUTE_SETTING_PREFIX):
        return None
    model_id = key[len(MODEL_OFFLOAD_ROUTE_SETTING_PREFIX) :]
    return model_id or None


def encode_offload_policy(policy: OffloadPolicy) -> str:
    """Serialize a policy into its stored ``site_settings`` value.

    ``engine_queue_limit`` and ``max_input_tokens`` are written only when the
    policy sets them, so a policy without them is stored exactly as it was
    before the fields existed.
    """
    value: dict[str, Any] = {"route_id": policy.route_id, "wait_seconds": policy.wait_seconds}
    if policy.engine_queue_limit is not None:
        value["engine_queue_limit"] = policy.engine_queue_limit
    if policy.max_input_tokens is not None:
        value["max_input_tokens"] = policy.max_input_tokens
    return json.dumps(value, sort_keys=True)


def decode_offload_policy(raw: Any) -> OffloadPolicy:
    """Parse a stored value back into a policy.

    Raises:
        ValueError: The value is not a JSON object naming a route and a positive,
            finite wait, or it carries an engine queue limit or a max input
            below 1.
    """
    try:
        payload = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError as exc:
        raise ValueError("offload route setting is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("offload route setting must be a JSON object")
    # OffloadPolicy validates every field itself and raises ValueError for a
    # missing or malformed one. A value written before the engine queue limit
    # or the max input existed has no such key, which reads as no limit.
    return OffloadPolicy(
        route_id=payload.get("route_id"),  # type: ignore[arg-type]
        wait_seconds=payload.get("wait_seconds"),  # type: ignore[arg-type]
        engine_queue_limit=payload.get("engine_queue_limit"),  # type: ignore[arg-type]
        max_input_tokens=payload.get("max_input_tokens"),  # type: ignore[arg-type]
    )


class AppliedOffloadPolicies:
    """The offload policies routing applies: those of models the shared router routes.

    The admin API refuses to set an offload route on a model routed by RouteWise or
    by the hybrid composition, and lists one stored before a model became either as
    inactive. The shared ``FixedRouter`` still sees those models: the composition
    drives it one planned attempt at a time, and ``/v1/messages`` and the admin
    playground call it for every model. So routing reads policies through this
    instead of the resolver, and a policy the admin list reports inactive for its
    model's router changes nothing.

    The model's router is read from the registry without building one: a model
    whose router has not been built yet is routed by its strategy alone.
    """

    def __init__(self, resolver: OffloadRouteResolver, registry: Any, shared_router: Any) -> None:
        self._resolver = resolver
        self._registry = registry
        self._shared_router = shared_router

    def get_offload_policy(self, model_id: str) -> OffloadPolicy | None:
        """Return the model's stored policy if its router applies one, else None."""
        policy = self._resolver.get_offload_policy(model_id)
        if policy is None:
            return None
        if self._registry.get_router_name(model_id) != "fixed":
            return None
        router = self._registry.get_cached_router(model_id)
        if router is not None and router is not self._shared_router:
            return None
        return policy


@dataclass(frozen=True, slots=True)
class OffloadRouteRecord:
    """A stored policy plus the audit fields of the row it was read from."""

    policy: OffloadPolicy
    updated_at: datetime | None = None
    updated_by: str | None = None


class OffloadRouteResolver:
    """Hold every model's offload policy for synchronous routing reads.

    Local writes and reloads are fenced by one generation counter, as
    ``RouteWiseSettingsResolver`` does: a reload publishes its result only if no
    admin write and no newer reload happened while its query was in flight, so a
    slow read can never resurrect a policy that was just cleared, or replace one
    that was just set. A fenced reload is simply dropped; the next one picks up
    whatever it missed.
    """

    def __init__(self, store: Any) -> None:
        self._store = store
        self._records: dict[str, OffloadRouteRecord] = {}
        self._generation = 0
        self._lock = RLock()

    async def load_all(self) -> bool:
        """Reload every policy and return whether the published snapshot changed."""
        with self._lock:
            self._generation += 1
            generation = self._generation

        rows = await self._store.list_settings()
        loaded: dict[str, OffloadRouteRecord] = {}
        for row in rows:
            model_id = model_id_from_offload_route_setting_key(str(row["key"]))
            if model_id is None:
                continue
            try:
                policy = decode_offload_policy(row.get("value"))
            except ValueError:
                logger.warning(
                    "Ignoring invalid offload route setting for model=%s",
                    model_id,
                    exc_info=True,
                )
                continue
            loaded[model_id] = OffloadRouteRecord(
                policy=policy,
                updated_at=row.get("updated_at"),
                updated_by=row.get("updated_by"),
            )

        with self._lock:
            if generation != self._generation:
                return False
            changed = loaded != self._records
            self._records = loaded
            return changed

    def get_offload_policy(self, model_id: str) -> OffloadPolicy | None:
        """Return the policy for one canonical model (sync snapshot read)."""
        with self._lock:
            record = self._records.get(model_id)
        return record.policy if record is not None else None

    def get_record(self, model_id: str) -> OffloadRouteRecord | None:
        """Return the stored policy and its audit fields for one model."""
        with self._lock:
            return self._records.get(model_id)

    def list_records(self) -> dict[str, OffloadRouteRecord]:
        """Return a copy of every model's stored policy."""
        with self._lock:
            return dict(self._records)

    def set_policy(
        self,
        model_id: str,
        policy: OffloadPolicy,
        *,
        updated_by: str | None = None,
        updated_at: datetime | None = None,
    ) -> None:
        """Publish a policy after a successful admin write."""
        with self._lock:
            self._generation += 1
            records = dict(self._records)
            records[model_id] = OffloadRouteRecord(
                policy=policy,
                updated_at=updated_at,
                updated_by=updated_by,
            )
            self._records = records

    def clear_model(self, model_id: str) -> None:
        """Drop one model's policy after an admin delete (or an ambiguous write)."""
        with self._lock:
            self._generation += 1
            if model_id in self._records:
                records = dict(self._records)
                records.pop(model_id, None)
                self._records = records
