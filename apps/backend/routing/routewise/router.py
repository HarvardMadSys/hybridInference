"""RouteWise cost-aware router with primal-dual decision logic.

This module provides the ``RouteWiseRouter``, a ``BaseRouter`` subclass that
classifies adapters by subscription type (quota / concurrency / API) and
selects among them using a primal-dual (PD) or look-ahead primal-dual (LA-PD)
threshold algorithm.

Layer 1 decides among three tiers with priority **S_C > S_Q > S_A**:

- **S_C** (concurrency): binary gate -- admit if slots available (gain_C = v_t).
- **S_Q** (quota): exponential shadow price threshold (gain_Q = v_t - theta_Q).
- **S_A** (API): pay-per-token baseline (gain_A = 0).

Since theta_Q > 0 always (L_seed > 0), S_C beats S_Q whenever slots exist.

When S_A is chosen and multiple API providers exist, Layer 2 applies LP-based
latency-aware cost optimization to select among them using empirical latency
profiles and SWRR sampling.

Slot and quota semantics follow selection-commit: resources are acquired in
``_select_adapter`` at the moment the decision is made.  Concurrency slots
are released in the ``_execute_adapter`` / ``_execute_stream_adapter`` finally
block, covering success, error, and cancellation.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

from routing.routers import BaseRouter, RoutingObservation
from serving.utils.logging import get_logger
from serving.utils.tokens import estimate_prompt_tokens

from .concurrency import ConcurrencyManager
from .hedging import HedgedAdapter, compute_hedge_threshold
from .latency import ProviderProfile, ShadowHedgeDecision, SWRRSampler
from .lp_solver import pre_filter_providers, solve_provider_lp_with_relaxation
from .predictor import EMAOutputPredictor
from .quota import QuotaManager

if TYPE_CHECKING:
    from serving.adapters.base import BaseAdapter

    from .config import RouteWiseConfig

logger = get_logger(__name__)


# TTL for non-streaming entries in RouteWiseRouter._pending_decisions. If
# ``chat_completion`` / ``stream_chat_completion`` doesn't consume an entry
# within this window — typically because the request was aborted, timed out,
# or hit a code-path bug — the periodic sweep evicts it and emits a
# ``routewise_decision_evicted`` log event. Active streaming requests are
# intentionally excluded because they can legitimately remain in-flight for
# longer than the fixed TTL.
PENDING_DECISIONS_TTL_SECONDS: float = 300.0
PENDING_DECISIONS_SWEEP_INTERVAL_SECONDS: float = 60.0


class SubscriptionType(Enum):
    """Subscription tier for an adapter endpoint.

    Each route entry in ``models.yaml`` can declare a ``subscription_type``
    field.  RouteWiseRouter reads this field from ``ModelConfig`` and uses
    it to classify adapters into one of the three tiers.
    """

    QUOTA = "quota"  # S_Q: daily quota subscription
    CONCURRENCY = "concurrency"  # S_C: concurrency-limited subscription
    API = "api"  # S_A: pay-per-token (default)


class RouteWiseRouter(BaseRouter):
    """Cost-aware router with primal-dual / LA-PD adapter selection.

    RouteWiseRouter is a **peer** of NimbusRouter -- both extend BaseRouter
    directly and operate on routes registered in a shared ``FixedRouter``.

    The router classifies adapters into S_C / S_Q / S_A tiers, then applies
    either the PD or LA-PD decision rule (controlled by ``config.decision_rule``)
    to decide tier priority: **S_C > S_Q > S_A**.

    Attributes:
        fixed_router: The shared ``FixedRouter`` whose ``routes`` dict
            provides the per-model adapter lists.
        config: ``RouteWiseConfig`` policy parameters.
        classified: Per-model adapter classification:
            ``{model_id: [(adapter, weight, SubscriptionType), ...]}``.
        predictor: EMA output-token predictor for value estimation.
        quota_mgr: Daily quota manager with shadow price computation.
        conc_mgr: Concurrency slot manager (None when disabled).
    """

    def __init__(
        self,
        fixed_router: Any = None,
        config: RouteWiseConfig | None = None,
        params: Any = None,
    ) -> None:
        """Initialize RouteWiseRouter.

        Two construction shapes are supported:

        1. Direct (legacy): pass ``fixed_router`` + ``config`` (a
           ``RouteWiseConfig`` dataclass).  Used by the existing bootstrap
           path and tests.
        2. Strategy-registry: pass ``params`` (a ``RouteWiseParams`` Pydantic
           model from the strategy registry).  ``params`` is translated to
           ``RouteWiseConfig`` via ``model_dump()``.  ``fixed_router`` is
           bound later by ``ModelRouterRegistry`` via
           :meth:`attach_fixed_router`.

        Exactly one of ``config`` or ``params`` should be provided.  When
        constructed via the registry without a ``fixed_router``, post-init
        classification is deferred until ``attach_fixed_router`` runs.
        """
        super().__init__()

        if config is None and params is not None:
            # Translate Pydantic params -> RouteWiseConfig dataclass.
            from .config import RouteWiseConfig as _RWC

            config = _RWC(**params.model_dump())
        if config is None:
            from .config import RouteWiseConfig as _RWC

            config = _RWC()
        self.fixed_router = fixed_router
        self.config = config

        # Per-model adapter classification.
        self.classified: dict[str, list[tuple[Any, float, SubscriptionType]]] = {}

        # Reverse lookup: adapter id(obj) -> SubscriptionType.
        self._adapter_sub_type: dict[int, SubscriptionType] = {}

        # Online predictors and quota tracking.
        self.predictor = EMAOutputPredictor(
            alpha=0.1,
            min_samples=20,
            default_output=500.0,
        )
        self.quota_mgr = QuotaManager(config)

        # S_C concurrency manager (None when concurrency_enabled=False).
        self.conc_mgr: ConcurrencyManager | None = None
        if self.config.concurrency_enabled:
            self.conc_mgr = ConcurrencyManager(config)

        # Per-request decision metadata, keyed by request_id.
        # Populated in _select_adapter(), consumed in chat_completion() /
        # stream_chat_completion().  Same pattern as NimbusRouter.
        self._pending_decisions: dict[str, dict[str, Any]] = {}
        # Periodic TTL-cleanup task; populated by start(), cancelled by stop().
        self._sweep_task: asyncio.Task[None] | None = None

        # Precomputed per-token prices for all S_A adapters, keyed by model.
        # Each entry: (adapter, price_prompt_per_token, price_completion_per_token).
        self._api_adapter_prices: dict[str, list[tuple[Any, float, float]]] = {}

        # Layer 2: Latency-aware provider selection state.
        # Latency profiles are keyed by endpoint_id.  This dict is router-global,
        # but endpoint_ids are model-scoped ("{model}:{location}") as generated
        # by registry.py, so profiles are implicitly per-model.  If endpoint_id
        # generation ever changes to truly cross-model physical IDs, this dict
        # must be restructured to avoid profile contamination.
        self._latency_profiles: dict[str, ProviderProfile] = {}
        # Per-model SWRR samplers, LP timestamps, weights, and statuses.
        # Each model gets its own sampler so multi-model routing never cross-contaminates.
        self._swrr_samplers: dict[str, SWRRSampler] = {}
        self._last_lp_times: dict[str, float] = {}
        self._last_lp_weights: dict[str, dict[str, float]] = {}
        self._last_lp_statuses: dict[str, str] = {}
        self._shadow_hedge_log: list[ShadowHedgeDecision] = []
        self._shadow_hedge_log_maxlen: int = 10_000  # Cap to prevent unbounded growth
        # Maps endpoint_id -> (adapter, p_in_per_token, p_out_per_token).
        self._api_endpoint_map: dict[str, tuple[Any, float, float]] = {}
        # Track in-flight LP solves to avoid duplicate concurrent solves.
        self._pending_lp_solves: set[str] = set()

        # Run post-init classification when a fixed_router is available.
        # When constructed via the registry without one, defer to
        # attach_fixed_router (ModelRouterRegistry calls it immediately).
        if self.fixed_router is not None:
            self._classify_all()
            self._build_adapter_sub_type_map()
            self._precompute_api_prices()
            self._validate_api_baseline()
            self._init_latency_profiles()

    def attach_fixed_router(self, fixed_router: Any) -> None:
        """Bind a ``FixedRouter`` after construction.

        Used by ``ModelRouterRegistry`` when a model is configured with
        ``router: routewise`` in YAML — the registry constructs the router
        via ``build_router("routewise", params)`` first, then attaches the
        shared ``FixedRouter`` so classification and latency-profile init
        can run.

        Safe to call more than once: this method rebuilds classification and
        clears any derived state tied to the previously attached router. In
        normal use it is called exactly once, immediately after
        ``build_router`` returns.
        """
        self.fixed_router = fixed_router
        self.classified = {}
        self._adapter_sub_type = {}
        self._pending_decisions = {}
        self._api_adapter_prices = {}
        self._latency_profiles = {}
        self._swrr_samplers = {}
        self._last_lp_times = {}
        self._last_lp_weights = {}
        self._last_lp_statuses = {}
        self._shadow_hedge_log = []
        self._api_endpoint_map = {}
        self._pending_lp_solves = set()
        self._classify_all()
        self._build_adapter_sub_type_map()
        self._precompute_api_prices()
        self._validate_api_baseline()
        self._init_latency_profiles()

    # ------------------------------------------------------------------
    # ProviderEventSink conformance
    # ------------------------------------------------------------------

    def on_provider_success(self, provider: str) -> None:
        """Record a successful request for *provider* (ProviderEventSink)."""
        self._on_success(provider)

    def on_provider_failure(self, provider: str, reason: str) -> None:
        """Record a failed request for *provider* (ProviderEventSink)."""
        self._on_failure(provider, reason=reason)

    # ------------------------------------------------------------------
    # Classification
    # ------------------------------------------------------------------

    def _classify_all(self) -> None:
        """Walk every route in FixedRouter and classify adapters."""
        for model_id, route_cfg in self.fixed_router.routes.items():
            entries: list[tuple[Any, float, SubscriptionType]] = []
            for adapter, weight in route_cfg.adapters:
                if weight <= 0:
                    continue  # Respect FixedRouter's disabled-route convention
                metadata = getattr(adapter.config, "route_metadata", None)
                if isinstance(metadata, dict):
                    sub_str = metadata.get(
                        "subscription_type", getattr(adapter.config, "subscription_type", "api")
                    )
                else:
                    sub_str = getattr(adapter.config, "subscription_type", "api")
                try:
                    sub_type = SubscriptionType(sub_str)
                except ValueError:
                    logger.warning(
                        "Unknown subscription_type '%s' for %s; defaulting to API",
                        sub_str,
                        adapter.config.endpoint_id or adapter.config.id,
                    )
                    sub_type = SubscriptionType.API
                entries.append((adapter, weight, sub_type))
            self.classified[model_id] = entries

    def _build_adapter_sub_type_map(self) -> None:
        """Build reverse lookup from ``id(adapter)`` to ``SubscriptionType``.

        Called after ``_classify_all()`` so the execution overrides
        (``_execute_adapter``, ``_execute_stream_adapter``) can determine
        whether a given adapter is S_C without touching ``self.classified``.
        """
        self._adapter_sub_type = {}
        for entries in self.classified.values():
            for adapter, _w, sub in entries:
                self._adapter_sub_type[id(adapter)] = sub

    def _validate_api_baseline(self) -> None:
        """Warn if any model lacks an S_A baseline adapter.

        Without S_A, the value estimation (v_t) is undefined (inf from
        cheapest-API lookup) and the last-resort fallthrough in
        ``_select_adapter`` has no safe adapter to return.
        """
        for model_id, entries in self.classified.items():
            has_api = any(s is SubscriptionType.API for _, _, s in entries)
            if not has_api:
                logger.warning(
                    "Model '%s' has no S_A (API) adapter. "
                    "RouteWise routing requires at least one S_A baseline "
                    "for value estimation; requests may return None when "
                    "S_C/S_Q resources are exhausted.",
                    model_id,
                )

    # ------------------------------------------------------------------
    # Per-request API cost computation
    # ------------------------------------------------------------------

    def _precompute_api_prices(self) -> None:
        """Cache per-token prices for every S_A adapter per model.

        Prices in ``models.yaml`` are per-1M-token; we store them as
        per-token for direct multiplication in value estimation.
        """
        for model_id, entries in self.classified.items():
            api_list: list[tuple[Any, float, float]] = []
            for adapter, _w, sub in entries:
                if sub is not SubscriptionType.API:
                    continue
                pricing = adapter.config.pricing
                p_in = float(pricing.get("prompt", "0")) / 1_000_000.0
                p_out = float(pricing.get("completion", "0")) / 1_000_000.0
                api_list.append((adapter, p_in, p_out))
            if api_list:
                self._api_adapter_prices[model_id] = api_list

    # ------------------------------------------------------------------
    # Layer 2: Latency-aware provider selection
    # ------------------------------------------------------------------

    def _init_latency_profiles(self) -> None:
        """Initialize latency profiles, endpoint map, and per-model SWRR samplers."""
        for model_id, entries in self.classified.items():
            has_api = False
            for adapter, _w, sub in entries:
                if sub is not SubscriptionType.API:
                    continue
                has_api = True
                eid = adapter.config.endpoint_id or adapter.config.id
                if eid not in self._latency_profiles:
                    self._latency_profiles[eid] = ProviderProfile(
                        endpoint_id=eid,
                        window_sec=self.config.latency_window_sec,
                    )
                if eid not in self._api_endpoint_map:
                    pricing = adapter.config.pricing
                    p_in = float(pricing.get("prompt", "0")) / 1_000_000.0
                    p_out = float(pricing.get("completion", "0")) / 1_000_000.0
                    self._api_endpoint_map[eid] = (adapter, p_in, p_out)
            if has_api:
                self._swrr_samplers[model_id] = SWRRSampler(
                    alpha=self.config.latency_swrr_alpha,
                )
                self._last_lp_times[model_id] = 0.0
                self._last_lp_weights[model_id] = {}
                self._last_lp_statuses[model_id] = "not_run"

    def _select_api_adapter(
        self,
        model_id: str,
        prompt_tokens: int,
        predicted_output: float,
    ) -> tuple[BaseAdapter | None, float]:
        """Layer 2 entry point: select an S_A adapter with latency awareness.

        Falls back to ``_cheapest_api_for_request`` when:
        - Single S_A provider for this model.
        - Fewer than 2 warmed profiles (< min_samples observations).

        Each model_id has its own SWRR sampler and LP state, so
        multi-model routing never cross-contaminates.

        Args:
            model_id: Model identifier.
            prompt_tokens: Number of input tokens.
            predicted_output: Predicted output token count.

        Returns:
            Tuple of (selected adapter, estimated cost).
        """
        api_list = self._api_adapter_prices.get(model_id, [])
        if len(api_list) <= 1:
            return self._cheapest_api_for_request(model_id, prompt_tokens, predicted_output)

        # Collect endpoint IDs for this model's S_A adapters.
        model_eids: list[str] = []
        for adapter, _p_in, _p_out in api_list:
            eid = adapter.config.endpoint_id or adapter.config.id
            model_eids.append(eid)

        # Check warmup: need >= 2 profiles with sufficient samples.
        now = time.time()
        warmed = [
            eid
            for eid in model_eids
            if eid in self._latency_profiles
            and self._latency_profiles[eid].sample_count(now) >= self.config.latency_min_samples
        ]
        if len(warmed) < 2:
            return self._cheapest_api_for_request(model_id, prompt_tokens, predicted_output)

        # LP + SWRR path (per-model state).
        self._maybe_update_lp(model_id, model_eids, prompt_tokens, predicted_output, now)

        sampler = self._swrr_samplers.get(model_id)
        if sampler is None:
            return self._cheapest_api_for_request(model_id, prompt_tokens, predicted_output)

        selected_eid = sampler.sample()
        if selected_eid is None or selected_eid not in self._api_endpoint_map:
            return self._cheapest_api_for_request(model_id, prompt_tokens, predicted_output)

        adapter, p_in, p_out = self._api_endpoint_map[selected_eid]
        cost = p_in * prompt_tokens + p_out * predicted_output

        # Hedge mode dispatch.
        if self.config.latency_hedge_mode == "shadow":
            self._compute_shadow_hedge(model_id, selected_eid, model_eids, now)
        elif self.config.latency_hedge_mode == "economic":
            hedged = self._maybe_create_hedged_adapter(
                model_id,
                selected_eid,
                model_eids,
                now,
            )
            if hedged is not None:
                return hedged, cost

        return adapter, cost

    def _maybe_update_lp(
        self,
        model_id: str,
        endpoint_ids: list[str],
        prompt_tokens: int,
        predicted_output: float,
        current_time: float,
    ) -> None:
        """Schedule LP re-solve if enough time has elapsed.

        The LP solve runs in a thread pool to avoid blocking the event loop.
        The current request uses cached weights; the next request after the
        solve completes will use the updated weights.

        Args:
            model_id: Model identifier for per-model state lookup.
            endpoint_ids: Candidate endpoint IDs for this model.
            prompt_tokens: Current request prompt tokens (for cost computation).
            predicted_output: Predicted output tokens.
            current_time: Current Unix timestamp.
        """
        last_lp_time = self._last_lp_times.get(model_id, 0.0)
        if (current_time - last_lp_time) < self.config.latency_lp_interval_sec:
            return

        # Skip if there's already an in-flight solve for this model.
        if model_id in self._pending_lp_solves:
            return

        # Eagerly update timestamp to prevent duplicate triggers.
        self._last_lp_times[model_id] = current_time

        # Snapshot inputs for the thread-safe solve.
        solve_args = self._prepare_lp_solve_args(
            endpoint_ids, prompt_tokens, predicted_output, current_time
        )
        if solve_args is None:
            return

        # Try to schedule in thread pool; fall back to sync for tests.
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            result = self._run_lp_solve(*solve_args)
            self._apply_lp_result(model_id, result, current_time)
            return

        self._pending_lp_solves.add(model_id)

        def _on_done(future: asyncio.Future) -> None:  # type: ignore[type-arg]
            self._pending_lp_solves.discard(model_id)
            try:
                result = future.result()
                self._apply_lp_result(model_id, result, current_time)
            except Exception:
                logger.warning("LP solve failed for model %s", model_id, exc_info=True)

        fut = loop.run_in_executor(None, self._run_lp_solve, *solve_args)
        fut.add_done_callback(_on_done)

    def _prepare_lp_solve_args(
        self,
        endpoint_ids: list[str],
        prompt_tokens: int,
        predicted_output: float,
        current_time: float,
    ) -> tuple[list[str], dict[str, float], float, float, float, tuple[float, ...]] | None:
        """Prepare arguments for the LP solve (read-only snapshot).

        Returns None if no eligible endpoints are available.
        """
        profiles_subset = {
            eid: self._latency_profiles[eid]
            for eid in endpoint_ids
            if eid in self._latency_profiles
        }
        eligible = pre_filter_providers(profiles_subset, current_time)
        if not eligible:
            eligible = list(profiles_subset.keys())
        if not eligible:
            return None

        costs: dict[str, float] = {}
        for eid in eligible:
            if eid in self._api_endpoint_map:
                _, p_in, p_out = self._api_endpoint_map[eid]
                costs[eid] = p_in * prompt_tokens + p_out * predicted_output
            else:
                costs[eid] = 1.0

        try:
            factors = tuple(float(f) for f in self.config.latency_relaxation_factors.split(","))
        except (ValueError, AttributeError):
            factors = (1.2, 1.5, 2.0)

        return (
            eligible,
            costs,
            self.config.latency_slo_sec,
            current_time,
            self.config.latency_target_cdf,
            factors,
        )

    def _run_lp_solve(
        self,
        eligible: list[str],
        costs: dict[str, float],
        slo_sec: float,
        current_time: float,
        target_cdf: float,
        factors: tuple[float, ...],
    ) -> tuple[dict[str, float], str]:
        """Execute LP solve (CPU-bound, thread-safe).

        This method accesses ``_latency_profiles`` read-only.  Profile
        updates from ``record_observation`` on the event loop are atomic
        (deque append + scalar update), so data races are benign.
        """
        return solve_provider_lp_with_relaxation(
            endpoint_ids=eligible,
            profiles={
                eid: self._latency_profiles[eid]
                for eid in eligible
                if eid in self._latency_profiles
            },
            costs=costs,
            slo_sec=slo_sec,
            current_time=current_time,
            target_cdf=target_cdf,
            kappa=self.config.latency_error_penalty,
            relaxation_factors=factors,
        )

    def _apply_lp_result(
        self,
        model_id: str,
        result: tuple[dict[str, float], str],
        current_time: float,
    ) -> None:
        """Apply LP solve result to per-model state (event-loop thread only)."""
        weights, status = result

        sampler = self._swrr_samplers.get(model_id)
        if sampler is None:
            sampler = SWRRSampler(alpha=self.config.latency_swrr_alpha)
            self._swrr_samplers[model_id] = sampler
        sampler.update_weights(weights)

        self._last_lp_weights[model_id] = weights
        self._last_lp_statuses[model_id] = status

        logger.debug(
            "Layer 2 LP update: model=%s status=%s, weights=%s",
            model_id,
            status,
            weights,
        )

    def _log_shadow_hedge(self, decision: ShadowHedgeDecision) -> None:
        """Append shadow hedge decision with bounded log size."""
        if len(self._shadow_hedge_log) >= self._shadow_hedge_log_maxlen:
            # Evict oldest half to amortize cost
            self._shadow_hedge_log = self._shadow_hedge_log[self._shadow_hedge_log_maxlen // 2 :]
        self._shadow_hedge_log.append(decision)

    def _compute_shadow_hedge(
        self,
        model_id: str,
        primary_eid: str,
        candidate_eids: list[str],
        current_time: float,
    ) -> None:
        """Compute and log a shadow hedge decision (no actual dispatch).

        Uses SMART_ECONOMIC ``compute_hedge_threshold()`` to determine
        whether hedging is cost-justified.  Logs "hedge_warranted" when
        h* < inf, "hedge_not_justified" otherwise.

        Args:
            model_id: Model that triggered this decision.
            primary_eid: Endpoint selected by SWRR.
            candidate_eids: All candidate endpoints for this model.
            current_time: Current Unix timestamp.
        """
        backups = [eid for eid in candidate_eids if eid != primary_eid]
        if not backups:
            self._log_shadow_hedge(
                ShadowHedgeDecision(
                    model_id=model_id,
                    primary_endpoint=primary_eid,
                    backup_endpoint=None,
                    hedge_threshold_sec=None,
                    reason="no_backup",
                    timestamp=current_time,
                )
            )
            return

        # Find backup with lowest p50.
        best_backup = min(
            backups,
            key=lambda eid: (
                self._latency_profiles[eid].percentile(50, current_time)
                if eid in self._latency_profiles
                else float("inf")
            ),
        )

        # Check backup has sufficient samples.
        backup_profile = self._latency_profiles.get(best_backup)
        primary_profile = self._latency_profiles.get(primary_eid)
        if (
            backup_profile is None
            or primary_profile is None
            or backup_profile.sample_count(current_time) < self.config.latency_min_samples
        ):
            self._log_shadow_hedge(
                ShadowHedgeDecision(
                    model_id=model_id,
                    primary_endpoint=primary_eid,
                    backup_endpoint=best_backup,
                    hedge_threshold_sec=None,
                    reason="insufficient_samples",
                    timestamp=current_time,
                )
            )
            return

        h_star = compute_hedge_threshold(
            primary_profile=primary_profile,
            backup_profile=backup_profile,
            slo_sec=self.config.latency_slo_sec,
            cost_ratio=self.config.latency_hedge_cost_ratio,
            dispatch_overhead_sec=self.config.latency_hedge_dispatch_overhead_sec,
            current_time=current_time,
        )

        if h_star == float("inf"):
            reason = "hedge_not_justified"
            hedge_threshold: float | None = None
        else:
            reason = "hedge_warranted"
            hedge_threshold = h_star

        self._log_shadow_hedge(
            ShadowHedgeDecision(
                model_id=model_id,
                primary_endpoint=primary_eid,
                backup_endpoint=best_backup,
                hedge_threshold_sec=hedge_threshold,
                reason=reason,
                timestamp=current_time,
            )
        )

        logger.debug(
            "Shadow hedge: model=%s primary=%s backup=%s reason=%s threshold=%s",
            model_id,
            primary_eid,
            best_backup,
            reason,
            hedge_threshold,
        )

    def _maybe_create_hedged_adapter(
        self,
        model_id: str,
        primary_eid: str,
        candidate_eids: list[str],
        current_time: float,
    ) -> HedgedAdapter | None:
        """Create a HedgedAdapter if hedging is cost-justified.

        Finds the fastest backup provider (lowest p50), checks that both
        profiles have sufficient samples, computes h* via SMART_ECONOMIC
        grid search, and returns a HedgedAdapter if h* < inf.

        Args:
            model_id: Model identifier.
            primary_eid: Primary endpoint selected by SWRR.
            candidate_eids: All candidate endpoint IDs for this model.
            current_time: Current Unix timestamp.

        Returns:
            A HedgedAdapter wrapping primary and backup, or None if
            hedging is not justified.
        """
        backups = [eid for eid in candidate_eids if eid != primary_eid]
        if not backups:
            return None

        # Find backup with lowest p50.
        best_backup_eid = min(
            backups,
            key=lambda eid: (
                self._latency_profiles[eid].percentile(50, current_time)
                if eid in self._latency_profiles
                else float("inf")
            ),
        )

        primary_profile = self._latency_profiles.get(primary_eid)
        backup_profile = self._latency_profiles.get(best_backup_eid)

        if primary_profile is None or backup_profile is None:
            return None

        # Check both profiles have sufficient samples.
        if (
            primary_profile.sample_count(current_time) < self.config.latency_min_samples
            or backup_profile.sample_count(current_time) < self.config.latency_min_samples
        ):
            return None

        h_star = compute_hedge_threshold(
            primary_profile=primary_profile,
            backup_profile=backup_profile,
            slo_sec=self.config.latency_slo_sec,
            cost_ratio=self.config.latency_hedge_cost_ratio,
            dispatch_overhead_sec=self.config.latency_hedge_dispatch_overhead_sec,
            current_time=current_time,
        )

        if h_star == float("inf"):
            return None

        # Look up adapters for primary and backup.
        primary_entry = self._api_endpoint_map.get(primary_eid)
        backup_entry = self._api_endpoint_map.get(best_backup_eid)
        if primary_entry is None or backup_entry is None:
            return None

        primary_adapter = primary_entry[0]
        backup_adapter = backup_entry[0]

        logger.debug(
            "Creating HedgedAdapter: model=%s primary=%s backup=%s h*=%.3f",
            model_id,
            primary_eid,
            best_backup_eid,
            h_star,
        )

        return HedgedAdapter(
            primary=primary_adapter,
            backup=backup_adapter,
            hedge_threshold_sec=h_star,
            event_sink=self,
        )

    def _cheapest_api_for_request(
        self,
        model_id: str,
        prompt_tokens: int,
        predicted_output: float,
    ) -> tuple[BaseAdapter | None, float]:
        """Return (adapter, estimated_cost) for the cheapest S_A option.

        The cost is ``p_in * prompt_tokens + p_out * predicted_output``,
        computed per adapter so the winner can change depending on the
        prompt/output ratio of the current request.

        Args:
            model_id: Model identifier.
            prompt_tokens: Number of input tokens in the current request.
            predicted_output: Predicted output token count.

        Returns:
            Tuple of (cheapest adapter, estimated cost).  If no S_A adapters
            exist, returns ``(None, inf)``.
        """
        api_list = self._api_adapter_prices.get(model_id, [])
        if not api_list:
            return None, float("inf")

        best_adapter: Any = None
        best_cost = float("inf")
        for adapter, p_in, p_out in api_list:
            cost = p_in * prompt_tokens + p_out * predicted_output
            if cost < best_cost:
                best_cost = cost
                best_adapter = adapter
        return best_adapter, best_cost

    # ------------------------------------------------------------------
    # Value estimation
    # ------------------------------------------------------------------

    def _estimate_value(self, model_id: str, prompt_tokens: int) -> float:
        """Estimate the API cost ``v_t`` saved by routing to S_Q.

        Computes the minimum over all S_A adapters of
        ``p_in * prompt_tokens + p_out * predicted_output``, so the cheapest
        baseline is chosen per-request rather than fixed at init time.

        For PD (``config.decision_rule == "pd"``), uses the median (q50)
        prediction.  For LA-PD (``"lapd"``), uses the conservative lower
        confidence bound (q10).

        Args:
            model_id: The model being requested.
            prompt_tokens: Number of prompt tokens in the current request.

        Returns:
            Estimated API cost in dollars.
        """
        prediction = self.predictor.predict(model_id)
        predicted_out = prediction.lcb if self.config.decision_rule == "lapd" else prediction.median
        _, cost = self._cheapest_api_for_request(model_id, prompt_tokens, predicted_out)
        return cost if cost < float("inf") else 0.0

    # ------------------------------------------------------------------
    # BaseRouter abstract method implementations
    # ------------------------------------------------------------------

    def _is_eligible(self, sub: SubscriptionType) -> bool:
        """Check whether an adapter with *sub* type is eligible.

        Concurrency adapters (S_C) are only eligible when
        ``concurrency_enabled`` is True.  Quota and API adapters are always
        eligible.  Used as the last-resort filter in the fallthrough path
        at the end of ``_select_adapter``.
        """
        if sub is SubscriptionType.CONCURRENCY:
            return self.config.concurrency_enabled
        return True

    def _select_adapter(self, model_id: str, context: dict[str, Any]) -> BaseAdapter | None:
        """Select an adapter for *model_id* using three-tier PD decision logic.

        Priority cascade: **S_C > S_Q > S_A**.

        Decision algorithm:
        1. Estimate prompt_tokens from context.
        2. Compute predicted output tokens via the EMA predictor.
        3. Compute value ``v_t`` (cheapest S_A cost for this request).
        4. Compute gains:
           - ``gain_C = v_t`` if S_C adapters exist and slots available, else ``-inf``.
           - ``gain_Q = v_t - theta_Q`` if S_Q adapters exist, quota > 0,
             and ``v_t >= theta_Q``, else ``-inf``.
           - ``gain_A = 0`` (baseline).
        5. Select the tier with the highest gain.
        6. Selection-commit: acquire slot (S_C) or consume quota (S_Q).

        Since ``theta_Q > 0`` always (L_seed > 0), ``gain_C = v_t > v_t - theta_Q = gain_Q``
        whenever both are available.

        Args:
            model_id: Model identifier.
            context: Routing context; may contain ``"prompt_tokens"`` or
                ``"messages"`` (list of message dicts).

        Returns:
            Selected adapter, or None if no eligible adapter exists.
        """
        entries = self.classified.get(model_id)
        if entries is None:
            raise ValueError(f"RouteWiseRouter has no route for model '{model_id}'")

        # -- Prompt tokens ------------------------------------------------
        prompt_tokens = context.get("prompt_tokens", 0)
        if prompt_tokens <= 0:
            prompt_tokens = estimate_prompt_tokens(context.get("messages") or [])

        # -- Output prediction --------------------------------------------
        prediction = self.predictor.predict(model_id)
        predicted_out = prediction.lcb if self.config.decision_rule == "lapd" else prediction.median

        # -- Value estimation for Layer 1 (uses cheapest API baseline) ------
        _, v_t = self._cheapest_api_for_request(
            model_id,
            prompt_tokens,
            predicted_out,
        )

        # -- Classify adapters by tier ------------------------------------
        conc_adapters = [a for a, _w, s in entries if s is SubscriptionType.CONCURRENCY]
        quota_adapters = [a for a, _w, s in entries if s is SubscriptionType.QUOTA]

        # -- Compute gains ------------------------------------------------
        gain_c = float("-inf")
        if conc_adapters and self.conc_mgr is not None and self.conc_mgr.available > 0:
            gain_c = v_t

        gain_q = float("-inf")
        theta_q = float("inf")
        if quota_adapters:
            theta_q = self.quota_mgr.get_shadow_price()
            if v_t >= theta_q and self.quota_mgr.remaining > 0:
                gain_q = v_t - theta_q

        gain_a = 0.0

        # -- Request ID for decision metadata --------------------------------
        request_id = context.get("request_id")

        # Shared decision metadata fields reused across all tiers.
        def _base_decision() -> dict[str, Any]:
            return {
                "v_t": v_t,
                "gain_c": gain_c,
                "gain_q": gain_q,
                "gain_a": gain_a,
                "theta_q": theta_q if theta_q < float("inf") else None,
                "quota_remaining": self.quota_mgr.remaining,
                "sc_active": self.conc_mgr.active if self.conc_mgr else 0,
                "sc_limit": self.conc_mgr.limit if self.conc_mgr else 0,
                # Wall-clock timestamp for TTL eviction (see _sweep_pending_decisions_once).
                "timestamp": time.time(),
            }

        # -- Tier selection (S_C > S_Q > S_A) -----------------------------
        best_gain = max(gain_c, gain_q, gain_a)

        # Try S_C first.
        if gain_c == best_gain and gain_c > float("-inf"):
            # Selection-commit: acquire slot atomically.
            if self.conc_mgr is not None and self.conc_mgr.try_acquire():
                logger.debug(
                    "PD decision: route to S_C (v_t=%.6f, slots=%d/%d)",
                    v_t,
                    self.conc_mgr.active,
                    self.conc_mgr.limit,
                )
                if request_id:
                    self._pending_decisions[request_id] = {
                        **_base_decision(),
                        "is_streaming": False,
                        "selected_tier": "concurrency",
                        "quota_committed": 0.0,
                        "sc_committed": True,
                        "hedged": False,
                        "backup_won": False,
                        "lp_status": None,
                    }
                return conc_adapters[0]
            # Race lost -- fall through to S_Q.
            logger.debug(
                "PD decision: S_C race lost, falling through to S_Q/S_A",
            )

        # Try S_Q.
        if gain_q >= gain_a and gain_q > float("-inf"):
            self.quota_mgr.consume()
            logger.debug(
                "PD decision: route to S_Q (v_t=%.6f >= theta_Q=%.6f, remaining=%d)",
                v_t,
                theta_q,
                self.quota_mgr.remaining,
            )
            if request_id:
                self._pending_decisions[request_id] = {
                    **_base_decision(),
                    "is_streaming": False,
                    "selected_tier": "quota",
                    "quota_committed": 0.0,
                    "sc_committed": False,
                    "hedged": False,
                    "backup_won": False,
                    "lp_status": None,
                }
            return quota_adapters[0]

        if quota_adapters:
            logger.debug(
                "PD decision: route to S_A (v_t=%.6f < theta_Q=%.6f or remaining=%d)",
                v_t,
                theta_q,
                self.quota_mgr.remaining,
            )

        # -- Layer 2: latency-aware S_A selection -------------------------
        api_adapter, _ = self._select_api_adapter(
            model_id,
            prompt_tokens,
            predicted_out,
        )
        if api_adapter is not None:
            if request_id:
                self._pending_decisions[request_id] = {
                    **_base_decision(),
                    "is_streaming": False,
                    "selected_tier": "api",
                    "quota_committed": 0.0,
                    "sc_committed": False,
                    "hedged": isinstance(api_adapter, HedgedAdapter),
                    "backup_won": False,
                    "lp_status": self._last_lp_statuses.get(model_id),
                }
            return api_adapter

        # Last resort: return any eligible S_A adapter.  S_C and S_Q are
        # excluded because this path bypasses try_acquire() / consume() --
        # returning them here would break slot/quota accounting and cause
        # spurious releases in _execute_adapter's finally block.
        for adapter, _w, sub in entries:
            if sub is SubscriptionType.API:
                if request_id:
                    self._pending_decisions[request_id] = {
                        **_base_decision(),
                        "is_streaming": False,
                        "selected_tier": "api",
                        "quota_committed": 0.0,
                        "sc_committed": False,
                        "hedged": False,
                        "backup_won": False,
                        "lp_status": None,
                    }
                return adapter
        return None

    def _get_fallback_adapters(
        self,
        model_id: str,
        failed_adapter: BaseAdapter,
    ) -> list[BaseAdapter]:
        """Return fallback adapters for *model_id*, excluding the failed one.

        Only S_A adapters are eligible for fallback.  S_Q and S_C are
        excluded because the ``BaseRouter`` fallback path bypasses
        ``_select_adapter`` entirely -- no PD decision is re-evaluated
        and no quota / concurrency accounting is performed.

        * **S_Q excluded**: if primary was S_Q the slot is already
          consumed; if primary was S_A the PD rule said "don't use quota".
        * **S_C excluded**: the fallback path does not call
          ``_execute_adapter`` / ``_execute_stream_adapter``, so the
          slot acquire/release lifecycle cannot be guaranteed.
        """
        entries = self.classified.get(model_id, [])
        return [a for a, _w, s in entries if a is not failed_adapter and s is SubscriptionType.API]

    def record_observation(self, obs: RoutingObservation) -> None:
        """Record a completed request observation.

        Updates the EMA output-token predictor and Layer 2 latency profiles.
        Quota accounting is handled by selection-commit in ``_select_adapter``
        and is intentionally *not* done here -- otherwise failed, cancelled,
        or fallback-to-S_A requests would silently leak quota.

        Args:
            obs: Observation from the completed request.
        """
        strategy_metadata = obs.strategy_metadata if isinstance(obs.strategy_metadata, dict) else {}
        routewise_metadata = strategy_metadata.get("routewise")
        if not isinstance(routewise_metadata, dict):
            routewise_metadata = {}

        if obs.completion_tokens > 0:
            self.predictor.update(obs.model_id, obs.completion_tokens)

        # Layer 2: update latency profile for the endpoint.
        if obs.endpoint_id and obs.endpoint_id in self._latency_profiles:
            now = time.time()
            error_type: str | None = None if obs.success else "error"
            ttft = obs.ttft_ms if obs.ttft_ms is not None else -1.0
            self._latency_profiles[obs.endpoint_id].record(now, ttft, error_type)

        logger.debug(
            "RouteWise observation: model=%s endpoint=%s completion_tokens=%d success=%s "
            "selected_tier=%s lp_status=%s",
            obs.model_id,
            obs.endpoint_id,
            obs.completion_tokens,
            obs.success,
            routewise_metadata.get("selected_tier"),
            routewise_metadata.get("lp_status"),
        )

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
        """Execute adapter with S_C slot release and backup_won detection.

        If *adapter* was selected as S_C, the concurrency slot acquired in
        ``_select_adapter`` is released here regardless of success or failure.

        For HedgedAdapter, detects config swap (backup won) and records it
        in ``_pending_decisions``.
        """
        is_sc = self._adapter_sub_type.get(id(adapter)) is SubscriptionType.CONCURRENCY
        original_config = adapter.config if isinstance(adapter, HedgedAdapter) else None
        try:
            result = await super()._execute_adapter(adapter, model_id, messages, **params)
            if original_config is not None and adapter.config is not original_config:
                request_id = params.get("request_id")
                if request_id and request_id in self._pending_decisions:
                    self._pending_decisions[request_id]["backup_won"] = True
            return result
        finally:
            if is_sc and self.conc_mgr is not None:
                self.conc_mgr.release()

    async def _execute_stream_adapter(
        self,
        adapter: Any,
        model_id: str,
        messages: list[dict[str, Any]],
        **params: Any,
    ) -> AsyncIterator[Any]:
        """Execute streaming adapter with S_C slot release and backup_won detection.

        Mirrors ``_execute_adapter`` for the streaming path.  The slot is
        released when the generator exits (normal completion, error, or
        ``GeneratorExit`` from cancellation).

        For HedgedAdapter, config swap happens before first yield, so we
        check in the finally block.
        """
        is_sc = self._adapter_sub_type.get(id(adapter)) is SubscriptionType.CONCURRENCY
        original_config = adapter.config if isinstance(adapter, HedgedAdapter) else None
        try:
            async for chunk in super()._execute_stream_adapter(
                adapter, model_id, messages, **params
            ):
                yield chunk
        finally:
            if original_config is not None and adapter.config is not original_config:
                request_id = params.get("request_id")
                if request_id and request_id in self._pending_decisions:
                    self._pending_decisions[request_id]["backup_won"] = True
            if is_sc and self.conc_mgr is not None:
                self.conc_mgr.release()

    # ------------------------------------------------------------------
    # chat_completion / stream_chat_completion: merge decision metadata
    # ------------------------------------------------------------------

    async def chat_completion(
        self, model_id: str, messages: list[dict[str, Any]], **params: Any
    ) -> dict[str, Any]:
        """Execute chat completion and merge RouteWise decision metadata.

        Follows the same ``_pending_decisions`` pattern as NimbusRouter:
        ensures request_id exists, calls super(), then merges decision
        metadata into ``resp["_routing"]["routewise"]``.
        """
        if not params.get("request_id"):
            params["request_id"] = f"req-{uuid.uuid4().hex[:12]}"
        request_id = params["request_id"]

        try:
            resp = await super().chat_completion(model_id, messages, **params)
        except BaseException as e:
            decision_info = self._pending_decisions.pop(request_id, None)
            if decision_info:
                exc_routing = getattr(e, "_routing", None)
                if exc_routing is not None:
                    exc_routing["routewise"] = decision_info
            raise

        decision_info = self._pending_decisions.pop(request_id, None)
        if decision_info and isinstance(resp, dict) and "_routing" in resp:
            resp["_routing"]["routewise"] = decision_info
        return resp

    async def stream_chat_completion(
        self, model_id: str, messages: list[dict[str, Any]], **params: Any
    ) -> AsyncIterator[Any]:
        """Stream chat completion and inject RouteWise decision metadata.

        Buffers the ``[DONE]`` sentinel, injects a routing metadata chunk
        containing ``_routing.routewise``, then yields ``[DONE]``.
        Same pattern as NimbusRouter.
        """
        if not params.get("request_id"):
            params["request_id"] = f"req-{uuid.uuid4().hex[:12]}"
        request_id = params["request_id"]

        if request_id in self._pending_decisions:
            self._pending_decisions[request_id]["is_streaming"] = True

        done_chunk: str | None = None

        try:
            async for chunk in super().stream_chat_completion(model_id, messages, **params):
                if isinstance(chunk, str) and chunk.strip() == "data: [DONE]":
                    done_chunk = chunk
                    continue
                yield chunk
        except BaseException as e:
            decision_info = self._pending_decisions.pop(request_id, None)
            if decision_info:
                exc_routing = getattr(e, "_routing", None)
                if exc_routing is not None:
                    exc_routing["routewise"] = decision_info
            raise

        decision_info = self._pending_decisions.pop(request_id, None)
        if decision_info:
            routing_chunk = {
                "choices": [],
                "_routing": {"routewise": decision_info},
            }
            yield f"data: {json.dumps(routing_chunk)}\n\n"

        if done_chunk:
            yield done_chunk

    # ------------------------------------------------------------------
    # Lifecycle: TTL sweep for _pending_decisions
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the periodic ``_pending_decisions`` TTL sweep task.

        Idempotent — calling it again while the sweep task is already running
        is a no-op so AppServices restart paths don't double-schedule.
        """
        if self._sweep_task is not None and not self._sweep_task.done():
            return
        self._sweep_task = asyncio.create_task(
            self._sweep_pending_decisions_loop(),
            name="RouteWiseRouter.sweep_pending_decisions",
        )

    async def stop(self) -> None:
        """Cancel the periodic ``_pending_decisions`` sweep task cleanly."""
        import contextlib

        task = self._sweep_task
        self._sweep_task = None
        if task is None:
            return
        task.cancel()
        # Cancellation is the expected exit path; swallow other errors so
        # shutdown can proceed even if the loop raised on its way out.
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    async def _sweep_pending_decisions_loop(self) -> None:
        """Run the TTL sweep on a fixed interval until cancelled."""
        try:
            while True:
                await asyncio.sleep(PENDING_DECISIONS_SWEEP_INTERVAL_SECONDS)
                try:
                    await self._sweep_pending_decisions_once()
                except Exception:
                    logger.exception("RouteWise pending-decisions sweep failed")
        except asyncio.CancelledError:
            return

    async def _sweep_pending_decisions_once(self) -> int:
        """Evict stale ``_pending_decisions`` entries; return count evicted.

        Entries whose ``timestamp`` is older than
        :data:`PENDING_DECISIONS_TTL_SECONDS` are removed and a
        ``routewise_decision_evicted`` log event is emitted for each.
        Active streaming requests are skipped because their generator may
        legitimately remain open longer than the fixed TTL.
        """
        now = time.time()
        cutoff = now - PENDING_DECISIONS_TTL_SECONDS
        evicted = 0
        stale: list[tuple[str, float]] = []
        for request_id, decision in self._pending_decisions.items():
            if decision.get("is_streaming"):
                continue
            ts = decision.get("timestamp")
            if not isinstance(ts, (int, float)):
                # Defensive: skip entries with no usable timestamp rather
                # than evicting them (can't compute an age).
                continue
            if ts < cutoff:
                stale.append((request_id, float(ts)))
        for request_id, ts in stale:
            # Only count + log an actual eviction. Completion paths pop
            # without coordinating with the sweep, so an entry we picked up
            # in ``stale`` may already have been consumed by a finishing
            # request by the time we reach this pop. Treat that race as
            # "not evicted" rather than emitting a misleading event.
            if self._pending_decisions.pop(request_id, None) is None:
                continue
            evicted += 1
            logger.info(
                "routewise_decision_evicted",
                extra={
                    "event": "routewise_decision_evicted",
                    "request_id": request_id,
                    "age_sec": int(now - ts),
                },
            )
        return evicted
