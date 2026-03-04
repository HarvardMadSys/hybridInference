"""RouteWise cost-aware router with primal-dual decision logic.

This module provides the ``RouteWiseRouter``, a ``BaseRouter`` subclass that
classifies adapters by subscription type (quota / concurrency / API) and
selects among them using a primal-dual (PD) or look-ahead primal-dual (LA-PD)
threshold algorithm.

Layer 1 (PD / LA-PD) decides whether to use S_Q or S_A.  When S_A is chosen
and multiple API providers exist, Layer 2 applies LP-based latency-aware cost
optimization to select among them using empirical latency profiles and SWRR
sampling.

Quota semantics follow dispatch-commit: one request slot is consumed from
the daily quota in ``_select_adapter`` at the moment the decision is made.
This ensures that failed, cancelled, and fallback-to-S_A requests still
account for quota usage, matching the real-world behaviour where the upstream
provider has already received the request.
"""

from __future__ import annotations

import time
from enum import Enum
from typing import TYPE_CHECKING, Any

from routing.routers import BaseRouter, RoutingObservation
from serving.utils.logging import get_logger
from serving.utils.tokens import estimate_prompt_tokens

from .latency import ProviderProfile, ShadowHedgeDecision, SWRRSampler
from .lp_solver import pre_filter_providers, solve_provider_lp_with_relaxation
from .predictor import EMAOutputPredictor
from .quota import QuotaManager

if TYPE_CHECKING:
    from serving.adapters.base import BaseAdapter

    from .config import RouteWiseConfig

logger = get_logger(__name__)


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

    The router classifies adapters into S_Q / S_C / S_A tiers, then applies
    either the PD or LA-PD decision rule (controlled by ``config.decision_rule``)
    to decide whether an incoming request should consume quota (S_Q) or be sent
    to a pay-per-token API (S_A).

    Attributes:
        fixed_router: The shared ``FixedRouter`` whose ``routes`` dict
            provides the per-model adapter lists.
        config: ``RouteWiseConfig`` policy parameters.
        classified: Per-model adapter classification:
            ``{model_id: [(adapter, weight, SubscriptionType), ...]}``.
        predictor: EMA output-token predictor for value estimation.
        quota_mgr: Daily quota manager with shadow price computation.
    """

    def __init__(
        self,
        fixed_router: Any,
        config: RouteWiseConfig,
        experiment_mode: bool = False,
    ) -> None:
        super().__init__(experiment_mode=experiment_mode)
        self.fixed_router = fixed_router
        self.config = config

        # Per-model adapter classification.
        self.classified: dict[str, list[tuple[Any, float, SubscriptionType]]] = {}
        self._classify_all()

        # Online predictors and quota tracking.
        self.predictor = EMAOutputPredictor(
            alpha=0.1,
            min_samples=20,
            default_output=500.0,
        )
        self.quota_mgr = QuotaManager(config)

        # Precomputed per-token prices for all S_A adapters, keyed by model.
        # Each entry: (adapter, price_prompt_per_token, price_completion_per_token).
        self._api_adapter_prices: dict[str, list[tuple[Any, float, float]]] = {}
        self._precompute_api_prices()

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
        # Maps endpoint_id -> (adapter, p_in_per_token, p_out_per_token).
        self._api_endpoint_map: dict[str, tuple[Any, float, float]] = {}
        self._init_latency_profiles()

    # ------------------------------------------------------------------
    # Classification
    # ------------------------------------------------------------------

    def _classify_all(self) -> None:
        """Walk every route in FixedRouter and classify adapters."""
        for model_id, route_cfg in self.fixed_router.routes.items():
            entries: list[tuple[Any, float, SubscriptionType]] = []
            for adapter, weight in route_cfg.adapters:
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

        # Shadow hedge computation.
        if self.config.latency_hedge_mode == "shadow":
            self._compute_shadow_hedge(model_id, selected_eid, model_eids, now)

        return adapter, cost

    def _maybe_update_lp(
        self,
        model_id: str,
        endpoint_ids: list[str],
        prompt_tokens: int,
        predicted_output: float,
        current_time: float,
    ) -> None:
        """Re-solve LP if enough time has elapsed since the last solve.

        All LP state (last solve time, weights, status, SWRR sampler) is
        per-model, so concurrent models never interfere with each other.

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

        # Pre-filter to eligible endpoints.
        profiles_subset = {
            eid: self._latency_profiles[eid]
            for eid in endpoint_ids
            if eid in self._latency_profiles
        }
        eligible = pre_filter_providers(profiles_subset, current_time)
        if not eligible:
            eligible = list(profiles_subset.keys())

        # Compute per-request costs for each endpoint.
        costs: dict[str, float] = {}
        for eid in eligible:
            if eid in self._api_endpoint_map:
                _, p_in, p_out = self._api_endpoint_map[eid]
                costs[eid] = p_in * prompt_tokens + p_out * predicted_output
            else:
                costs[eid] = 1.0

        # Parse relaxation factors.
        try:
            factors = tuple(float(f) for f in self.config.latency_relaxation_factors.split(","))
        except (ValueError, AttributeError):
            factors = (1.2, 1.5, 2.0)

        weights, status = solve_provider_lp_with_relaxation(
            endpoint_ids=eligible,
            profiles={eid: self._latency_profiles[eid] for eid in eligible},
            costs=costs,
            slo_sec=self.config.latency_slo_sec,
            current_time=current_time,
            target_cdf=self.config.latency_target_cdf,
            kappa=self.config.latency_error_penalty,
            relaxation_factors=factors,
        )

        # Update per-model state.
        sampler = self._swrr_samplers.get(model_id)
        if sampler is None:
            sampler = SWRRSampler(alpha=self.config.latency_swrr_alpha)
            self._swrr_samplers[model_id] = sampler
        sampler.update_weights(weights)

        self._last_lp_times[model_id] = current_time
        self._last_lp_weights[model_id] = weights
        self._last_lp_statuses[model_id] = status

        logger.debug(
            "Layer 2 LP update: model=%s status=%s, weights=%s",
            model_id,
            status,
            weights,
        )

    def _compute_shadow_hedge(
        self,
        model_id: str,
        primary_eid: str,
        candidate_eids: list[str],
        current_time: float,
    ) -> None:
        """Compute and log a shadow hedge decision (no actual dispatch).

        Compares the selected provider's p50 latency against backup
        providers.  If a backup has lower p50, logs "hedge_warranted".

        Args:
            model_id: Model that triggered this decision.
            primary_eid: Endpoint selected by SWRR.
            candidate_eids: All candidate endpoints for this model.
            current_time: Current Unix timestamp.
        """
        backups = [eid for eid in candidate_eids if eid != primary_eid]
        if not backups:
            self._shadow_hedge_log.append(
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

        primary_p50 = self._latency_profiles[primary_eid].percentile(50, current_time)

        # Find backup with lowest p50.
        best_backup = min(
            backups,
            key=lambda eid: self._latency_profiles[eid].percentile(50, current_time)
            if eid in self._latency_profiles
            else float("inf"),
        )
        backup_p50 = (
            self._latency_profiles[best_backup].percentile(50, current_time)
            if best_backup in self._latency_profiles
            else float("inf")
        )

        if backup_p50 >= primary_p50:
            reason = "backup_slower"
            hedge_threshold = None
        else:
            reason = "hedge_warranted"
            # Hedge threshold: midpoint between primary p50 and backup p50.
            hedge_threshold = (primary_p50 + backup_p50) / 2.0

        self._shadow_hedge_log.append(
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
        """Check whether an adapter with *sub* type is eligible in Stage 1.

        Concurrency adapters are only eligible when ``concurrency_enabled``
        is True in the config.  Quota and API adapters are always eligible.
        """
        if sub is SubscriptionType.CONCURRENCY:
            return self.config.concurrency_enabled
        return True

    def _select_adapter(self, model_id: str, context: dict[str, Any]) -> BaseAdapter | None:
        """Select an adapter for *model_id* using PD / LA-PD decision logic.

        Decision algorithm:
        1. Estimate prompt_tokens from context (explicit value or message
           heuristic).
        2. Compute predicted output tokens via the EMA predictor.
        3. Find the cheapest S_A adapter for this specific request.
        4. If S_Q adapters exist and ``v_t >= theta_Q`` with quota remaining:
           **dispatch-commit** one request slot and select S_Q.
        5. Otherwise: select cheapest S_A adapter.
        6. S_C gating is unchanged (respects ``_is_eligible``).

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
        # prompt_tokens is arrival-time ground truth (not a prediction).
        # Computed from messages via tiktoken when available, else chars/4.
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

        # -- S_Q decision (Layer 1) ---------------------------------------
        quota_adapters = [a for a, _w, s in entries if s is SubscriptionType.QUOTA]

        if quota_adapters:
            theta_q = self.quota_mgr.get_shadow_price()

            if v_t >= theta_q and self.quota_mgr.remaining > 0:
                # Dispatch-commit: consume one quota slot *now*, before the
                # adapter even executes.  Failed / cancelled requests still
                # count against the daily budget (one request = one slot).
                self.quota_mgr.consume()
                logger.debug(
                    "PD decision: route to S_Q (v_t=%.6f >= theta_Q=%.6f, " "remaining=%d)",
                    v_t,
                    theta_q,
                    self.quota_mgr.remaining,
                )
                return quota_adapters[0]

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
            return api_adapter

        for adapter, _w, sub in entries:
            if self._is_eligible(sub):
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
        * **S_C excluded**: concurrency slot lifecycle (acquire / release)
          is not wired in the fallback path.  S_C will participate in
          fallback once Stage 2 adds proper slot accounting.
        """
        entries = self.classified.get(model_id, [])
        return [a for a, _w, s in entries if a is not failed_adapter and s is SubscriptionType.API]

    def record_observation(self, obs: RoutingObservation) -> None:
        """Record a completed request observation.

        Updates the EMA output-token predictor and Layer 2 latency profiles.
        Quota accounting is handled by dispatch-commit in ``_select_adapter``
        and is intentionally *not* done here -- otherwise failed, cancelled,
        or fallback-to-S_A requests would silently leak quota.

        Args:
            obs: Observation from the completed request.
        """
        if obs.completion_tokens > 0:
            self.predictor.update(obs.model_id, obs.completion_tokens)

        # Layer 2: update latency profile for the endpoint.
        if obs.endpoint_id and obs.endpoint_id in self._latency_profiles:
            now = time.time()
            error_type: str | None = None if obs.success else "error"
            ttft = obs.ttft_ms if obs.ttft_ms is not None else -1.0
            self._latency_profiles[obs.endpoint_id].record(now, ttft, error_type)

        logger.debug(
            "RouteWise observation: model=%s endpoint=%s completion_tokens=%d success=%s",
            obs.model_id,
            obs.endpoint_id,
            obs.completion_tokens,
            obs.success,
        )
