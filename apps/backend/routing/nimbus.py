"""Opt-in Nimbus RouterProtocol implementation for controlled experiments.

Requests use the normal model registry and bound LeafBackend execution path.
Only arrived, unsent calls are batched. This module never owns credentials,
retries, fallback, provider billing, or the HTTP serving surface. Cloud adapters
must be metered at the experiment composition root, with hidden retries disabled.

The pool ledger is a single-process reservation estimate, not engine telemetry.
An unsuccessful dispatched local call retains its estimate for a configurable
grace after closing the transport; that grace is not cancellation acknowledgement.
Experiment boundaries must separately verify the local engine is idle.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from routing.backends import LeafBackend
from routing.dispatch import DispatchMismatchError
from routing.endpoints import endpoint_id_for_adapter
from routing.nimbus_policy import Candidate, CapacitySnapshot, LocalProfile, select_requests
from routing.prefill_load import estimate_prefill_tokens
from routing.protocols import RoutingRequestOptions
from routing.route_scope import adapter_in_endpoint_scope
from routing.routers import TargetUnavailableError
from routing.streaming import has_non_empty_content
from serving.utils import context as req_ctx
from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Collection

    from routing.endpoint_health import EndpointHealthRegistry
    from routing.route_table import RouteTableView
    from routing.routers import RoutingObservation
    from routing.strategies.nimbus import NimbusParams

logger = get_logger(__name__)


@dataclass
class _Reservation:
    candidate: Candidate
    prefill_done: bool = False
    release_after_s: float | None = None


class NimbusLocalPool:
    """Single owner of reservation estimates for one explicitly identified pool."""

    def __init__(self, endpoint_id: str, profile: LocalProfile) -> None:
        self.endpoint_id = endpoint_id
        self.profile = profile
        self.lock = asyncio.Lock()
        self._reservations: dict[str, _Reservation] = {}

    def _snapshot_locked(self) -> CapacitySnapshot:
        now = time.monotonic()
        self._reservations = {
            key: reservation
            for key, reservation in self._reservations.items()
            if reservation.release_after_s is None or reservation.release_after_s > now
        }
        values = tuple(self._reservations.values())
        return CapacitySnapshot(
            running_requests=len(values),
            reserved_tokens=sum(
                item.candidate.prompt_tokens + item.candidate.estimated_output_tokens
                for item in values
            ),
            remaining_prefill_tokens=sum(
                item.candidate.uncached_prefill_tokens for item in values if not item.prefill_done
            ),
            remaining_decode_tokens=sum(item.candidate.estimated_output_tokens for item in values),
        )

    async def snapshot(self) -> CapacitySnapshot:
        """Read current estimated reservations, expiring elapsed cancellation grace."""
        async with self.lock:
            return self._snapshot_locked()

    async def first_token(self, reservation_id: str) -> None:
        """Retain full context/decode reservation while dropping completed prefill."""
        async with self.lock:
            reservation = self._reservations.get(reservation_id)
            if reservation is not None:
                reservation.prefill_done = True

    async def release(self, reservation_id: str, *, grace_s: float = 0) -> None:
        """Release idempotently, or retain conservatively after uncertain cancellation."""
        async with self.lock:
            reservation = self._reservations.get(reservation_id)
            if reservation is not None and grace_s > 0:
                if reservation.release_after_s is None:
                    reservation.release_after_s = time.monotonic() + grace_s
            else:
                self._reservations.pop(reservation_id, None)


class NimbusPoolRegistry:
    """Composition-owned pool registry shared across model instances and aliases.

    A different profile or endpoint cannot silently create extra capacity under
    the same pool name. Distinct pool names cannot wrap the same endpoint either.
    This is not a multi-worker or multi-process capacity service.
    """

    def __init__(self) -> None:
        self._pools: dict[str, NimbusLocalPool] = {}
        self._endpoint_pools: dict[str, str] = {}

    def get_pool(self, pool_id: str, endpoint_id: str, profile: LocalProfile) -> NimbusLocalPool:
        """Resolve the sole owner or reject a conflicting pool declaration."""
        existing_id = self._endpoint_pools.get(endpoint_id)
        if existing_id is not None and existing_id != pool_id:
            raise ValueError("Nimbus local endpoint already belongs to a different pool")
        pool = self._pools.get(pool_id)
        if pool is not None:
            if pool.endpoint_id != endpoint_id or pool.profile != profile:
                raise ValueError("Nimbus pool endpoint/profile conflicts with its existing owner")
            return pool
        pool = NimbusLocalPool(endpoint_id, profile)
        self._pools[pool_id] = pool
        self._endpoint_pools[endpoint_id] = pool_id
        return pool


@dataclass
class _Pending:
    candidate: Candidate
    request_id: str
    future: asyncio.Future[_Dispatch]
    local: LeafBackend | None
    cloud: LeafBackend | None


@dataclass(frozen=True)
class _Dispatch:
    leaf: LeafBackend
    reservation_id: str
    local: bool
    metadata: dict[str, Any]


class NimbusRouter:
    """Experimental peer strategy with one atomic local-admission owner.

    It intentionally does not learn from completion labels yet: fixed online
    output estimates and measured deployment profiles remain explicit inputs.
    Calls always execute their own bound adapter in the original caller context.
    """

    def __init__(
        self,
        params: NimbusParams,
        *,
        health_registry: EndpointHealthRegistry | None = None,
        nimbus_pools: NimbusPoolRegistry | None = None,
        nimbus_decision_sink: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.params = params
        self._health_registry = health_registry
        self._pools = nimbus_pools
        self._sink = nimbus_decision_sink
        self.route_table: RouteTableView | None = None
        self._model_scope: frozenset[str] = frozenset()
        self._pool: NimbusLocalPool | None = None
        self._pending: dict[str, _Pending] = {}
        self._pending_lock = asyncio.Lock()
        self._batch_task: asyncio.Task[None] | None = None
        self._stopped = False
        self._started = False

    def attach_route_table(
        self, route_table: RouteTableView, *, model_scope: Collection[str]
    ) -> None:
        """Bind the standard route-table port to exactly one canonical model."""
        if self._pools is None:
            raise ValueError("Nimbus requires an explicitly shared NimbusPoolRegistry dependency")
        scope = frozenset(route_table.canonical_id(model_id) for model_id in model_scope)
        if len(scope) != 1:
            raise ValueError("one Nimbus router must own exactly one canonical model")
        self.route_table = route_table
        self._model_scope = scope
        self._leaves(next(iter(scope)))
        self._pool = self._pools.get_pool(
            self.params.pool_id, self.params.local_endpoint_id, self.params.profile
        )

    def _leaves(self, model_id: str) -> tuple[LeafBackend, LeafBackend]:
        table = self.route_table
        if table is None:
            raise RuntimeError("Nimbus route table is not attached")
        canonical = table.canonical_id(model_id)
        if canonical not in self._model_scope:
            raise ValueError("model is outside this Nimbus router's scope")
        adapters = {
            endpoint_id_for_adapter(adapter): adapter
            for route in table.iter_effective_routes()
            if route.canonical_model_id == canonical
            for adapter, _weight in route.adapters
        }
        leaves = []
        for endpoint in (self.params.local_endpoint_id, self.params.cloud_endpoint_id):
            if endpoint not in adapters:
                raise ValueError(
                    f"configured Nimbus endpoint {endpoint!r} is absent from model route"
                )
            leaves.append(
                LeafBackend(
                    adapters[endpoint],
                    endpoint_id=endpoint,
                    model_id=canonical,
                    pool_id=self.params.pool_id
                    if endpoint == self.params.local_endpoint_id
                    else None,
                )
            )
        return leaves[0], leaves[1]

    def refresh_route_table(self) -> None:
        """Validate refreshed routes; already pending calls retain their bindings."""
        if self._model_scope:
            self._leaves(next(iter(self._model_scope)))

    async def start(self) -> bool:
        """Enable lazy batching without creating background polling work."""
        was_started = self._started
        self._started = True
        self._stopped = False
        return not was_started

    async def stop(self) -> None:
        """Reject pending work; active calls keep their original execution owner."""
        async with self._pending_lock:
            self._stopped = True
            self._started = False
            task = self._batch_task
            self._batch_task = None
            pending = list(self._pending.values())
            self._pending.clear()
            for item in pending:
                if not item.future.done():
                    item.future.set_exception(TargetUnavailableError("Nimbus router stopped"))
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def _allowed_leaves(
        self, model_id: str, options: RoutingRequestOptions
    ) -> tuple[LeafBackend | None, LeafBackend | None]:
        local, cloud = self._leaves(model_id)
        if options.bound_endpoint is not None and (
            not options.require_target
            or options.preferred_endpoint_id != options.bound_endpoint.endpoint_id
            or options.bound_endpoint.model_id != local.model_id
        ):
            raise DispatchMismatchError(
                "Nimbus bound endpoint requires a matching exact model/target"
            )
        if options.required_modalities:
            raise ValueError("experimental Nimbus currently supports text-only requests")
        allowed = []
        for leaf in (local, cloud):
            available = options.endpoint_scope is None or adapter_in_endpoint_scope(
                leaf.adapter, options.endpoint_scope
            )
            if options.pin_provider is not None:
                available = available and options.pin_provider in (
                    leaf.config.provider,
                    leaf.endpoint_id,
                )
            if options.require_target:
                available = available and leaf.endpoint_id == options.preferred_endpoint_id
            if (
                options.bound_endpoint is not None
                and available
                and options.bound_endpoint.endpoint_id == leaf.endpoint_id
                and options.bound_endpoint.adapter is not leaf.adapter
            ):
                raise DispatchMismatchError("Nimbus cannot replace its metered route binding")
            allowed.append(leaf if available else None)
        if options.preferred_endpoint_id is not None and not options.require_target:
            raise ValueError(
                "experimental Nimbus requires an exact target for endpoint preferences"
            )
        if not any(allowed):
            raise TargetUnavailableError("Nimbus request scope contains no configured endpoint")
        return allowed[0], allowed[1]

    async def _admit(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        options: RoutingRequestOptions,
        params: dict[str, Any],
    ) -> _Dispatch:
        local, cloud = self._allowed_leaves(model_id, options)
        pool = self._pool
        if pool is None:
            raise RuntimeError("Nimbus has no shared local pool")
        now = time.monotonic()
        prompt_tokens = estimate_prefill_tokens(
            messages, tools=params.get("tools"), response_format=params.get("response_format")
        )
        output_limit = params.get("max_completion_tokens", params.get("max_tokens"))
        output_estimate = self.params.estimated_output_tokens
        if output_limit is not None:
            if (
                isinstance(output_limit, bool)
                or not isinstance(output_limit, int)
                or output_limit < 1
            ):
                raise ValueError("Nimbus requires a positive integer output-token cap")
            output_estimate = min(output_estimate, output_limit)
        reservation_id = uuid.uuid4().hex
        candidate = Candidate(
            request_id=reservation_id,
            arrival_s=now,
            deadline_s=now + self.params.ttft_slo_s,
            prompt_tokens=prompt_tokens,
            uncached_prefill_tokens=prompt_tokens,
            estimated_output_tokens=output_estimate,
            estimated_remote_cost=(
                prompt_tokens * self.params.remote_input_cost_per_million
                + output_estimate * self.params.remote_output_cost_per_million
            )
            / 1_000_000,
        )
        item = _Pending(
            candidate=candidate,
            request_id=str(
                req_ctx.get().get("request_id") or params.get("request_id") or reservation_id
            ),
            future=asyncio.get_running_loop().create_future(),
            local=local,
            cloud=cloud,
        )
        async with self._pending_lock:
            if self._stopped:
                raise TargetUnavailableError("Nimbus router stopped")
            if len(self._pending) >= self.params.max_pending:
                raise TargetUnavailableError("Nimbus pending queue is full")
            self._pending[reservation_id] = item
            if self._batch_task is None:
                self._batch_task = asyncio.create_task(self._flush())
        try:
            return await item.future
        except BaseException:
            async with self._pending_lock:
                self._pending.pop(reservation_id, None)
            if (
                item.future.done()
                and not item.future.cancelled()
                and item.future.exception() is None
                and item.future.result().local
            ):
                await pool.release(reservation_id)
            raise

    async def _flush(self) -> None:
        try:
            await asyncio.sleep(self.params.batch_window_s)
            pool = self._pool
            assert pool is not None
            # Keep the queue and task visible to stop() while waiting on a pool
            # shared with another router. Selection/commit below has no await.
            async with pool.lock, self._pending_lock:
                batch = [item for item in self._pending.values() if not item.future.done()]
                self._pending.clear()
                self._batch_task = None
                self._select_batch_locked(batch, pool)
        except BaseException as exc:
            async with self._pending_lock:
                if self._batch_task is asyncio.current_task():
                    pending = list(self._pending.values())
                    self._pending.clear()
                    self._batch_task = None
                    for item in pending:
                        if not item.future.done():
                            item.future.set_exception(
                                TargetUnavailableError("Nimbus batch interrupted before dispatch")
                            )
            if not isinstance(exc, Exception):
                raise
            logger.exception("nimbus_batch_failed")

    def _select_batch_locked(self, batch: list[_Pending], pool: NimbusLocalPool) -> None:
        try:
            batch = [item for item in batch if not item.future.done()]
            started = time.monotonic()
            snapshot = pool._snapshot_locked()
            candidates = [item.candidate for item in batch if item.local is not None]
            selection = select_requests(
                self.params.policy, candidates, snapshot, self.params.profile, started
            )
            decisions = {decision.request_id: decision for decision in selection.decisions}
            finished = time.monotonic()
            prepared: list[tuple[_Pending, _Dispatch | None]] = []
            for item in batch:
                decision = decisions.get(item.candidate.request_id)
                is_local = decision is not None and decision.selected
                leaf = item.local if is_local else item.cloud
                record = {
                    "event": "nimbus_decision",
                    "request_id": item.request_id,
                    "reservation_id": item.candidate.request_id,
                    "policy": self.params.policy,
                    "pool_id": self.params.pool_id,
                    "decision_start_s": started,
                    "decision_end_s": finished,
                    "gateway_wait_s": started - item.candidate.arrival_s,
                    "candidate": asdict(item.candidate),
                    "prediction": asdict(decision) if decision is not None else None,
                    "snapshot_before": asdict(snapshot),
                    "snapshot_proposed": asdict(selection.resulting_snapshot),
                    "profile": asdict(self.params.profile),
                    "search_truncated": selection.search_truncated,
                    "endpoint_id": leaf.endpoint_id if leaf else None,
                    "route": "local" if is_local else "api" if leaf else "rejected",
                    "reason": decision.reason if decision is not None else "caller_cloud_only",
                    "feature_source": "message_bytes_estimate_and_configured_output_estimate",
                    "cache_estimate": "all_uncached",
                    "cancel_grace_s": self.params.cancel_grace_s,
                }
                if self._sink is not None:
                    self._sink(record)
                else:
                    logger.info("nimbus_decision", extra=record)
                dispatch = (
                    _Dispatch(leaf, item.candidate.request_id, is_local, record)
                    if leaf is not None
                    else None
                )
                prepared.append((item, dispatch))
            selection_order = {
                request_id: index for index, request_id in enumerate(selection.selected_ids)
            }
            prepared.sort(
                key=lambda item: selection_order.get(
                    item[0].candidate.request_id, len(selection_order)
                )
            )
            for item, dispatch in prepared:
                if dispatch is None:
                    item.future.set_exception(
                        TargetUnavailableError(
                            "Nimbus local admission rejected within caller scope"
                        )
                    )
                else:
                    if dispatch.local:
                        pool._reservations[dispatch.reservation_id] = _Reservation(item.candidate)
                    item.future.set_result(dispatch)
        except BaseException as exc:
            for item in batch:
                if not item.future.done():
                    item.future.set_exception(
                        TargetUnavailableError("Nimbus batch interrupted before dispatch")
                        if isinstance(exc, asyncio.CancelledError)
                        else exc
                    )
            if not isinstance(exc, Exception):
                raise

    @staticmethod
    def _routing(dispatch: _Dispatch) -> dict[str, Any]:
        return {
            "provider": dispatch.leaf.config.provider,
            "base_url": dispatch.leaf.config.base_url,
            "endpoint_id": dispatch.leaf.endpoint_id,
            "strategy_metadata": {"nimbus": dispatch.metadata},
        }

    async def _release(self, dispatch: _Dispatch, *, dispatched: bool, completed: bool) -> None:
        if dispatch.local:
            assert self._pool is not None
            await self._pool.release(
                dispatch.reservation_id,
                grace_s=self.params.cancel_grace_s if dispatched and not completed else 0,
            )

    async def chat_completion(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        *,
        routing_options: RoutingRequestOptions | None = None,
        **params: Any,
    ) -> dict[str, Any]:
        """Admit once and execute one bound adapter with no retry or fallback."""
        dispatch = await self._admit(
            model_id, messages, routing_options or RoutingRequestOptions(), params
        )
        completed = False
        try:
            with req_ctx.push(model=model_id, provider=dispatch.leaf.config.provider):
                response = await dispatch.leaf.chat_completion(messages, **params)
            completed = True
            response = dict(response)
            response["_routing"] = {**response.get("_routing", {}), **self._routing(dispatch)}
            return response
        except Exception as exc:
            exc._routing = self._routing(dispatch)  # type: ignore[attr-defined]
            raise
        finally:
            await self._release(dispatch, dispatched=True, completed=completed)

    async def stream_chat_completion(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        *,
        routing_options: RoutingRequestOptions | None = None,
        **params: Any,
    ) -> AsyncIterator[Any]:
        """Preserve one provider stream; close its iterator before capacity cleanup."""
        dispatch = await self._admit(
            model_id, messages, routing_options or RoutingRequestOptions(), params
        )
        completed = False
        dispatched = False
        stream = None
        try:
            yield f"data: {json.dumps({'choices': [], '_routing': self._routing(dispatch)})}\n\n"
            with req_ctx.push(model=model_id, provider=dispatch.leaf.config.provider):
                stream = aiter(dispatch.leaf.stream_chat_completion(messages, **params))
                dispatched = True
                first = True
                async for chunk in stream:
                    if first and has_non_empty_content(chunk):
                        first = False
                        if dispatch.local:
                            assert self._pool is not None
                            await self._pool.first_token(dispatch.reservation_id)
                    yield chunk
                completed = True
        except Exception as exc:
            exc._routing = self._routing(dispatch)  # type: ignore[attr-defined]
            raise
        finally:
            try:
                close = getattr(stream, "aclose", None)
                if close is not None:
                    await close()
            finally:
                await self._release(dispatch, dispatched=dispatched, completed=completed)

    def record_observation(self, obs: RoutingObservation) -> None:
        """Keep provider feedback with the execution owner; no online learning yet."""

    def get_provider_status(self) -> dict[str, dict[str, Any]]:
        """Expose only bound endpoints from shared read-only health snapshots."""
        statuses = self._health_registry.snapshot() if self._health_registry is not None else {}
        endpoints = {self.params.local_endpoint_id, self.params.cloud_endpoint_id}
        return {key: value for key, value in statuses.items() if key in endpoints}
