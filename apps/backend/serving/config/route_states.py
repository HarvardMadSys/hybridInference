"""Runtime resolver for per-route admin state: disabled and removed routes.

A route present in the snapshot is excluded from routing: the router zeroes its
weight, so weighted selection, every retry and fallback loop, the offload pick
and RouteWise's candidate set all skip it (each already gates on ``weight > 0``).
The route's configuration and weights are left as they are, so enabling it
again restores exactly what it had. A *removed* config route is also taken out
of the route table at boot; until then (another process, a failed table update)
the exclusion alone keeps it from being selected.

Routes are named by their route id (``routing.endpoints.route_id_for_adapter``),
which survives an edit that moves the endpoint. The snapshot is read
synchronously during routing, like :class:`DisabledProviderResolver`.
"""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
from typing import Any


@dataclass(frozen=True)
class RouteState:
    """One route's admin state; a route with neither flag has no state."""

    disabled: bool = False
    removed: bool = False

    @property
    def excluded(self) -> bool:
        """Whether routing must not select the route."""
        return self.disabled or self.removed


ENABLED = RouteState()


class RouteStateResolver:
    """Hold per-route admin state for synchronous routing reads."""

    def __init__(self, store: Any) -> None:
        self._store = store
        self._states: dict[tuple[str, str], RouteState] = {}
        self._lock = RLock()

    async def load_all(self) -> bool:
        """Reload every route state and return whether the snapshot changed."""
        rows = await self._store.list_all_provider_route_states()
        states = {
            (str(row["model_id"]), str(row["route_id"])): RouteState(
                disabled=bool(row["disabled"]),
                removed=bool(row["removed"]),
            )
            for row in rows
            if row["disabled"] or row["removed"]
        }
        with self._lock:
            changed = states != self._states
            self._states = states
            return changed

    def get(self, model_id: str, route_id: str) -> RouteState:
        """Return a route's state (enabled when it has none)."""
        with self._lock:
            return self._states.get((model_id, route_id), ENABLED)

    def is_excluded(self, model_id: str, route_id: str) -> bool:
        """Return whether routing must skip the route (sync snapshot)."""
        return self.get(model_id, route_id).excluded

    def removed_routes(self) -> list[tuple[str, str]]:
        """Return the (model id, route id) of every removed route."""
        with self._lock:
            return sorted(key for key, state in self._states.items() if state.removed)

    def set_state(self, model_id: str, route_id: str, state: RouteState) -> None:
        """Update the snapshot after a successful admin write."""
        with self._lock:
            if state.excluded:
                self._states[(model_id, route_id)] = state
            else:
                self._states.pop((model_id, route_id), None)

    def clear_model(self, model_id: str) -> None:
        """Forget every state of a removed model."""
        with self._lock:
            self._states = {key: value for key, value in self._states.items() if key[0] != model_id}
