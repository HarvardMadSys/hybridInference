"""RouteWise cost-aware router with primal-dual decision logic.

This module provides the ``RouteWiseRouter``, a ``BaseRouter`` subclass that
classifies adapters by subscription type (quota / concurrency / API) and
selects among them using a primal-dual (PD) or look-ahead primal-dual (LA-PD)
threshold algorithm.

The PD decision rule routes to S_Q when the estimated API cost ``v_t`` exceeds
the shadow price ``theta_Q``.  LA-PD uses a conservative lower confidence bound
(q10) for the output-token prediction instead of the median (q50).

Quota semantics follow dispatch-commit: one request slot is consumed from
the daily quota in ``_select_adapter`` at the moment the decision is made.
This ensures that failed, cancelled, and fallback-to-S_A requests still
account for quota usage, matching the real-world behaviour where the upstream
provider has already received the request.
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, Any

from routing.routers import BaseRouter, RoutingObservation
from serving.utils.logging import get_logger
from serving.utils.tokens import estimate_prompt_tokens

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

        # -- Cheapest S_A for this request --------------------------------
        cheapest_api, v_t = self._cheapest_api_for_request(
            model_id,
            prompt_tokens,
            predicted_out,
        )

        # -- S_Q decision -------------------------------------------------
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

        # -- Fallback to cheapest S_A or first eligible -------------------
        if cheapest_api is not None:
            return cheapest_api

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

        Only updates the EMA output-token predictor.  Quota accounting is
        handled by dispatch-commit in ``_select_adapter`` and is intentionally
        *not* done here -- otherwise failed, cancelled, or fallback-to-S_A
        requests would silently leak quota.

        Args:
            obs: Observation from the completed request.
        """
        if obs.completion_tokens > 0:
            self.predictor.update(obs.model_id, obs.completion_tokens)

        logger.debug(
            "RouteWise observation: model=%s endpoint=%s completion_tokens=%d success=%s",
            obs.model_id,
            obs.endpoint_id,
            obs.completion_tokens,
            obs.success,
        )
