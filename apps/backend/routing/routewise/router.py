"""Current RouteWise body router for FreeInference.

The production ``routewise`` strategy now follows the current RouteWise
paper/simulator body-routing semantics:

1. Convert every feasible provider to one effective cost.
2. Solve a cost-budgeted mean-TTFT LP over all feasible providers.
3. Sample one primary provider from the sparse LP mixture.

When ``latency_hedge_mode="probability_target"``, the router may wrap the
selected primary in a delayed ``HedgedAdapter`` using RouteWise checkpoint
probability math. Prefix-cache hits are observed by default without changing
routing. When the guarded cost-adjustment flag is enabled, API candidates may
use the session-scoped prefix estimate as part of their effective cost before
the LP; the ``L/U`` envelope and actual billing remain driven by observed cost.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import random
import threading
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from routewise.core import (
    HEDGE_SUCCESS_TARGET,
    BackupCandidate,
    CheckpointBackupDispatch,
    combined_success_probability,
    hedge_checkpoints_for_slo,
    select_probability_backup,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterable, Mapping

    from serving.adapters.base import BaseAdapter

    from .config import RouteWiseConfig

from routing.routers import BaseRouter, RoutingObservation
from serving.utils import context as req_ctx
from serving.utils.logging import get_logger
from serving.utils.tokens import estimate_prompt_tokens

from .candidates import (
    CandidatePricing,
    ConcurrencyPolicy,
    ProviderCandidate as RouteProviderCandidate,
    ProviderType,
    QuotaPolicy,
    QuotaSource,
    build_provider_candidates,
    endpoint_id_for_adapter,
)
from .concurrency import ConcurrencyManager
from .effective_cost import api_request_cost_usd, quota_shadow_price_usd
from .envelope import (
    CostEnvelopeEstimator,
    CostEnvelopeSnapshot,
    EnvelopeNotCalibratedError,
)
from .hedging import HedgedAdapter
from .latency import ProviderProfile
from .lp import LPCandidate, LPSolution, solve_cost_budgeted_mean_ttft
from .predictor import BucketMeanOutputPredictor, BucketMeanPrediction
from .prefix_cache import PrefixCacheCoordinator, price_delta_per_token
from .quota import SnapshotQuotaPool
from .quota_snapshot import ProviderQuotaSnapshotStore

logger = get_logger(__name__)


PENDING_DECISIONS_TTL_SECONDS: float = 300.0
PENDING_DECISIONS_SWEEP_INTERVAL_SECONDS: float = 60.0
PROBABILITY_TARGET_HEDGE_MODE: str = "probability_target"
RATE_LIMIT_ERROR_PENALTY_MS: float = 60_000.0
_PREFIX_CACHE_PENDING_MAX: int = 10_000
_WORKER_COUNT_ENV_KEYS = (
    "WEB_CONCURRENCY",
    "UVICORN_WORKERS",
    "GUNICORN_WORKERS",
)


@dataclass(frozen=True)
class FeasibleProviderCandidate:
    """Internal feasible-provider representation used by the provider-mixer LP."""

    endpoint_id: str
    adapter: BaseAdapter
    provider_type: Literal["on_demand", "quota", "concurrency"]
    weight: float
    effective_cost_usd: float
    request_cost_usd: float
    mean_ttft_sec: float
    cost_reason: str
    prefix_cache_discount_usd: float = 0.0
    prefix_cache_expected_tokens: float = 0.0
    prefix_cache_adjustment_applied: bool = False
    quota_pool: str | None = None
    concurrency_pool: str | None = None
    quota_used_fraction: float | None = None
    quota_remaining: int | None = None


@dataclass(frozen=True)
class HedgePlan:
    """In-flight checkpoint hedge schedule for one primary dispatch."""

    checkpoints_sec: tuple[float, ...]


@dataclass
class ProviderReservation:
    """Resource reservation for one concrete provider dispatch."""

    router: RouteWiseRouter
    candidate: FeasibleProviderCandidate
    acquired: bool = False

    def acquire(self) -> bool:
        """Acquire quota or concurrency for the candidate if needed."""
        if self.acquired:
            return True
        self.acquired = self.router._commit_candidate(self.candidate)
        return self.acquired

    def release(self) -> None:
        """Release a previously acquired reservation."""
        if not self.acquired:
            return
        self.router._release_candidate(self.candidate)
        self.acquired = False


def _dedupe_failed_attempts(attempts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str | None, str | None, str | None]] = set()
    result: list[dict[str, Any]] = []
    for attempt in attempts:
        endpoint = attempt.get("endpoint_id") or attempt.get("base_url") or attempt.get("provider")
        key = (
            endpoint if isinstance(endpoint, str) else None,
            attempt.get("error_type") if isinstance(attempt.get("error_type"), str) else None,
            attempt.get("error") if isinstance(attempt.get("error"), str) else None,
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(attempt)
    return result


def _configured_worker_count() -> int | None:
    """Best-effort detection for common ASGI worker-count environment vars."""
    for key in _WORKER_COUNT_ENV_KEYS:
        raw = os.getenv(key)
        if raw is None:
            continue
        try:
            count = int(raw)
        except ValueError:
            continue
        if count > 0:
            return count
    return None


class RouteWiseRouter(BaseRouter):
    """RouteWise current body router.

    ``fixed_router.routes`` remains the source of model -> adapter mappings;
    this router only changes the selection policy for models configured with
    ``router: routewise``.
    """

    def __init__(
        self,
        fixed_router: Any = None,
        config: RouteWiseConfig | None = None,
        params: Any = None,
    ) -> None:
        super().__init__()

        if config is None and params is not None:
            from .config import RouteWiseConfig as _RWC

            config = _RWC(**params.model_dump())
        if config is None:
            from .config import RouteWiseConfig as _RWC

            config = _RWC()

        self.fixed_router = fixed_router
        self.config = config
        self._rng = random.Random(self.config.random_seed)
        self.reference_api_price = self._parse_reference_api_price(config.reference_api_price)
        self.route_candidates: dict[str, list[RouteProviderCandidate]] = {}
        self._model_routewise_pools: dict[str, str] = {}
        self.classified: dict[str, list[tuple[Any, float, ProviderType]]] = {}
        self._adapter_provider_type: dict[int, ProviderType] = {}
        self._adapter_endpoint_ids: dict[int, str] = {}
        self._endpoint_adapter: dict[str, Any] = {}

        self.predictor = BucketMeanOutputPredictor(
            default_output=self.config.output_default_tokens,
            min_bucket_samples=self.config.output_min_bucket_samples,
            min_model_samples=self.config.output_min_model_samples,
            min_global_samples=self.config.output_min_global_samples,
        )
        self.envelope = CostEnvelopeEstimator(
            lower_percentile=self.config.envelope_lower_percentile,
            upper_percentile=self.config.envelope_upper_percentile,
            window_sec=self.config.shadow_price_window_hours * 3600.0,
            min_samples=self.config.envelope_min_samples,
        )
        self.quota_snapshots = ProviderQuotaSnapshotStore()
        # One resource manager per pool id, built from route-level policies.
        self.quota_pools: dict[str, SnapshotQuotaPool] = {}
        self.concurrency_pools: dict[str, ConcurrencyManager] = {}
        self._endpoint_concurrency_pool: dict[str, str] = {}
        self.prefix_cache = PrefixCacheCoordinator(
            enabled=self.config.prefix_cache_cost_adjustment_enabled,
        )

        self._latency_profiles: dict[str, ProviderProfile] = {}
        self._pending_decisions: dict[str, dict[str, Any]] = {}
        self._prefix_cache_pending: dict[str, tuple[Any, dict[str, Any]]] = {}
        self._primary_reservations: dict[str, ProviderReservation] = {}
        self._route_commit_lock = threading.RLock()
        self._sweep_task: asyncio.Task[None] | None = None
        self._quota_refresh_task: asyncio.Task[None] | None = None
        # Last LP state retained for tests and diagnostics.
        self._last_lp_statuses: dict[str, str] = {}
        self._last_lp_weights: dict[str, dict[str, float]] = {}

        if self.fixed_router is not None:
            self._rebuild_from_fixed_router()

    # ------------------------------------------------------------------
    # Lifecycle / registry binding
    # ------------------------------------------------------------------

    def attach_fixed_router(self, fixed_router: Any) -> None:
        """Bind the shared ``FixedRouter`` after strategy construction."""
        self.fixed_router = fixed_router
        self._pending_decisions = {}
        self._primary_reservations = {}
        self._last_lp_statuses = {}
        self._last_lp_weights = {}
        self._rebuild_from_fixed_router()

    def apply_runtime_overrides(
        self,
        *,
        latency_slo_sec: float | None = None,
        latency_min_samples: int | None = None,
    ) -> None:
        """Apply live RouteWise runtime settings from the admin API.

        Only algorithm knobs are runtime-overridable; resource limits are
        route-level configuration (``quota:`` / ``concurrency:`` blocks).
        """
        if latency_slo_sec is not None:
            self.config.latency_slo_sec = latency_slo_sec
        if latency_min_samples is not None:
            self.config.latency_min_samples = latency_min_samples

    def _rebuild_from_fixed_router(self) -> None:
        self.classified = {}
        self.route_candidates = {}
        self._model_routewise_pools = {}
        self._adapter_provider_type = {}
        self._adapter_endpoint_ids = {}
        self._endpoint_adapter = {}
        self._latency_profiles = {}
        self._classify_all()
        self._build_resource_pools()
        for candidates in self.route_candidates.values():
            for candidate in candidates:
                adapter = candidate.adapter
                self._adapter_provider_type[id(adapter)] = candidate.provider_type
                endpoint_id = candidate.endpoint_id
                self._adapter_endpoint_ids[id(adapter)] = endpoint_id
                self._endpoint_adapter[endpoint_id] = adapter
                if endpoint_id not in self._latency_profiles:
                    self._latency_profiles[endpoint_id] = ProviderProfile(
                        endpoint_id=endpoint_id,
                        window_sec=self.config.latency_window_sec,
                        max_samples=self.config.latency_max_samples_per_profile,
                    )
        self._validate_routes()

    async def start(self) -> None:
        """Start periodic maintenance tasks.

        Validates that any quota-bearing pool has a calibrated envelope before
        any background tasks are scheduled; an uncalibrated envelope raises
        :class:`EnvelopeNotCalibratedError`, which the server bootstrap path
        propagates so deployment fails fast instead of silently routing on
        fabricated shadow prices.
        """
        self._validate_envelope_calibration()
        if self._sweep_task is None or self._sweep_task.done():
            self._sweep_task = asyncio.create_task(
                self._sweep_pending_decisions_loop(),
                name="RouteWiseRouter.sweep_pending_decisions",
            )
        if self._quota_sources() and (
            self._quota_refresh_task is None or self._quota_refresh_task.done()
        ):
            self._quota_refresh_task = asyncio.create_task(
                self._refresh_quota_snapshots_loop(),
                name="RouteWiseRouter.refresh_quota_snapshots",
            )

    async def stop(self) -> None:
        """Stop periodic maintenance tasks."""
        tasks = [task for task in (self._sweep_task, self._quota_refresh_task) if task is not None]
        self._sweep_task = None
        self._quota_refresh_task = None
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _sweep_pending_decisions_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(PENDING_DECISIONS_SWEEP_INTERVAL_SECONDS)
                await self._sweep_pending_decisions_once()
        except asyncio.CancelledError:
            return

    async def _sweep_pending_decisions_once(self) -> int:
        now = time.time()
        cutoff = now - PENDING_DECISIONS_TTL_SECONDS
        stale = [
            request_id
            for request_id, decision in self._pending_decisions.items()
            if not decision.get("is_streaming")
            and isinstance(decision.get("timestamp"), (int, float))
            and float(decision["timestamp"]) < cutoff
        ]
        evicted = 0
        for request_id in stale:
            decision = self._pending_decisions.pop(request_id, None)
            with self._route_commit_lock:
                self._prefix_cache_pending.pop(request_id, None)
            if decision is None:
                continue
            evicted += 1
            logger.info(
                "routewise_decision_evicted",
                extra={
                    "event": "routewise_decision_evicted",
                    "request_id": request_id,
                    "age_sec": int(now - float(decision.get("timestamp", now))),
                },
            )
        return evicted

    async def refresh_quota_snapshots_once(self) -> None:
        """Refresh provider quota snapshots for configured S_Q candidates."""
        await self.quota_snapshots.refresh_once(self._quota_sources())

    async def _refresh_quota_snapshots_loop(self) -> None:
        interval = max(1.0, float(self.config.quota_snapshot_refresh_interval_sec))
        try:
            while True:
                try:
                    await self.refresh_quota_snapshots_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "routewise_quota_snapshot_loop_failed",
                        extra={"event": "routewise_quota_snapshot_loop_failed"},
                    )
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            return

    # ------------------------------------------------------------------
    # Classification and helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _endpoint_id(adapter: BaseAdapter) -> str:
        return endpoint_id_for_adapter(adapter)

    def _candidate_endpoint_id(self, adapter: BaseAdapter) -> str:
        return self._adapter_endpoint_ids.get(id(adapter), self._endpoint_id(adapter))

    @staticmethod
    def _pricing(adapter: BaseAdapter) -> tuple[float, float]:
        pricing = adapter.config.pricing or {}
        return float(pricing.get("prompt", "0")), float(pricing.get("completion", "0"))

    def _classify_all(self) -> None:
        for route_key, route_cfg in self.fixed_router.routes.items():
            model_id = getattr(route_cfg, "canonical_model_id", None) or route_key
            if model_id in self.route_candidates:
                continue
            candidates = build_provider_candidates(model_id, route_cfg.adapters)
            self.route_candidates[model_id] = candidates
            self.classified[model_id] = [
                (candidate.adapter, candidate.weight, candidate.provider_type)
                for candidate in candidates
            ]
            pools = {candidate.routewise_pool for candidate in candidates}
            if len(pools) == 1:
                self._model_routewise_pools[model_id] = next(iter(pools))
            else:
                self._model_routewise_pools[model_id] = model_id
                if pools:
                    logger.warning(
                        "Model '%s' has multiple routewise_pool values %s; using model_id pool",
                        model_id,
                        sorted(pools),
                    )

    def _build_resource_pools(self) -> None:
        """(Re)build per-pool resource managers from route-level policies.

        Routes sharing a pool id share one manager and must declare identical
        policies; conflicting declarations fail at boot. Quota pools are
        snapshot-backed (provider-reported usage is the truth source), so a
        shared pool id naturally shares the provider's global accounting.
        Concurrency counters are router-scoped.
        """
        quota_specs: dict[str, tuple[QuotaPolicy, QuotaSource | None, str]] = {}
        concurrency_specs: dict[str, tuple[ConcurrencyPolicy, str]] = {}
        self._endpoint_concurrency_pool = {}
        for candidates in self.route_candidates.values():
            for candidate in candidates:
                if (
                    candidate.provider_type is ProviderType.QUOTA
                    and candidate.quota_pool is not None
                    and candidate.quota_policy is not None
                ):
                    prior = quota_specs.get(candidate.quota_pool)
                    spec = (candidate.quota_policy, candidate.quota_source)
                    if prior is not None and (prior[0], prior[1]) != spec:
                        raise ValueError(
                            f"RouteWise quota_pool {candidate.quota_pool!r} is declared "
                            f"with conflicting quota policies by {prior[2]!r} and "
                            f"{candidate.endpoint_id!r}; routes sharing a pool must "
                            "declare identical quota blocks."
                        )
                    quota_specs[candidate.quota_pool] = (*spec, candidate.endpoint_id)
                elif (
                    candidate.provider_type is ProviderType.CONCURRENCY
                    and candidate.concurrency_pool is not None
                    and candidate.concurrency_policy is not None
                ):
                    prior_c = concurrency_specs.get(candidate.concurrency_pool)
                    if prior_c is not None and prior_c[0] != candidate.concurrency_policy:
                        raise ValueError(
                            f"RouteWise concurrency_pool {candidate.concurrency_pool!r} is "
                            f"declared with conflicting limits by {prior_c[1]!r} and "
                            f"{candidate.endpoint_id!r}; routes sharing a pool must "
                            "declare identical concurrency blocks."
                        )
                    concurrency_specs[candidate.concurrency_pool] = (
                        candidate.concurrency_policy,
                        candidate.endpoint_id,
                    )
                    self._endpoint_concurrency_pool[candidate.endpoint_id] = (
                        candidate.concurrency_pool
                    )

        quota_pools: dict[str, SnapshotQuotaPool] = {}
        for pool_id, (policy, source, endpoint) in quota_specs.items():
            if source is None:  # pragma: no cover - enforced in candidates.py
                raise ValueError(
                    f"RouteWise quota_pool {pool_id!r} ({endpoint!r}) has no quota_source"
                )
            # Snapshot pools are stateless wrappers (truth lives in the
            # store), so they are always rebuilt against the current store.
            quota_pools[pool_id] = SnapshotQuotaPool(
                self.quota_snapshots,
                source,
                policy=policy,
            )
        self.quota_pools = quota_pools

        concurrency_pools: dict[str, ConcurrencyManager] = {}
        for pool_id, (policy, _endpoint) in concurrency_specs.items():
            existing_c = self.concurrency_pools.get(pool_id)
            if existing_c is not None and existing_c.limit == policy.limit:
                concurrency_pools[pool_id] = existing_c
            else:
                concurrency_pools[pool_id] = ConcurrencyManager(policy.limit)
        self.concurrency_pools = concurrency_pools

    def _canonical_model_id(self, model_id: str) -> str:
        route_cfg = getattr(self.fixed_router, "routes", {}).get(model_id)
        return getattr(route_cfg, "canonical_model_id", None) or model_id

    def _validate_routes(self) -> None:
        has_stateful_provider = False
        for model_id, candidates in self.route_candidates.items():
            route_has_stateful_provider = any(
                candidate.provider_type in (ProviderType.QUOTA, ProviderType.CONCURRENCY)
                for candidate in candidates
            )
            has_stateful_provider = has_stateful_provider or route_has_stateful_provider
            if not any(
                candidate.provider_type is ProviderType.ON_DEMAND for candidate in candidates
            ):
                logger.warning(
                    "Model '%s' has no on-demand baseline; RouteWise will use "
                    "reference_api_price for L/U if configured. no P_O baseline is configured.",
                    model_id,
                )
        if has_stateful_provider:
            self._validate_stateful_provider_worker_scope()

    def _validate_stateful_provider_worker_scope(self) -> None:
        worker_count = _configured_worker_count()
        if worker_count is not None and worker_count > 1:
            if self.config.stateful_providers_single_worker_only:
                raise RuntimeError(
                    "RouteWise quota/concurrency providers are process-local in this "
                    "integration and require a single backend worker. Configure "
                    "only on-demand providers for routewise in multi-worker deployments, "
                    "or disable stateful_providers_single_worker_only after adding "
                    "shared quota/concurrency state."
                )
            logger.warning(
                "RouteWise quota/concurrency providers are process-local but worker_count=%d; "
                "quota and concurrency limits will be per-worker.",
                worker_count,
            )

    def _validate_envelope_calibration(self) -> None:
        """Refuse to operate if any quota-bearing pool has an uncalibrated envelope.

        The RouteWise paper requires the quota shadow price to be parameterized
        by a workload-derived ``[L, U]``; there is no seed fallback. Pools that
        contain at least one quota provider must therefore have a non-empty
        envelope before requests are served. Pure API or pure concurrency
        models are unaffected.
        """
        uncalibrated: list[tuple[str, str, int]] = []
        for model_id, candidates in self.route_candidates.items():
            if not any(candidate.provider_type is ProviderType.QUOTA for candidate in candidates):
                continue
            pool = self._routewise_pool(model_id)
            if self.envelope.snapshot(pool) is None:
                uncalibrated.append((pool, model_id, self.envelope.sample_count(pool)))
        if not uncalibrated:
            return
        needed = max(int(self.envelope.min_samples), 1)
        details = "\n".join(
            f"  - pool='{p}', model='{m}': {n}/{needed} envelope samples in window"
            for p, m, n in uncalibrated
        )
        raise EnvelopeNotCalibratedError(
            "RouteWise refuses to start: cost envelope is uncalibrated for "
            f"quota-bearing pools (each needs >= {needed} request-cost samples "
            "within the lookback window). Ensure api_logs has recent traffic "
            "for these models before deploying, or remove their quota "
            "providers.\n" + details
        )

    @staticmethod
    def _parse_reference_api_price(raw: dict[str, Any] | None) -> CandidatePricing | None:
        if raw is None:
            return None
        return CandidatePricing.from_raw(raw, context="reference_api_price")

    def _routewise_pool(self, model_id: str) -> str:
        model_id = self._canonical_model_id(model_id)
        return self._model_routewise_pools.get(model_id, model_id)

    def _quota_sources(self) -> list[QuotaSource]:
        sources: dict[QuotaSource, QuotaSource] = {}
        for candidates in self.route_candidates.values():
            for candidate in candidates:
                if (
                    candidate.provider_type is ProviderType.QUOTA
                    and candidate.quota_source is not None
                ):
                    sources[candidate.quota_source] = candidate.quota_source
        return list(sources.values())

    def _max_tokens_from_context(self, context: dict[str, Any]) -> int | None:
        params = context.get("params")
        if isinstance(params, dict):
            for key in ("max_completion_tokens", "max_tokens"):
                value = params.get(key)
                if value is not None:
                    try:
                        return int(value)
                    except (TypeError, ValueError):
                        return None
        return None

    def _prompt_tokens_from_context(self, context: dict[str, Any]) -> int:
        value = context.get("prompt_tokens")
        if value is None and isinstance(context.get("params"), dict):
            value = context["params"].get("prompt_tokens")
        try:
            tokens = int(value or 0)
        except (TypeError, ValueError):
            tokens = 0
        if tokens > 0:
            return tokens
        return estimate_prompt_tokens(context.get("messages") or [])

    def _predict_output(
        self,
        model_id: str,
        prompt_tokens: int,
        context: dict[str, Any],
    ) -> BucketMeanPrediction:
        model_id = self._canonical_model_id(model_id)
        return self.predictor.predict(
            model_id,
            prompt_tokens,
            max_tokens=self._max_tokens_from_context(context),
        )

    def _mean_ttft_sec(self, endpoint_id: str, now: float) -> float:
        profile = self._latency_profiles.get(endpoint_id)
        if profile is None:
            return self.config.latency_unprofiled_ttft_ms / 1000.0
        if profile.total_count(now) <= 0:
            return self.config.latency_unprofiled_ttft_ms / 1000.0
        mean = profile.mean_with_errors_sec(
            now,
            error_penalty_ms=RATE_LIMIT_ERROR_PENALTY_MS,
        )
        if mean is None:
            return self.config.latency_unprofiled_ttft_ms / 1000.0
        return mean

    def _api_cost_for_adapter(
        self,
        adapter: BaseAdapter,
        *,
        prompt_tokens: int,
        predicted_output_tokens: float,
    ) -> float:
        p_in, p_out = self._pricing(adapter)
        return api_request_cost_usd(
            prompt_tokens=prompt_tokens,
            predicted_output_tokens=predicted_output_tokens,
            input_price_per_m=p_in,
            output_price_per_m=p_out,
        )

    @staticmethod
    def _api_cost_for_pricing(
        pricing: CandidatePricing,
        *,
        prompt_tokens: int,
        output_tokens: float,
    ) -> float:
        return api_request_cost_usd(
            prompt_tokens=prompt_tokens,
            predicted_output_tokens=output_tokens,
            input_price_per_m=pricing.prompt,
            output_price_per_m=pricing.completion,
        )

    def _cheapest_api_cost(
        self,
        model_id: str,
        *,
        prompt_tokens: int,
        output_tokens: float,
    ) -> float | None:
        model_id = self._canonical_model_id(model_id)
        entries = self.route_candidates.get(model_id, [])
        costs = [
            self._api_cost_for_pricing(
                candidate.pricing,
                prompt_tokens=prompt_tokens,
                output_tokens=output_tokens,
            )
            for candidate in entries
            if candidate.provider_type is ProviderType.ON_DEMAND
        ]
        return min(costs) if costs else None

    def _reference_api_cost(
        self,
        model_id: str,
        *,
        prompt_tokens: int,
        output_tokens: float,
    ) -> float | None:
        api_cost = self._cheapest_api_cost(
            model_id,
            prompt_tokens=prompt_tokens,
            output_tokens=output_tokens,
        )
        if api_cost is not None:
            return api_cost
        if self.reference_api_price is None:
            return None
        return self._api_cost_for_pricing(
            self.reference_api_price,
            prompt_tokens=prompt_tokens,
            output_tokens=output_tokens,
        )

    def _estimate_value(self, model_id: str, prompt_tokens: int) -> float:
        """Compatibility helper: cheapest cold-cache API cost for one request."""
        model_id = self._canonical_model_id(model_id)
        prediction = self.predictor.predict(model_id, prompt_tokens)
        cost = self._reference_api_cost(
            model_id,
            prompt_tokens=prompt_tokens,
            output_tokens=prediction.tokens,
        )
        return cost if cost is not None else 0.0

    def _build_candidates(
        self,
        model_id: str,
        *,
        prompt_tokens: int,
        predicted_output_tokens: float,
        envelope: CostEnvelopeSnapshot | None,
        now: float,
        context: dict[str, Any] | None = None,
    ) -> tuple[list[FeasibleProviderCandidate], tuple[tuple[Any, ...], dict[str, Any]] | None]:
        model_id = self._canonical_model_id(model_id)
        entries = self.route_candidates.get(model_id)
        if entries is None:
            raise ValueError(f"RouteWiseRouter has no route for model '{model_id}'")

        candidates: list[FeasibleProviderCandidate] = []
        prefix_context = self._prefix_cache_cost_context(model_id, context)

        for route_candidate in entries:
            adapter = route_candidate.adapter
            endpoint_id = route_candidate.endpoint_id
            self._ensure_health(endpoint_id)
            circuit = self._circuits[endpoint_id]
            if not circuit.allow_request():
                continue

            request_cost = self._api_cost_for_pricing(
                route_candidate.pricing,
                prompt_tokens=prompt_tokens,
                output_tokens=predicted_output_tokens,
            )

            provider_type: Literal["on_demand", "quota", "concurrency"]
            if route_candidate.provider_type is ProviderType.ON_DEMAND:
                provider_type = "on_demand"
                cost = request_cost
                reason = "cold_api_cost"
                prefix_discount = 0.0
                prefix_expected = 0.0
                prefix_applied = False
                if prefix_context is not None:
                    (
                        cost,
                        prefix_discount,
                        prefix_expected,
                        prefix_applied,
                    ) = self._apply_prefix_cache_cost_adjustment(
                        model_id=model_id,
                        route_candidate=route_candidate,
                        cold_cost=request_cost,
                        prefix_context=prefix_context,
                    )
                    if prefix_applied:
                        reason = "prefix_cache_adjusted_api_cost"
            elif route_candidate.provider_type is ProviderType.QUOTA:
                if envelope is None:
                    # No calibrated envelope: skip quota providers rather than
                    # invent a shadow price. Startup validation should
                    # normally have caught this before we got here.
                    logger.warning(
                        "Skipping quota provider %s for model %s: envelope uncalibrated",
                        endpoint_id,
                        model_id,
                    )
                    continue
                quota_pool_id = route_candidate.quota_pool
                quota_pool = self.quota_pools.get(quota_pool_id) if quota_pool_id else None
                if quota_pool is None or not quota_pool.ready:
                    # Snapshot-backed pools are skipped until the first provider
                    # snapshot lands rather than priced off invented state.
                    continue
                if quota_pool.remaining <= 0:
                    continue
                used_fraction = quota_pool.used_fraction
                quota_remaining = quota_pool.remaining
                provider_type = "quota"
                cost = quota_shadow_price_usd(
                    used_fraction=used_fraction,
                    lower=envelope.lower,
                    upper=envelope.upper,
                )
                reason = "quota_shadow_price"
                prefix_discount = 0.0
                prefix_expected = 0.0
                prefix_applied = False
            elif route_candidate.provider_type is ProviderType.CONCURRENCY:
                concurrency_pool_id = route_candidate.concurrency_pool
                concurrency_pool = (
                    self.concurrency_pools.get(concurrency_pool_id) if concurrency_pool_id else None
                )
                if concurrency_pool is None or concurrency_pool.available <= 0:
                    continue
                provider_type = "concurrency"
                cost = 0.0
                reason = "available_concurrency_slot"
                prefix_discount = 0.0
                prefix_expected = 0.0
                prefix_applied = False
            else:
                continue

            if route_candidate.provider_type is not ProviderType.QUOTA:
                quota_pool_id = None
                used_fraction = None
                quota_remaining = None
            if route_candidate.provider_type is not ProviderType.CONCURRENCY:
                concurrency_pool_id = None

            candidates.append(
                FeasibleProviderCandidate(
                    endpoint_id=endpoint_id,
                    adapter=adapter,
                    provider_type=provider_type,
                    weight=route_candidate.weight,
                    effective_cost_usd=cost,
                    request_cost_usd=request_cost,
                    mean_ttft_sec=self._mean_ttft_sec(endpoint_id, now),
                    cost_reason=reason,
                    prefix_cache_discount_usd=prefix_discount,
                    prefix_cache_expected_tokens=prefix_expected,
                    prefix_cache_adjustment_applied=prefix_applied,
                    quota_pool=quota_pool_id,
                    concurrency_pool=concurrency_pool_id,
                    quota_used_fraction=used_fraction,
                    quota_remaining=quota_remaining,
                )
            )
        return candidates, prefix_context

    def _prefix_cache_cost_context(
        self,
        model_id: str,
        context: dict[str, Any] | None,
    ) -> tuple[tuple[Any, ...], dict[str, Any]] | None:
        """Return request prefix-cache inputs for cost adjustment, if eligible."""
        if not self.config.prefix_cache_cost_adjustment_enabled or context is None:
            return None
        params = context.get("params")
        session = str(params.get("session_id") or "") if isinstance(params, dict) else ""
        if not session:
            return None
        messages = context.get("messages") or []
        try:
            blocks = self.prefix_cache.build_blocks(
                messages,
                tools=params.get("tools") if isinstance(params, dict) else None,
                response_format=params.get("response_format") if isinstance(params, dict) else None,
            )
        except Exception:
            logger.debug("prefix_cache cost build_blocks failed", exc_info=True)
            return None
        user = str(req_ctx.get().get("affinity_key") or "")
        cache_params = self._cache_affecting_params(params)
        # ``scopes`` is filled in by _apply_prefix_cache_cost_adjustment as each
        # eligible candidate is priced, so the post-success warm only covers the
        # providers the cost estimate actually applied to (direct, cache-priced,
        # non-rotating) and never rebuilds blocks/scopes a second time.
        return blocks, {
            "model_id": model_id,
            "session": session,
            "user": user,
            "cache_params": cache_params,
            "scopes": {},
        }

    def _apply_prefix_cache_cost_adjustment(
        self,
        *,
        model_id: str,
        route_candidate: RouteProviderCandidate,
        cold_cost: float,
        prefix_context: tuple[tuple[Any, ...], dict[str, Any]],
    ) -> tuple[float, float, float, bool]:
        """Return API effective cost after a guarded prefix-cache discount."""
        adapter = route_candidate.adapter
        if self._has_rotating_key_pool(adapter):
            return cold_cost, 0.0, 0.0, False
        delta = price_delta_per_token(
            route_candidate.pricing.prompt,
            route_candidate.pricing.cache_read,
        )
        if delta <= 0.0:
            return cold_cost, 0.0, 0.0, False

        blocks, info = prefix_context
        provider_id = str(getattr(adapter.config, "provider", "") or "")
        scope = self.prefix_cache.scope_for(
            session=str(info["session"]),
            provider_id=provider_id,
            endpoint_id=route_candidate.endpoint_id,
            model_profile=model_id,
            user=str(info["user"]),
            cache_params=str(info["cache_params"]),
        )
        # Stash this eligible candidate's scope so the selected request can warm
        # it after a successful observation without rebuilding blocks/scopes.
        scopes = info.get("scopes")
        if isinstance(scopes, dict):
            scopes[route_candidate.endpoint_id] = scope
        record = self.prefix_cache.evaluate(
            scope,
            blocks,
            cold_cost=cold_cost,
            price_delta=delta,
        )
        discount = record.cache_discount if record.would_apply else 0.0
        adjusted = max(0.0, cold_cost - discount)
        return adjusted, discount, record.expected_cached_tokens, record.would_apply

    @staticmethod
    def _has_rotating_key_pool(adapter: Any) -> bool:
        keys = getattr(adapter.config, "api_keys", None)
        return isinstance(keys, list) and len(keys) > 1

    @staticmethod
    def _routing_dollar_estimate(candidate: FeasibleProviderCandidate) -> float:
        """Return the decision-time dollar estimate for metadata / hedging cost."""
        if candidate.provider_type == "on_demand":
            return candidate.effective_cost_usd
        return candidate.request_cost_usd

    def _sample_solution(
        self,
        candidates: list[FeasibleProviderCandidate],
        solution: LPSolution,
    ) -> FeasibleProviderCandidate | None:
        by_id = {candidate.endpoint_id: candidate for candidate in candidates}
        total = sum(solution.weights.values())
        if total <= 0:
            return None
        threshold = self._rng.random()
        cumulative = 0.0
        last: FeasibleProviderCandidate | None = None
        for endpoint_id, weight in solution.weights.items():
            candidate = by_id.get(endpoint_id)
            if candidate is None or weight <= 0:
                continue
            last = candidate
            cumulative += weight / total
            if threshold <= cumulative:
                return candidate
        return last

    def _commit_candidate(self, candidate: FeasibleProviderCandidate) -> bool:
        if candidate.provider_type == "concurrency":
            pool = (
                self.concurrency_pools.get(candidate.concurrency_pool)
                if candidate.concurrency_pool
                else None
            )
            return pool is not None and pool.try_acquire()
        if candidate.provider_type == "quota":
            quota_pool = (
                self.quota_pools.get(candidate.quota_pool) if candidate.quota_pool else None
            )
            if quota_pool is None:
                return False
            # Commit at selection time and do not refund on provider failure:
            # fallback is API-only, so refunding would let failed quota attempts
            # become free retries against the same scarce subscription.
            return quota_pool.consume()
        return True

    def _release_candidate(self, candidate: FeasibleProviderCandidate) -> None:
        if candidate.provider_type != "concurrency" or not candidate.concurrency_pool:
            return
        pool = self.concurrency_pools.get(candidate.concurrency_pool)
        if pool is not None:
            pool.release()

    def _reserve_candidate(self, candidate: FeasibleProviderCandidate) -> ProviderReservation:
        return ProviderReservation(router=self, candidate=candidate)

    def _remember_primary_reservation(
        self,
        request_id: str | None,
        candidate: FeasibleProviderCandidate,
    ) -> None:
        if not request_id:
            return
        self._release_pending_primary_reservation(request_id)
        if candidate.provider_type == "concurrency":
            self._primary_reservations[request_id] = ProviderReservation(
                router=self,
                candidate=candidate,
                acquired=True,
            )

    def _release_pending_primary_reservation(self, request_id: str | None) -> bool:
        if not request_id:
            return False
        reservation = self._primary_reservations.pop(request_id, None)
        if reservation is None:
            return False
        reservation.release()
        return True

    def _release_execution_primary_capacity(
        self,
        request_id: str | None,
        primary_adapter: Any,
    ) -> None:
        if self._release_pending_primary_reservation(request_id):
            return
        if self._adapter_provider_type.get(id(primary_adapter)) is not ProviderType.CONCURRENCY:
            return
        endpoint_id = self._adapter_endpoint_ids.get(id(primary_adapter))
        pool_id = self._endpoint_concurrency_pool.get(endpoint_id) if endpoint_id else None
        pool = self.concurrency_pools.get(pool_id) if pool_id else None
        if pool is not None:
            pool.release()

    def _quota_metadata_state(
        self,
        selected: FeasibleProviderCandidate,
    ) -> tuple[float | None, int | None]:
        """Post-commit quota state of the selected candidate's pool, if any."""
        if selected.provider_type == "quota" and selected.quota_pool:
            pool = self.quota_pools.get(selected.quota_pool)
            if pool is not None:
                return pool.used_fraction, pool.remaining
        return None, None

    def _decision_metadata(
        self,
        *,
        model_id: str,
        request_id: str | None,
        prompt_tokens: int,
        prediction: BucketMeanPrediction,
        envelope: CostEnvelopeSnapshot | None,
        candidates: list[FeasibleProviderCandidate],
        solution: LPSolution,
        selected: FeasibleProviderCandidate,
        hedge_plan: HedgePlan | None = None,
    ) -> dict[str, Any]:
        selected_weight = solution.weights.get(selected.endpoint_id, 0.0)
        api_cost = self._reference_api_cost(
            model_id,
            prompt_tokens=prompt_tokens,
            output_tokens=prediction.tokens,
        )
        quota_used_fraction, quota_remaining = self._quota_metadata_state(selected)
        selected_quota_source = (
            getattr(self.quota_pools.get(selected.quota_pool), "source", None)
            if selected.quota_pool
            else None
        )
        selected_concurrency_pool = (
            self.concurrency_pools.get(selected.concurrency_pool)
            if selected.concurrency_pool
            else None
        )
        return {
            "request_id": request_id,
            "timestamp": time.time(),
            "is_streaming": False,
            "selected_provider_type": selected.provider_type,
            "selected_endpoint": selected.endpoint_id,
            "selected_effective_cost_usd": selected.effective_cost_usd,
            "selected_mean_ttft_sec": selected.mean_ttft_sec,
            "selected_lp_weight": selected_weight,
            "budget_usd": solution.budget_usd,
            "lp_status": solution.status,
            "lp_weights": dict(solution.weights),
            "candidate_costs_usd": {c.endpoint_id: c.effective_cost_usd for c in candidates},
            "candidate_request_costs_usd": {c.endpoint_id: c.request_cost_usd for c in candidates},
            "candidate_cost_reasons": {c.endpoint_id: c.cost_reason for c in candidates},
            "candidate_prefix_cache_discounts_usd": {
                c.endpoint_id: c.prefix_cache_discount_usd
                for c in candidates
                if c.prefix_cache_discount_usd > 0
            },
            "candidate_prefix_cache_expected_tokens": {
                c.endpoint_id: c.prefix_cache_expected_tokens
                for c in candidates
                if c.prefix_cache_expected_tokens > 0
            },
            "candidate_mean_ttft_sec": {c.endpoint_id: c.mean_ttft_sec for c in candidates},
            "candidate_provider_types": {c.endpoint_id: c.provider_type for c in candidates},
            "candidate_quota_used_fraction": {
                c.endpoint_id: c.quota_used_fraction
                for c in candidates
                if c.quota_used_fraction is not None
            },
            "candidate_quota_remaining": {
                c.endpoint_id: c.quota_remaining
                for c in candidates
                if c.quota_remaining is not None
            },
            "prompt_tokens": prompt_tokens,
            "predicted_output_tokens": prediction.tokens,
            "output_prediction_source": prediction.source,
            "output_prediction_bucket": prediction.bucket,
            "output_prediction_sample_count": prediction.sample_count,
            "api_reference_cost_usd": api_cost,
            "api_cheapest_cost_usd": api_cost,
            "v_t": api_cost if api_cost is not None else 0.0,
            "cache_assumption": "cold",
            "estimated_cached_input_tokens": 0,
            "stateful_providers_single_worker_only": self.config.stateful_providers_single_worker_only,
            "L": envelope.lower if envelope is not None else None,
            "U": envelope.upper if envelope is not None else None,
            "envelope_sample_count": (envelope.sample_count if envelope is not None else 0),
            "quota_pool": selected.quota_pool,
            "quota_source": (
                {
                    "provider": selected_quota_source.provider,
                    "usage_label": selected_quota_source.usage_label,
                    "unit": selected_quota_source.unit,
                }
                if selected_quota_source is not None
                else None
            ),
            "quota_used_fraction": quota_used_fraction,
            "quota_remaining": quota_remaining,
            "quota_committed": 0.0,
            "concurrency_pool": selected.concurrency_pool,
            "sc_active": selected_concurrency_pool.active if selected_concurrency_pool else 0,
            "sc_limit": selected_concurrency_pool.limit if selected_concurrency_pool else 0,
            "sc_committed": selected.provider_type == "concurrency",
            "hedged": False,
            "backup_won": False,
            # --- H6 canonical cross-source fields ---------------------------
            # Aligned with the SIM/REAL PerRequestRecord schema
            # (docs in the RouteWise simulator repo: SCHEMA_UNIFICATION). These
            # are added alongside the prod-native fields above (which the
            # observation/health pipeline still reads) so cross-source parity
            # has one field-name contract. Provider granularity is endpoint-level
            # here vs provider-level in SIM/REAL — that value difference is
            # inherent to the source, not a schema gap.
            "policy": "routewise",
            "primary_provider": selected.endpoint_id,
            "primary_provider_type": selected.provider_type,
            "backup_provider": None,
            "backup_provider_type": None,
            "hedge_triggered": False,
            "hedge_winner": None,
            "hedge_algorithm": "probability_target" if hedge_plan is not None else "disabled",
            "hedge_schedule": "slo_relative_checkpoints" if hedge_plan is not None else None,
            "hedge_delay_ms": None,
            "hedge_success_probability": None,
            "lp_budget_usd": solution.budget_usd,
            # Decision-time dollar estimate at predicted tokens. On-demand uses the
            # effective cost because guarded prefix-cache adjustment is part of
            # the P_O cost formula; P_Q/P_C retain the raw API reference because
            # their effective cost is a quota/concurrency shadow price.
            "primary_routing_estimated_cost_usd": self._routing_dollar_estimate(selected),
            "backup_routing_estimated_cost_usd": None,
            "routing_estimated_cost_usd": self._routing_dollar_estimate(selected),
            # lp_weights and lp_status (above) already use canonical names.
        }

    def _select_hedge_plan(
        self,
        *,
        selected: FeasibleProviderCandidate,
        now: float,
    ) -> HedgePlan | None:
        """Return the checkpoint schedule for probability-target hedging."""
        if self.config.latency_hedge_mode != PROBABILITY_TARGET_HEDGE_MODE:
            return None

        primary_profile = self._latency_profiles.get(selected.endpoint_id)
        if (
            primary_profile is None
            or primary_profile.sample_count(now) < self.config.latency_min_samples
        ):
            return None

        checkpoints = hedge_checkpoints_for_slo(self.config.latency_slo_sec * 1000.0)
        return HedgePlan(checkpoints_sec=checkpoints) if checkpoints else None

    def _select_checkpoint_backup(
        self,
        *,
        model_id: str,
        request_id: str | None,
        context: dict[str, Any] | None,
        prompt_tokens: int,
        predicted_output_tokens: float,
        envelope: CostEnvelopeSnapshot | None,
        selected: FeasibleProviderCandidate,
        checkpoints_sec: tuple[float, ...],
        elapsed_sec: float,
        checkpoint_ts: float,
    ) -> CheckpointBackupDispatch | None:
        """Select and reserve a backup at one in-flight checkpoint."""
        with self._route_commit_lock:
            return self._select_checkpoint_backup_locked(
                model_id=model_id,
                request_id=request_id,
                context=context,
                prompt_tokens=prompt_tokens,
                predicted_output_tokens=predicted_output_tokens,
                envelope=envelope,
                selected=selected,
                checkpoints_sec=checkpoints_sec,
                elapsed_sec=elapsed_sec,
                checkpoint_ts=checkpoint_ts,
            )

    def _select_checkpoint_backup_locked(
        self,
        *,
        model_id: str,
        request_id: str | None,
        context: dict[str, Any] | None,
        prompt_tokens: int,
        predicted_output_tokens: float,
        envelope: CostEnvelopeSnapshot | None,
        selected: FeasibleProviderCandidate,
        checkpoints_sec: tuple[float, ...],
        elapsed_sec: float,
        checkpoint_ts: float,
    ) -> CheckpointBackupDispatch | None:
        primary_profile = self._latency_profiles.get(selected.endpoint_id)
        if (
            primary_profile is None
            or primary_profile.sample_count(checkpoint_ts) < self.config.latency_min_samples
        ):
            return None

        candidates, _ = self._build_candidates(
            model_id,
            prompt_tokens=prompt_tokens,
            predicted_output_tokens=predicted_output_tokens,
            envelope=envelope,
            now=checkpoint_ts,
            context=context,
        )
        current = self._select_hedge_candidate_at_elapsed(
            primary_profile=primary_profile,
            candidates=candidates,
            selected=selected,
            now=checkpoint_ts,
            elapsed_sec=elapsed_sec,
        )
        if current is None:
            return None

        scheduled_checkpoint = any(
            abs(checkpoint - elapsed_sec) <= 1e-9 for checkpoint in checkpoints_sec
        )
        # Primary failure asks for an immediate backup at an arbitrary elapsed
        # time; only scheduled checkpoints should defer to a later checkpoint.
        if scheduled_checkpoint:
            for future_elapsed in checkpoints_sec:
                if future_elapsed <= elapsed_sec + 1e-9:
                    continue
                future = self._select_hedge_candidate_at_elapsed(
                    primary_profile=primary_profile,
                    candidates=candidates,
                    selected=selected,
                    now=checkpoint_ts,
                    elapsed_sec=future_elapsed,
                )
                if future is not None:
                    return None

        backup = current.provider
        reservation = self._reserve_candidate(backup)
        if not reservation.acquire():
            return None

        self._record_hedge_dispatch(
            request_id=request_id,
            backup=backup,
            elapsed_sec=elapsed_sec,
            success_probability=current.success_probability,
        )
        return CheckpointBackupDispatch(
            backup=backup.adapter,
            elapsed_sec=elapsed_sec,
            success_probability=current.success_probability,
            release=reservation.release,
        )

    def _select_hedge_candidate_at_elapsed(
        self,
        *,
        primary_profile: ProviderProfile,
        candidates: list[FeasibleProviderCandidate],
        selected: FeasibleProviderCandidate,
        now: float,
        elapsed_sec: float,
    ) -> BackupCandidate[FeasibleProviderCandidate] | None:
        backup_candidates: list[BackupCandidate[FeasibleProviderCandidate]] = []
        for candidate in candidates:
            if candidate.endpoint_id == selected.endpoint_id:
                continue
            profile = self._latency_profiles.get(candidate.endpoint_id)
            if profile is None or profile.sample_count(now) < self.config.latency_min_samples:
                continue
            success_probability = combined_success_probability(
                lambda value_ms: primary_profile.cdf_at(value_ms / 1000.0, now),
                lambda value_ms, backup_profile=profile: backup_profile.cdf_at(
                    value_ms / 1000.0,
                    now,
                ),
                elapsed_ms=elapsed_sec * 1000.0,
                slo_ms=self.config.latency_slo_sec * 1000.0,
                dispatch_overhead_ms=0.0,
            )
            backup_candidates.append(
                BackupCandidate(
                    provider=candidate,
                    success_probability=success_probability,
                    marginal_cost=self._routing_dollar_estimate(candidate),
                    true_mean_ms=candidate.mean_ttft_sec * 1000.0,
                    success_target=HEDGE_SUCCESS_TARGET,
                )
            )
        return select_probability_backup(backup_candidates)

    def _record_hedge_dispatch(
        self,
        *,
        request_id: str | None,
        backup: FeasibleProviderCandidate,
        elapsed_sec: float,
        success_probability: float,
    ) -> None:
        if not request_id or request_id not in self._pending_decisions:
            return
        meta = self._pending_decisions[request_id]
        meta["backup_provider"] = backup.endpoint_id
        meta["backup_provider_type"] = backup.provider_type
        meta["hedge_delay_ms"] = elapsed_sec * 1000.0
        meta["hedge_success_probability"] = success_probability
        backup_cost = self._routing_dollar_estimate(backup)
        meta["backup_routing_estimated_cost_usd"] = backup_cost
        primary_cost = meta.get("primary_routing_estimated_cost_usd")
        if primary_cost is not None:
            meta["routing_estimated_cost_usd"] = float(primary_cost) + backup_cost
        else:
            meta["routing_estimated_cost_usd"] = backup_cost

    def _apply_hedge_execution_metadata(self, adapter: Any, request_id: str | None) -> None:
        """Update pending RouteWise metadata after a HedgedAdapter has run."""
        if not request_id or request_id not in self._pending_decisions:
            return
        if not isinstance(adapter, HedgedAdapter):
            return

        meta = self._pending_decisions[request_id]
        hedge_triggered = bool(getattr(adapter, "hedge_triggered", False))
        backup_won = bool(getattr(adapter, "backup_won", False))
        meta["hedged"] = hedge_triggered
        meta["hedge_triggered"] = hedge_triggered
        meta["backup_won"] = backup_won
        if hedge_triggered and getattr(adapter, "hedge_delay_sec", None) is not None:
            meta["hedge_delay_ms"] = float(adapter.hedge_delay_sec) * 1000.0
        if hedge_triggered and getattr(adapter, "hedge_success_probability", None) is not None:
            meta["hedge_success_probability"] = adapter.hedge_success_probability
        if hedge_triggered:
            meta["hedge_winner"] = "backup" if backup_won else "primary"
        else:
            meta["backup_provider"] = None
            meta["backup_provider_type"] = None
            meta["hedge_winner"] = None
        failed_attempts = getattr(adapter, "failed_attempts", None)
        if failed_attempts:
            existing = meta.get("failed_attempts")
            meta["failed_attempts"] = _dedupe_failed_attempts(
                [
                    *(existing if isinstance(existing, list) else []),
                    *failed_attempts,
                ]
            )

    # ------------------------------------------------------------------
    # BaseRouter integration
    # ------------------------------------------------------------------

    def on_provider_success(self, provider: str) -> None:
        """Record a provider success emitted by HedgedAdapter."""
        self._on_success(provider)

    def on_provider_failure(self, provider: str, reason: str) -> None:
        """Record a provider failure emitted by HedgedAdapter."""
        self._on_failure(provider, reason=reason)

    def _select_adapter(self, model_id: str, context: dict[str, Any]) -> BaseAdapter | None:
        model_id = self._canonical_model_id(model_id)
        if model_id not in self.classified:
            raise ValueError(f"RouteWiseRouter has no route for model '{model_id}'")

        with self._route_commit_lock:
            return self._select_adapter_locked(model_id, context)

    def _select_adapter_locked(
        self,
        model_id: str,
        context: dict[str, Any],
    ) -> BaseAdapter | None:
        prompt_tokens = self._prompt_tokens_from_context(context)
        prediction = self._predict_output(model_id, prompt_tokens, context)
        pool = self._routewise_pool(model_id)
        envelope = self.envelope.snapshot(pool)
        now = time.time()

        candidates, prefix_context = self._build_candidates(
            model_id,
            prompt_tokens=prompt_tokens,
            predicted_output_tokens=prediction.tokens,
            envelope=envelope,
            now=now,
            context=context,
        )
        if not candidates:
            return None

        # If a selected concurrency/quota candidate loses a race while
        # committing, remove it and re-solve with the remaining candidates.
        while candidates:
            lp_candidates = [
                LPCandidate(c.endpoint_id, c.effective_cost_usd, c.mean_ttft_sec)
                for c in candidates
            ]
            solution = solve_cost_budgeted_mean_ttft(
                lp_candidates,
                alpha=self.config.budget_alpha,
            )
            self._last_lp_statuses[model_id] = solution.status
            self._last_lp_weights[model_id] = dict(solution.weights)
            selected = self._sample_solution(candidates, solution)
            if selected is None:
                return None
            if self._commit_candidate(selected):
                request_id = context.get("request_id")
                self._remember_primary_reservation(request_id, selected)
                hedge_plan = self._select_hedge_plan(
                    selected=selected,
                    now=now,
                )
                adapter: BaseAdapter = selected.adapter
                if hedge_plan is not None:

                    def _select_checkpoint_backup_for_request(
                        elapsed_sec: float,
                        checkpoint_ts: float,
                        *,
                        request_id: str | None = request_id,
                        selected: FeasibleProviderCandidate = selected,
                        checkpoints_sec: tuple[float, ...] = hedge_plan.checkpoints_sec,
                    ) -> CheckpointBackupDispatch | None:
                        return self._select_checkpoint_backup(
                            model_id=model_id,
                            request_id=request_id,
                            context=context,
                            prompt_tokens=prompt_tokens,
                            predicted_output_tokens=prediction.tokens,
                            envelope=envelope,
                            selected=selected,
                            checkpoints_sec=checkpoints_sec,
                            elapsed_sec=elapsed_sec,
                            checkpoint_ts=checkpoint_ts,
                        )

                    adapter = HedgedAdapter(
                        primary=selected.adapter,
                        event_sink=self,
                        hedge_checkpoints_sec=hedge_plan.checkpoints_sec,
                        checkpoint_backup_selector=_select_checkpoint_backup_for_request,
                    )
                if request_id:
                    self._pending_decisions[request_id] = self._decision_metadata(
                        model_id=model_id,
                        request_id=request_id,
                        prompt_tokens=prompt_tokens,
                        prediction=prediction,
                        envelope=envelope,
                        candidates=candidates,
                        solution=solution,
                        selected=selected,
                        hedge_plan=hedge_plan,
                    )
                if self.prefix_cache.enabled:
                    self._stash_prefix_for_commit(prefix_context, request_id)
                return adapter
            candidates = [c for c in candidates if c.endpoint_id != selected.endpoint_id]
            if not candidates:
                return None
        return None

    def _stash_prefix_for_commit(
        self,
        prefix_context: tuple[tuple[Any, ...], dict[str, Any]] | None,
        request_id: str | None,
    ) -> None:
        """Stash request blocks and eligible-candidate scopes for the warm on success.

        The scopes were collected by :meth:`_apply_prefix_cache_cost_adjustment`
        while pricing candidates, so only providers the cost estimate applied to
        (direct, cache-priced, non-rotating) are present. The commit
        (:meth:`_commit_prefix_cache_observation`) looks the winning endpoint up by
        id, so a winner without a scope here -- e.g. a rotating-key or no-delta
        provider -- is simply not warmed.

        Keyed by the external request id from the request context, the same id the
        observation path reads back.
        """
        if prefix_context is None:
            return
        blocks, info = prefix_context
        scopes = info.get("scopes") or {}
        if not scopes:
            return
        stash_key = str(req_ctx.get().get("request_id") or request_id or "")
        if not stash_key:
            return
        self._prefix_cache_pending[stash_key] = (blocks, scopes)
        while len(self._prefix_cache_pending) > _PREFIX_CACHE_PENDING_MAX:
            self._prefix_cache_pending.pop(next(iter(self._prefix_cache_pending)), None)

    def _commit_prefix_cache_observation(self, obs: RoutingObservation) -> None:
        """On a selected success, commit the winning provider's prefix to memory.

        Only successful observations warm history, and only under the endpoint that
        actually served (``obs.endpoint_id``) — so failed, fallback, or lost-hedge
        attempts are never recorded as warm. The request is correlated to its
        route-time blocks via ``request_id`` from the request context.
        """
        request_id = str(req_ctx.get().get("request_id") or "")
        if not request_id:
            return
        with self._route_commit_lock:
            stashed = self._prefix_cache_pending.pop(request_id, None)
        if not obs.success:
            return
        if stashed is None:
            return
        blocks, scopes = stashed
        scope = scopes.get(obs.endpoint_id)
        if scope is None:
            return
        self.prefix_cache.remember(
            scope,
            blocks,
        )

    @staticmethod
    def _cache_affecting_params(params: Any) -> str:
        """Return a stable string of the request params that bust prefix cache."""
        if not isinstance(params, dict):
            return ""
        keys = ("temperature", "top_p", "top_k", "tools", "response_format")
        relevant = {key: params[key] for key in keys if params.get(key) is not None}
        return json.dumps(relevant, sort_keys=True, default=str)

    def _get_fallback_adapters(
        self,
        model_id: str,
        failed_adapter: BaseAdapter,
    ) -> list[BaseAdapter]:
        model_id = self._canonical_model_id(model_id)
        entries = self.classified.get(model_id, [])
        return [
            a
            for a, _w, provider_type in entries
            if a is not failed_adapter and provider_type is ProviderType.ON_DEMAND
        ]

    def record_observation(self, obs: RoutingObservation) -> None:
        """Update output predictor, latency profile, and L/U envelope."""
        if self.prefix_cache.enabled:
            self._commit_prefix_cache_observation(obs)
        model_id = self._canonical_model_id(obs.model_id)
        if obs.completion_tokens > 0:
            self.predictor.update(model_id, obs.prompt_tokens, obs.completion_tokens)

        if obs.endpoint_id and obs.endpoint_id in self._latency_profiles:
            now = time.time()
            error_type: str | None = None if obs.success else "error"
            latency_ms = (
                obs.ttft_ms
                if obs.ttft_ms is not None
                else obs.total_latency_ms
                if obs.success
                else -1.0
            )
            self._latency_profiles[obs.endpoint_id].record(now, latency_ms, error_type)

        if obs.prompt_tokens > 0 and obs.completion_tokens > 0:
            sample_cost = self._reference_api_cost(
                model_id,
                prompt_tokens=obs.prompt_tokens,
                output_tokens=obs.completion_tokens,
            )
            if sample_cost is not None:
                self.envelope.observe(self._routewise_pool(model_id), sample_cost)

        logger.debug(
            "RouteWise observation: model=%s endpoint=%s completion_tokens=%d success=%s",
            model_id,
            obs.endpoint_id,
            obs.completion_tokens,
            obs.success,
        )

    def bootstrap_from_log_rows(
        self,
        rows: Iterable[Mapping[str, Any]],
        *,
        include_latency: bool = True,
        include_envelope: bool = True,
    ) -> dict[str, int]:
        """Warm latency profiles and the cost envelope from historical api_logs rows."""
        counts = {"rows": 0, "latency_events": 0, "failed_attempts": 0, "envelope_samples": 0}
        for row in rows:
            counts["rows"] += 1
            ts = self._timestamp_sec(row.get("timestamp"))
            if ts is None:
                continue

            model_id = self._canonical_model_id(str(row.get("model_id") or ""))
            if include_latency:
                endpoint_id = self._string_or_none(row.get("endpoint_id")) or self._string_or_none(
                    row.get("provider")
                )
                success = self._is_success_status(row.get("status_code"), row.get("error"))

                if endpoint_id and endpoint_id in self._latency_profiles:
                    latency_ms = self._bootstrap_latency_ms(row, success)
                    error_type = None if success else self._bootstrap_error_type(row)
                    self._latency_profiles[endpoint_id].record(ts, latency_ms, error_type)
                    counts["latency_events"] += 1

                for attempt in self._bootstrap_failed_attempts(row):
                    failed_endpoint = (
                        self._string_or_none(attempt.get("endpoint_id"))
                        or self._string_or_none(attempt.get("base_url"))
                        or self._string_or_none(attempt.get("provider"))
                    )
                    if not failed_endpoint or failed_endpoint not in self._latency_profiles:
                        continue
                    error_type = self._string_or_none(attempt.get("error_type")) or "error"
                    self._latency_profiles[failed_endpoint].record(ts, -1.0, error_type)
                    counts["failed_attempts"] += 1

            if include_envelope:
                prompt_tokens = self._int_or_zero(row.get("prompt_tokens"))
                completion_tokens = self._int_or_zero(row.get("completion_tokens"))
                if prompt_tokens > 0 and completion_tokens > 0:
                    sample_cost = self._reference_api_cost(
                        model_id,
                        prompt_tokens=prompt_tokens,
                        output_tokens=completion_tokens,
                    )
                    if sample_cost is not None:
                        self.envelope.observe(self._routewise_pool(model_id), sample_cost, now=ts)
                        counts["envelope_samples"] += 1
        return counts

    @staticmethod
    def _timestamp_sec(value: Any) -> float | None:
        if isinstance(value, (int, float)):
            return float(value)
        timestamp = getattr(value, "timestamp", None)
        if callable(timestamp):
            try:
                return float(timestamp())
            except (TypeError, ValueError, OSError, OverflowError):
                return None
        return None

    @staticmethod
    def _string_or_none(value: Any) -> str | None:
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _int_or_zero(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    @classmethod
    def _is_success_status(cls, status_code: Any, error: Any) -> bool:
        code = cls._int_or_zero(status_code)
        return code > 0 and code < 400 and not error

    @classmethod
    def _bootstrap_latency_ms(cls, row: Mapping[str, Any], success: bool) -> float:
        if not success:
            return -1.0
        ttft_ms = row.get("ttft_ms")
        if ttft_ms is not None:
            try:
                return float(ttft_ms)
            except (TypeError, ValueError):
                pass
        try:
            return float(row.get("latency_ms") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    @classmethod
    def _bootstrap_error_type(cls, row: Mapping[str, Any]) -> str:
        error = cls._string_or_none(row.get("error"))
        if error:
            return error[:120]
        return "error"

    @staticmethod
    def _bootstrap_failed_attempts(row: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
        attempts = row.get("failed_attempts")
        if not isinstance(attempts, (list, tuple)):
            return ()
        seen: set[tuple[str | None, str | None, str | None]] = set()
        result: list[Mapping[str, Any]] = []
        for attempt in attempts:
            if not isinstance(attempt, dict):
                continue
            endpoint = (
                attempt.get("endpoint_id") or attempt.get("base_url") or attempt.get("provider")
            )
            key = (
                endpoint if isinstance(endpoint, str) else None,
                attempt.get("error_type") if isinstance(attempt.get("error_type"), str) else None,
                attempt.get("error") if isinstance(attempt.get("error"), str) else None,
            )
            if key in seen:
                continue
            seen.add(key)
            result.append(attempt)
        return tuple(result)

    # ------------------------------------------------------------------
    # Execution overrides: S_C slot lifecycle
    # ------------------------------------------------------------------

    async def _execute_adapter(
        self,
        adapter: Any,
        model_id: str,
        messages: list[dict[str, Any]],
        **params: Any,
    ) -> dict[str, Any]:
        primary_adapter = adapter.primary if isinstance(adapter, HedgedAdapter) else adapter
        request_id = params.get("request_id")
        original_config = getattr(adapter, "config", None)
        try:
            result = await super()._execute_adapter(adapter, model_id, messages, **params)
            if (
                getattr(adapter, "config", None) is not original_config
                and request_id
                and request_id in self._pending_decisions
            ):
                self._pending_decisions[request_id]["backup_won"] = True
            self._apply_hedge_execution_metadata(adapter, request_id)
            return result
        finally:
            self._apply_hedge_execution_metadata(adapter, request_id)
            self._release_execution_primary_capacity(request_id, primary_adapter)

    async def _execute_stream_adapter(
        self,
        adapter: Any,
        model_id: str,
        messages: list[dict[str, Any]],
        **params: Any,
    ) -> AsyncIterator[Any]:
        primary_adapter = adapter.primary if isinstance(adapter, HedgedAdapter) else adapter
        request_id = params.get("request_id")
        original_config = getattr(adapter, "config", None)
        try:
            async for chunk in super()._execute_stream_adapter(
                adapter, model_id, messages, **params
            ):
                yield chunk
        finally:
            if (
                getattr(adapter, "config", None) is not original_config
                and request_id
                and request_id in self._pending_decisions
            ):
                self._pending_decisions[request_id]["backup_won"] = True
            self._apply_hedge_execution_metadata(adapter, request_id)
            self._release_execution_primary_capacity(request_id, primary_adapter)

    # ------------------------------------------------------------------
    # chat_completion / stream_chat_completion: merge decision metadata
    # ------------------------------------------------------------------

    @staticmethod
    def _attach_decision_info(routing: dict[str, Any], decision_info: dict[str, Any]) -> None:
        routing["routewise"] = decision_info
        failed_attempts = decision_info.get("failed_attempts")
        if not isinstance(failed_attempts, list) or not failed_attempts:
            return
        existing = routing.get("failed_attempts")
        routing["failed_attempts"] = _dedupe_failed_attempts(
            [
                *(existing if isinstance(existing, list) else []),
                *failed_attempts,
            ]
        )

    async def chat_completion(
        self, model_id: str, messages: list[dict[str, Any]], **params: Any
    ) -> dict[str, Any]:
        """Run a non-streaming RouteWise chat completion."""
        if not params.get("request_id"):
            params["request_id"] = f"req-{uuid.uuid4().hex[:12]}"
        request_id = params["request_id"]

        try:
            try:
                resp = await super().chat_completion(model_id, messages, **params)
            except BaseException as e:
                decision_info = self._pending_decisions.pop(request_id, None)
                if decision_info:
                    exc_routing = getattr(e, "_routing", None)
                    if isinstance(exc_routing, dict):
                        self._attach_decision_info(exc_routing, decision_info)
                raise

            decision_info = self._pending_decisions.pop(request_id, None)
            if decision_info and isinstance(resp, dict) and "_routing" in resp:
                self._attach_decision_info(resp["_routing"], decision_info)
            return resp
        finally:
            self._release_pending_primary_reservation(request_id)

    async def stream_chat_completion(
        self, model_id: str, messages: list[dict[str, Any]], **params: Any
    ) -> AsyncIterator[Any]:
        """Run a streaming RouteWise chat completion."""
        if not params.get("request_id"):
            params["request_id"] = f"req-{uuid.uuid4().hex[:12]}"
        request_id = params["request_id"]

        done_chunk: str | None = None
        try:
            try:
                async for chunk in super().stream_chat_completion(model_id, messages, **params):
                    if request_id in self._pending_decisions:
                        self._pending_decisions[request_id]["is_streaming"] = True
                    if isinstance(chunk, str) and chunk.strip() == "data: [DONE]":
                        done_chunk = chunk
                        continue
                    yield chunk
            except BaseException as e:
                decision_info = self._pending_decisions.pop(request_id, None)
                if decision_info:
                    exc_routing = getattr(e, "_routing", None)
                    if isinstance(exc_routing, dict):
                        self._attach_decision_info(exc_routing, decision_info)
                raise

            decision_info = self._pending_decisions.pop(request_id, None)
            if decision_info:
                decision_info["is_streaming"] = True
                routing: dict[str, Any] = {}
                self._attach_decision_info(routing, decision_info)
                routing_chunk = {
                    "choices": [],
                    "_routing": routing,
                }
                yield f"data: {json.dumps(routing_chunk)}\n\n"

            if done_chunk:
                yield done_chunk
        finally:
            self._release_pending_primary_reservation(request_id)
