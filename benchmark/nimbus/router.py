"""Experiment-local RouterProtocol implementation for greedy/knapsack baselines.

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

from benchmark.nimbus.baselines import (
    BaselineProfile,
    BaselineRequest,
    BaselineSnapshot,
    InflightPrefill,
    request_commitment_tokens,
    select_baseline,
)
from routing.backends import LeafBackend
from routing.dispatch import DispatchMismatchError
from routing.endpoints import endpoint_id_for_adapter
from routing.prefill_load import estimate_prefill_tokens
from routing.protocols import RoutingRequestOptions
from routing.route_scope import adapter_in_endpoint_scope
from routing.routers import TargetUnavailableError
from serving.utils import context as req_ctx
from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Collection

    from benchmark.nimbus.registry import BaselineParams
    from routing.endpoint_health import EndpointHealthRegistry
    from routing.route_table import RouteTableView
    from routing.routers import RoutingObservation

logger = get_logger(__name__)


@dataclass
class _Reservation:
    candidate: BaselineRequest
    phase: str = "waiting"
    first_output_s: float | None = None
    release_after_s: float | None = None


class BaselineLocalPool:
    """One FIFO queue, physical sequence-slot owner and peak-KV estimate.

    Estimated decode completion never releases an actual slot. Only observed
    completion, cancellation grace expiry, or an unsent cancellation does so.
    These frontend slots are not a claim about the engine's internal scheduler.
    """

    def __init__(self, endpoint_id: str, profile: BaselineProfile, max_inflight: int) -> None:
        self.endpoint_id = endpoint_id
        self.profile = profile
        self.max_inflight = max_inflight
        self.lock = asyncio.Lock()
        self._changed = asyncio.Condition(self.lock)
        self._reservations: dict[str, _Reservation] = {}

    def _snapshot_locked(self) -> BaselineSnapshot:
        now = time.monotonic()
        expired = [
            key
            for key, item in self._reservations.items()
            if item.release_after_s is not None and item.release_after_s <= now
        ]
        for key in expired:
            self._reservations.pop(key)
        if expired:
            self._changed.notify_all()
        waiting = []
        prefills = []
        decoding = []
        residence = max(0, self.profile.max_tokens - 1) * self.profile.tpot_s
        for item in self._reservations.values():
            if item.phase == "waiting":
                waiting.append(item.candidate)
            elif item.phase == "prefill":
                # Transport close does not acknowledge engine cancellation.
                # Keep its full work in the shared prefill lane during grace,
                # and hold its physical sequence slot for at least that grace.
                prefills.append(
                    InflightPrefill(
                        item.candidate.prompt_tokens,
                        self.profile.max_tokens,
                        slot_release_floor_s=max(0, item.release_after_s - now)
                        if item.release_after_s is not None
                        else 0,
                    )
                )
            else:
                assert item.first_output_s is not None
                decoding.append(
                    max(residence, item.release_after_s - now)
                    if item.release_after_s is not None
                    else residence
                )
        return BaselineSnapshot(
            reserved_tokens=sum(
                request_commitment_tokens(item.candidate, self.profile)
                for item in self._reservations.values()
            ),
            waiting=tuple(waiting),
            inflight_prefills=tuple(prefills),
            inflight_remaining_s=tuple(decoding),
            max_inflight=self.max_inflight,
        )

    async def snapshot(self) -> BaselineSnapshot:
        """Read current ownership, expiring elapsed cancellation grace."""
        async with self.lock:
            return self._snapshot_locked()

    async def acquire_dispatch(self, reservation_id: str) -> None:
        """Wait for the oldest accepted request and an actually free local slot."""
        async with self._changed:
            while True:
                snapshot = self._snapshot_locked()
                reservation = self._reservations.get(reservation_id)
                if reservation is None:
                    raise TargetUnavailableError("local reservation stopped before dispatch")
                if reservation.phase != "waiting":
                    raise RuntimeError("local reservation may dispatch only once")
                active = len(snapshot.inflight_prefills) + len(snapshot.inflight_remaining_s)
                if snapshot.waiting[0].request_id == reservation_id and active < self.max_inflight:
                    reservation.phase = "prefill"
                    self._changed.notify_all()
                    return
                expirations = [
                    item.release_after_s
                    for item in self._reservations.values()
                    if item.release_after_s is not None
                ]
                timeout = max(0, min(expirations) - time.monotonic()) if expirations else None
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._changed.wait(), timeout)

    async def first_token(self, reservation_id: str) -> None:
        """Observe prefill completion without releasing the slot or peak KV."""
        async with self.lock:
            reservation = self._reservations.get(reservation_id)
            if reservation is not None and reservation.first_output_s is None:
                reservation.phase = "decode"
                reservation.first_output_s = time.monotonic()

    async def release(self, reservation_id: str, *, grace_s: float = 0) -> None:
        """Release unsent/completed work, retaining dispatched uncertainty for grace."""
        async with self._changed:
            reservation = self._reservations.get(reservation_id)
            if reservation is not None and reservation.phase != "waiting" and grace_s > 0:
                if reservation.release_after_s is None:
                    reservation.release_after_s = time.monotonic() + grace_s
            else:
                self._reservations.pop(reservation_id, None)
            self._changed.notify_all()

    async def reject_waiting(self, reservation_ids: set[str]) -> None:
        """Reject one router's unsent work while preserving other and active owners."""
        async with self._changed:
            for identifier in reservation_ids:
                item = self._reservations.get(identifier)
                if item is not None and item.phase == "waiting":
                    self._reservations.pop(identifier)
            self._changed.notify_all()


class BaselinePoolRegistry:
    """Share one experiment-owned pool across model instances and aliases.

    Conflicting profiles, frontend slot counts, and endpoint aliases fail closed.
    This is deliberately single-process; no production capacity API is added.
    """

    def __init__(self) -> None:
        self._pools: dict[str, BaselineLocalPool] = {}
        self._endpoint_pools: dict[str, str] = {}

    def get_pool(
        self, pool_id: str, endpoint_id: str, profile: BaselineProfile, max_inflight: int
    ) -> BaselineLocalPool:
        """Resolve the sole owner or reject a conflicting pool declaration."""
        existing_id = self._endpoint_pools.get(endpoint_id)
        if existing_id is not None and existing_id != pool_id:
            raise ValueError("baseline local endpoint already belongs to a different pool")
        pool = self._pools.get(pool_id)
        if pool is not None:
            if (pool.endpoint_id, pool.profile, pool.max_inflight) != (
                endpoint_id,
                profile,
                max_inflight,
            ):
                raise ValueError(
                    "baseline pool endpoint/profile/slots conflict with its existing owner"
                )
            return pool
        pool = BaselineLocalPool(endpoint_id, profile, max_inflight)
        self._pools[pool_id] = pool
        self._endpoint_pools[endpoint_id] = pool_id
        return pool


def _has_generated_output(chunk: Any) -> bool:
    """Recognize text/reasoning/function deltas, excluding protocol metadata."""
    if isinstance(chunk, bytes):
        chunk = chunk.decode("utf-8", errors="replace")
    if not isinstance(chunk, str):
        return False
    data = "\n".join(line[5:].lstrip() for line in chunk.splitlines() if line.startswith("data:"))
    try:
        payload = json.loads(data)
    except (ValueError, TypeError):
        return False
    if not isinstance(payload, dict):
        return False
    for choice in payload.get("choices") or []:
        if not isinstance(choice, dict) or not isinstance(choice.get("delta"), dict):
            continue
        delta = choice["delta"]
        if any(
            isinstance(delta.get(key), str) and delta[key]
            for key in ("content", "reasoning_content", "reasoning", "thinking")
        ):
            return True
        for call in delta.get("tool_calls") or []:
            if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
                continue
            if any(
                isinstance(call["function"].get(key), str) and call["function"][key]
                for key in ("name", "arguments")
            ):
                return True
    return False


@dataclass
class _Pending:
    candidate: BaselineRequest
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


class BaselineRouter:
    """Experimental peer strategy with one atomic local-admission owner.

    It intentionally does not learn from completion labels yet: a fixed known
    generation cap and measured deployment profiles remain explicit inputs.
    Calls always execute their own bound adapter in the original caller context.
    """

    def __init__(
        self,
        params: BaselineParams,
        *,
        health_registry: EndpointHealthRegistry | None = None,
        baseline_pools: BaselinePoolRegistry | None = None,
        baseline_decision_sink: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.params = params
        self._health_registry = health_registry
        self._pools = baseline_pools
        self._sink = baseline_decision_sink
        self.route_table: RouteTableView | None = None
        self._model_scope: frozenset[str] = frozenset()
        self._pool: BaselineLocalPool | None = None
        self._pending: dict[str, _Pending] = {}
        self._waiting_local: set[str] = set()
        self._pending_lock = asyncio.Lock()
        self._batch_task: asyncio.Task[None] | None = None
        self._stopped = False
        self._started = False

    def attach_route_table(
        self, route_table: RouteTableView, *, model_scope: Collection[str]
    ) -> None:
        """Bind the standard route-table port to exactly one canonical model."""
        if self._pools is None:
            raise ValueError(
                "Baseline requires an explicitly shared BaselinePoolRegistry dependency"
            )
        scope = frozenset(route_table.canonical_id(model_id) for model_id in model_scope)
        if len(scope) != 1:
            raise ValueError("one Baseline router must own exactly one canonical model")
        self.route_table = route_table
        self._model_scope = scope
        self._leaves(next(iter(scope)))
        self._pool = self._pools.get_pool(
            self.params.pool_id,
            self.params.local_endpoint_id,
            self.params.profile,
            self.params.local_max_inflight,
        )

    def _leaves(self, model_id: str) -> tuple[LeafBackend, LeafBackend]:
        table = self.route_table
        if table is None:
            raise RuntimeError("Baseline route table is not attached")
        canonical = table.canonical_id(model_id)
        if canonical not in self._model_scope:
            raise ValueError("model is outside this Baseline router's scope")
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
                    f"configured Baseline endpoint {endpoint!r} is absent from model route"
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
            waiting_local = set(self._waiting_local)
            for item in pending:
                if not item.future.done():
                    item.future.set_exception(TargetUnavailableError("Baseline router stopped"))
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if waiting_local:
            assert self._pool is not None
            await self._pool.reject_waiting(waiting_local)
            self._waiting_local.difference_update(waiting_local)

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
                "Baseline bound endpoint requires a matching exact model/target"
            )
        if options.required_modalities:
            raise ValueError("experimental Baseline currently supports text-only requests")
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
                raise DispatchMismatchError("Baseline cannot replace its metered route binding")
            allowed.append(leaf if available else None)
        if options.preferred_endpoint_id is not None and not options.require_target:
            raise ValueError(
                "experimental Baseline requires an exact target for endpoint preferences"
            )
        if not any(allowed):
            raise TargetUnavailableError("Baseline request scope contains no configured endpoint")
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
            raise RuntimeError("Baseline has no shared local pool")
        now = time.monotonic()
        prompt_tokens = estimate_prefill_tokens(
            messages, tools=params.get("tools"), response_format=params.get("response_format")
        )
        output_limits = [params.get("max_tokens")]
        if "max_completion_tokens" in params:
            output_limits.append(params["max_completion_tokens"])
        if (
            any(
                isinstance(limit, bool)
                or not isinstance(limit, int)
                or not 1 <= limit <= self.params.profile.max_tokens
                for limit in output_limits
            )
            or len(set(output_limits)) != 1
        ):
            raise ValueError(
                "baseline requests require a positive output cap within profile.max_tokens"
            )
        reservation_id = uuid.uuid4().hex
        candidate = BaselineRequest(
            request_id=reservation_id,
            arrival_s=now,
            prompt_tokens=prompt_tokens,
            estimated_remote_cost=(
                prompt_tokens * self.params.remote_input_cost_per_million
                + self.params.profile.max_tokens * self.params.remote_output_cost_per_million
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
                raise TargetUnavailableError("Baseline router stopped")
            if len(self._pending) + len(self._waiting_local) >= self.params.max_pending:
                raise TargetUnavailableError("Baseline pending queue is full")
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
                self._waiting_local.discard(reservation_id)
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
                                TargetUnavailableError("Baseline batch interrupted before dispatch")
                            )
            if not isinstance(exc, Exception):
                raise
            logger.exception("baseline_batch_failed")

    def _select_batch_locked(self, batch: list[_Pending], pool: BaselineLocalPool) -> None:
        try:
            batch = [item for item in batch if not item.future.done()]
            started = time.monotonic()
            snapshot = pool._snapshot_locked()
            candidates = [item.candidate for item in batch if item.local is not None]
            current = snapshot
            decisions = {}
            selected_ids = []
            windows = []
            while candidates:
                selection = select_baseline(
                    self.params.policy,
                    candidates,
                    current,
                    self.params.profile,
                    started,
                    max_candidates=self.params.max_candidates,
                )
                deferred = set(selection.deferred_ids)
                decisions.update(
                    {
                        decision.request_id: decision
                        for decision in selection.decisions
                        if decision.request_id not in deferred
                    }
                )
                selected_ids.extend(selection.selected_ids)
                windows.append(
                    {
                        "candidate_ids": [
                            item.request_id
                            for item in candidates
                            if item.request_id not in deferred
                        ],
                        "selected_ids": list(selection.selected_ids),
                        "deferred_ids": list(selection.deferred_ids),
                        "saved_cost": selection.saved_cost,
                    }
                )
                current = selection.resulting_snapshot
                candidates = [item for item in candidates if item.request_id in deferred]
            finished = time.monotonic()
            prepared: list[tuple[_Pending, _Dispatch | None]] = []
            for item in batch:
                decision = decisions.get(item.candidate.request_id)
                is_local = decision is not None and decision.selected
                leaf = item.local if is_local else item.cloud
                record = {
                    "event": "baseline_decision",
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
                    "snapshot_proposed": asdict(current),
                    "profile": asdict(self.params.profile),
                    "selection_windows": windows,
                    "window_truncated": len(windows) > 1,
                    "max_candidates": self.params.max_candidates,
                    "endpoint_id": leaf.endpoint_id if leaf else None,
                    "route": "local" if is_local else "api" if leaf else "rejected",
                    "reason": decision.reason if decision is not None else "caller_cloud_only",
                    "feature_source": "repository_message_bytes_estimate_fixed_cap_first_output_only",
                    "cache_estimate": "all_uncached",
                    "cancel_grace_s": self.params.cancel_grace_s,
                }
                if self._sink is not None:
                    self._sink(record)
                else:
                    logger.info("baseline_decision", extra=record)
                dispatch = (
                    _Dispatch(leaf, item.candidate.request_id, is_local, record)
                    if leaf is not None
                    else None
                )
                prepared.append((item, dispatch))
            selection_order = {request_id: index for index, request_id in enumerate(selected_ids)}
            prepared.sort(
                key=lambda item: selection_order.get(
                    item[0].candidate.request_id, len(selection_order)
                )
            )
            for item, dispatch in prepared:
                if dispatch is None:
                    item.future.set_exception(
                        TargetUnavailableError(
                            "Baseline local admission rejected within caller scope"
                        )
                    )
                else:
                    if dispatch.local:
                        pool._reservations[dispatch.reservation_id] = _Reservation(item.candidate)
                        self._waiting_local.add(dispatch.reservation_id)
                    item.future.set_result(dispatch)
        except BaseException as exc:
            for item in batch:
                if not item.future.done():
                    item.future.set_exception(
                        TargetUnavailableError("Baseline batch interrupted before dispatch")
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
            "strategy_metadata": {"baseline_experiment": dispatch.metadata},
        }

    async def _release(self, dispatch: _Dispatch, *, dispatched: bool, completed: bool) -> None:
        if dispatch.local:
            assert self._pool is not None
            await self._pool.release(
                dispatch.reservation_id,
                grace_s=self.params.cancel_grace_s if dispatched and not completed else 0,
            )
            self._waiting_local.discard(dispatch.reservation_id)

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
        dispatched = False
        try:
            if dispatch.local:
                assert self._pool is not None
                await self._pool.acquire_dispatch(dispatch.reservation_id)
                self._waiting_local.discard(dispatch.reservation_id)
            dispatched = True
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
            await self._release(dispatch, dispatched=dispatched, completed=completed)

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
            if dispatch.local:
                assert self._pool is not None
                await self._pool.acquire_dispatch(dispatch.reservation_id)
                self._waiting_local.discard(dispatch.reservation_id)
            with req_ctx.push(model=model_id, provider=dispatch.leaf.config.provider):
                stream = aiter(dispatch.leaf.stream_chat_completion(messages, **params))
                dispatched = True
                first = True
                async for chunk in stream:
                    if first and _has_generated_output(chunk):
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
