"""Synchronize committed Admin routing edits across backend processes.

The database records only changed model identifiers and counters. Each changed
model is brought to its stored rows on its own, keeping what already matches:
unrelated models, unchanged routes and unchanged strategies keep their router
instances, learned state and requests in flight. A model that cannot be applied
is retried without holding back the others or the credential state, which every
refresh publishes last. The existing instance reporter records whether this
process applied its observed counters, and which models it could not.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from typing import Any

from serving.config import live, revisions
from serving.servers.deps import model_router_transition_lock
from serving.utils.logging import get_logger

logger = get_logger(__name__)
GROUP = "routing.runtime"
#: The counter that provider definitions, provider keys and env-key state share.
PROVIDERS = "@providers"
_REVISIONS_SQL = "SELECT model_id, revision FROM routing_config_revisions"
_MODEL_REVISION_SQL = "SELECT revision FROM routing_config_revisions WHERE model_id = $1"


@dataclass
class RoutingSynchronizer:
    """One serialized routing refresh loop per backend process."""

    services: Any
    pool: Any
    interval: float = 10.0
    applied: dict[str, int] = field(default_factory=dict)
    state: str = "applying"
    version: revisions.SourceVersion | None = None
    checked: revisions.SourceVersion | None = None
    # Models (and PROVIDERS) the last refresh could not apply: retried by the next.
    failed: set[str] = field(default_factory=set)
    _task: asyncio.Task | None = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def start(self) -> None:
        """Check what boot restored against the store before admitting requests, then poll."""
        await self.refresh()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Stop refreshing before the routing lifecycle shuts down."""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    def _report(self, state: str, *, error: str | None = None) -> None:
        self.state = state
        reporter = live.COORDINATOR.reporter
        if reporter is not None:
            reporter.set_group(
                GROUP,
                {
                    "state": state,
                    "source": "routing_config_revisions",
                    "applied_models": len(self.applied),
                    "epoch": self.checked.epoch if self.checked else None,
                    "checked_revision": self.checked.revision if self.checked else None,
                    "reconciled_revision": self.version.revision if self.version else None,
                    "error_code": error,
                    # Identifiers only, as the counters record them.
                    "error_keys": sorted(self.failed),
                },
            )

    def _scope(self, observed: dict[str, int]) -> set[str]:
        """Return what this refresh reconciles: changed counters and earlier failures."""
        scope = {
            key
            for key in observed.keys() | self.applied.keys()
            if observed.get(key) != self.applied.get(key)
        }
        scope |= self.failed
        if self.version is None:
            # Boot restored this state from the store a moment ago. Every model
            # the store names is checked against it; what matches is kept.
            scope |= observed.keys() | {PROVIDERS}
        elif self.checked is not None and self.version.epoch != self.checked.epoch:
            # A restored database may have the same counters for different
            # values. Reconcile every local model as well, so a model removed
            # by the restore is retired too.
            scope |= observed.keys() | self.applied.keys() | {PROVIDERS}
            scope |= set(self.services.router.routes)
        elif PROVIDERS in scope:
            # A key or provider change can make a route that could not be
            # built buildable (a re-enabled key), or the reverse.
            scope |= observed.keys()
        return scope

    async def refresh(self) -> None:
        """Apply a coherent database snapshot, one model at a time."""
        async with self._lock:
            try:
                async with (
                    self.pool.acquire() as conn,
                    conn.transaction(isolation="repeatable_read"),
                ):
                    checked, _ = await revisions.read_version(conn, "routing")
                    if checked is None:
                        raise RuntimeError("Routing source version is missing")
                    self.checked = checked
                    observed = {
                        row["model_id"]: int(row["revision"])
                        for row in await conn.fetch(_REVISIONS_SQL)
                    }
                    scope = self._scope(observed)
                    if not scope:
                        self.version = checked
                        self._report("applied")
                        return
                    self._report("applying")
                    store = self.services.operational_store.on_connection(conn)
                    failed, deferred = await self._apply(store, observed, scope)
            except Exception:
                self._report("failed", error="routing_apply_failed")
                logger.warning("Routing configuration refresh failed; retrying", exc_info=True)
                return
            for key in scope - failed - deferred:
                if key in observed:
                    self.applied[key] = observed[key]
                else:
                    self.applied.pop(key, None)
            self.failed = failed
            self.version = checked
            if failed:
                self._report("failed", error="routing_apply_failed")
            else:
                self._report("applying" if deferred else "applied")

    async def _apply(
        self, store: Any, observed: dict[str, int], scope: set[str]
    ) -> tuple[set[str], set[str]]:
        """Apply provider definitions, each model in *scope* on its own, then credentials.

        Returns the keys that failed, to retry, and the models left to the next
        refresh because they changed after this snapshot was taken.
        """
        from serving.adapters import dynamic_keys, provider_registry
        from serving.servers.routers.admin.provider_definitions import (
            _config_managed_provider_names,
            _configured_provider_specs,
            config_route_provider_labels,
        )
        from serving.servers.routers.admin.provider_routes import (
            read_stored_model_routes,
            reconcile_model_from_store,
        )

        failed: set[str] = set()
        deferred: set[str] = set()
        models = sorted(scope - {PROVIDERS})
        if PROVIDERS in scope:
            try:
                specs = _configured_provider_specs()
                await provider_registry.apply_provider_definitions_at_boot(
                    store,
                    reserved_providers=_config_managed_provider_names(specs),
                    config_label_providers=config_route_provider_labels(specs),
                    remove_missing=True,
                )
            except Exception:
                failed.add(PROVIDERS)
                logger.warning("Provider definitions refresh failed; retrying", exc_info=True)
        stored = None
        if models and PROVIDERS not in failed:
            try:
                stored = await read_stored_model_routes(store)
            except Exception:
                logger.warning("Routing rows could not be read; retrying", exc_info=True)
        if stored is None:
            # Without the providers or the rows, a route could look unbuildable
            # and be dropped; every model waits for the next refresh instead.
            failed.update(models)
            models = []
        for model_id in models:
            try:
                async with model_router_transition_lock(self.services, model_id):
                    if await self._changed_since(model_id, observed):
                        deferred.add(model_id)
                        continue
                    await reconcile_model_from_store(
                        self.services, store, model_id, stored.get(model_id)
                    )
            except Exception:
                failed.add(model_id)
                logger.warning(
                    "Routing configuration for model=%s could not be applied; retrying",
                    model_id,
                    exc_info=True,
                )
        # Credential state last, and whatever failed above: a route rebuilt from
        # this snapshot cannot outlive a revocation in it, and no model that
        # cannot be applied holds a revocation back.
        try:
            await dynamic_keys.reconcile_db_keys(store)
        except Exception:
            failed.add(PROVIDERS)
            logger.warning("Provider key refresh failed; retrying", exc_info=True)
        return failed, deferred

    async def _changed_since(self, model_id: str, observed: dict[str, int]) -> bool:
        """Whether *model_id* changed after this refresh's snapshot was taken.

        Read outside the snapshot, under the model's transition lock. An edit
        made through this process has by then installed what it committed, and
        applying the older rows would undo it until the next refresh, which
        applies the newer ones instead.
        """
        async with self.pool.acquire() as conn:
            revision = await conn.fetchval(_MODEL_REVISION_SQL, model_id)
        return revision is not None and int(revision) != observed.get(model_id)

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            await self.refresh()


async def status_group(store: Any) -> dict[str, Any]:
    """Return the routing source's version and registered-process acknowledgements."""
    from serving.config import instances

    async with (
        store.connection() as conn,
        conn.transaction(isolation="repeatable_read", readonly=True),
    ):
        version, _ = await revisions.read_version(conn, "routing")
        found = await instances.fetch_instances(conn)
    return {
        "name": GROUP,
        "keys": [],
        "schema_version": 1,
        "source": "routing",
        "version": version.as_dict() if version else None,
        "contract": {"active": True, "schema_version": 1},
        "coverage": instances.coverage(
            found,
            group=GROUP,
            schema_version=1,
            epoch=version.epoch if version else None,
            revision=version.revision if version else None,
        ),
        "instances": [
            {
                "instance_id": item["instance_id"],
                "build": item["build"],
                "lifecycle": item["lifecycle"],
                "started_at": item["started_at"],
                "heartbeat_age_seconds": item["heartbeat_age_seconds"],
                "stale": item["stale"],
                "supports": instances.supports(item["capabilities"], GROUP, 1),
                "status": (item["groups"] or {}).get(GROUP),
            }
            for item in found
        ],
    }
