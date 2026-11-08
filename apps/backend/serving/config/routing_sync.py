"""Synchronize committed Admin routing edits across backend processes.

The database records only changed model identifiers and counters. Existing
model transition helpers own publication and retirement; unrelated models keep
their router instances, learned state and requests in flight. The existing
instance reporter records whether this process applied its observed counters.
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
    _task: asyncio.Task | None = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def start(self) -> None:
        """Establish the boot baseline before admitting requests, then poll."""
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
                },
            )

    async def refresh(self) -> None:
        """Apply a coherent database snapshot using scoped existing transitions."""
        from serving.adapters import dynamic_keys, provider_registry
        from serving.servers.routers.admin.provider_definitions import (
            _config_managed_provider_names,
            _configured_provider_specs,
            config_route_provider_labels,
        )
        from serving.servers.routers.admin.provider_routes import reconcile_live_routes_from_store

        async with self._lock:
            try:
                async with (
                    self.pool.acquire() as conn,
                    conn.transaction(isolation="repeatable_read"),
                ):
                    self.checked, _ = await revisions.read_version(conn, "routing")
                    if self.checked is None:
                        raise RuntimeError("Routing source version is missing")
                    observed = {
                        row["model_id"]: int(row["revision"])
                        for row in await conn.fetch(
                            "SELECT model_id, revision FROM routing_config_revisions"
                        )
                    }
                    changed = {
                        key
                        for key in observed.keys() | self.applied.keys()
                        if observed.get(key) != self.applied.get(key)
                    }
                    if self.version is not None and self.version.epoch != self.checked.epoch:
                        # A restored database may have the same counters for
                        # different values. Reconcile every local model as well
                        # so a model removed by the restore is retired too.
                        changed |= observed.keys() | self.applied.keys()
                        changed |= set(self.services.router.routes) | {"@providers"}
                    if not changed:
                        self.version = self.checked
                        self._report("applied")
                        return
                    self._report("applying")
                    store = self.services.operational_store.on_connection(conn)
                    if "@providers" in changed:
                        specs = _configured_provider_specs()
                        await provider_registry.apply_provider_definitions_at_boot(
                            store,
                            reserved_providers=_config_managed_provider_names(specs),
                            config_label_providers=config_route_provider_labels(specs),
                        )
                        await dynamic_keys.apply_db_keys_at_boot(store)
                        await dynamic_keys.load_min_role_declarations(store)
                    for model_id in sorted(changed - {"@providers"}):
                        async with model_router_transition_lock(self.services, model_id):
                            await reconcile_live_routes_from_store(
                                self.services,
                                store,
                                model_ids={model_id},
                                strict=True,
                                lock_held=True,
                            )
                self.applied = observed
                self.version = self.checked
                self._report("applied")
            except Exception:
                self._report("failed", error="routing_apply_failed")
                logger.warning("Routing configuration refresh failed; retrying", exc_info=True)

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
