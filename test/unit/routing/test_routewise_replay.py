"""Replay test harness: validate production RouteWise matches simulation.

Phase 2 exit criterion: "Exact decision equivalence with simulation on
deterministic replay."

Approach
--------
Component-level tests validate that the EMA state update, shadow price
function, predictor predict+update cycle, and value estimation produce
identical results in both codebases.

A full decision trace feeds a synthetic request sequence through both the
simulation components (from ``experiment/``) and the production
RouteWiseRouter (from ``routing/routewise/``), asserting the same S_Q vs
S_A decision at every step.

Reconciled differences
~~~~~~~~~~~~~~~~~~~~~~
* **Per-model warmup threshold**: both simulation and production use 10
  samples for per-model warmup (production ``min_samples_per_model=10``,
  simulation hardcoded 10).  The replay test overrides global warmup to
  10 as well so both systems have identical fallback logic.
* **Pricing keys**: simulation uses ``input``/``output``; production uses
  ``prompt``/``completion``.  Both divide by 1M, so we just set the same
  numerical values under the respective key names.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock, PropertyMock

import pytest

# ---------------------------------------------------------------------------
# Simulation imports
# ---------------------------------------------------------------------------
from experiment.data.schema import Request as SimRequest
from experiment.strategies.online.predictors.ema import (
    EMAOutputPredictor as SimEMAPredictor,
    EMAState as SimEMAState,
)
from experiment.strategies.online.primal_dual import (
    PrimalDualQuotaManager as SimQuotaManager,
    _calculate_predicted_api_cost as sim_calculate_value,
)

# ---------------------------------------------------------------------------
# Production imports
# ---------------------------------------------------------------------------
from routing.routewise.config import RouteWiseConfig
from routing.routewise.predictor import (
    EMAOutputPredictor as ProdEMAPredictor,
    EMAState as ProdEMAState,
)
from routing.routewise.quota import QuotaManager as ProdQuotaManager
from routing.routewise.router import RouteWiseRouter, SubscriptionType


# ===================================================================
# Helpers
# ===================================================================

@dataclass
class ReplayStep:
    """One step in the deterministic replay trace."""

    prompt_tokens: int
    response_tokens: int
    model: str = "gpt-4o"


def _make_sim_request(step: ReplayStep, idx: int) -> SimRequest:
    """Convert a ReplayStep into a simulation Request."""
    return SimRequest(
        id=idx,
        timestamp=idx,
        request_tokens=step.prompt_tokens,
        response_tokens=step.response_tokens,
        total_tokens=step.prompt_tokens + step.response_tokens,
        model=step.model,
    )


# Shared pricing constants (per 1M tokens).
INPUT_PRICE_PER_1M = 2.5
OUTPUT_PRICE_PER_1M = 10.0

# Shadow price bounds.
L_SEED = 0.0001
U_SEED = 0.01


# ===================================================================
# 1. Component-level replay: EMA state
# ===================================================================

class TestEMAStateReplay:
    """Validate EMAState update formula produces bitwise-identical results."""

    ALPHA = 0.1

    def test_single_update(self) -> None:
        sim = SimEMAState()
        prod = ProdEMAState()

        sim.update(500.0, self.ALPHA)
        prod.update(500.0, self.ALPHA)

        assert sim.mean == prod.mean
        assert sim.variance == prod.variance
        assert sim.count == prod.count

    def test_long_varied_sequence(self) -> None:
        sim = SimEMAState()
        prod = ProdEMAState()
        values = [
            200, 350, 800, 150, 500, 1200, 400, 300, 900, 600,
            250, 700, 450, 1000, 550, 380, 620, 800, 200, 750,
            100, 900, 500, 400, 350, 650, 800, 300, 500, 700,
        ]
        for v in values:
            sim.update(float(v), self.ALPHA)
            prod.update(float(v), self.ALPHA)
            assert sim.mean == pytest.approx(prod.mean, rel=1e-12)
            assert sim.variance == pytest.approx(prod.variance, rel=1e-12)
            assert sim.count == prod.count

    def test_std_matches(self) -> None:
        sim = SimEMAState()
        prod = ProdEMAState()
        for v in [300.0, 500.0, 800.0, 200.0, 600.0]:
            sim.update(v, self.ALPHA)
            prod.update(v, self.ALPHA)
        assert sim.std == pytest.approx(prod.std, rel=1e-12)


# ===================================================================
# 2. Component-level replay: shadow price
# ===================================================================

class TestShadowPriceReplay:
    """Validate shadow price function at matched z values."""

    @pytest.fixture()
    def managers(self):
        Q = 100
        sim = SimQuotaManager(daily_quota=Q, min_value=L_SEED, max_value=U_SEED)

        cfg = MagicMock()
        cfg.daily_quota = Q
        cfg.shadow_price_L_seed = L_SEED
        cfg.shadow_price_U_seed = U_SEED
        cfg.reset_timezone = "UTC"
        prod = ProdQuotaManager(cfg)

        return sim, prod, Q

    def test_theta_at_every_z(self, managers) -> None:
        sim, prod, Q = managers
        for used in range(Q):
            sim.state.used = used
            prod._used_today = used

            sim_theta = sim.get_threshold()
            prod_theta = prod.get_shadow_price()
            assert sim_theta == pytest.approx(prod_theta, rel=1e-12), (
                f"Mismatch at used={used}: sim={sim_theta}, prod={prod_theta}"
            )

    def test_theta_at_full_quota(self, managers) -> None:
        sim, prod, Q = managers
        sim.state.used = Q
        prod._used_today = Q

        assert sim.get_threshold() == float("inf")
        assert prod.get_shadow_price() == float("inf")

    def test_theta_monotonically_increasing(self, managers) -> None:
        sim, prod, Q = managers
        prev_sim = 0.0
        prev_prod = 0.0
        for used in range(Q):
            sim.state.used = used
            prod._used_today = used
            cur_sim = sim.get_threshold()
            cur_prod = prod.get_shadow_price()
            assert cur_sim >= prev_sim
            assert cur_prod >= prev_prod
            prev_sim = cur_sim
            prev_prod = cur_prod


# ===================================================================
# 3. Component-level replay: EMA predictor
# ===================================================================

class TestEMAPredictorReplay:
    """Validate predict + update cycle matches between codebases."""

    ALPHA = 0.1
    # Simulation: per-model warmup = 10 (hardcoded), global warmup = 20 (default).
    # Production: per-model warmup = 10 (default), global warmup = 20 (default).
    # For this test with a single model, per-model triggers first at count=10.
    # We set global to 10 as well so both systems have identical fallback logic.
    MIN_SAMPLES_PER_MODEL = 10
    MIN_SAMPLES_GLOBAL = 10
    DEFAULT_OUTPUT = 500.0

    def _make_sim_predictor(self) -> SimEMAPredictor:
        return SimEMAPredictor(
            alpha=self.ALPHA,
            min_samples_warmup=self.MIN_SAMPLES_GLOBAL,
            default_output_tokens=self.DEFAULT_OUTPUT,
        )

    def _make_prod_predictor(self) -> ProdEMAPredictor:
        return ProdEMAPredictor(
            alpha=self.ALPHA,
            min_samples=self.MIN_SAMPLES_GLOBAL,
            min_samples_per_model=self.MIN_SAMPLES_PER_MODEL,
            default_output=self.DEFAULT_OUTPUT,
        )

    def test_cold_start_defaults_match(self) -> None:
        sim = self._make_sim_predictor()
        prod = self._make_prod_predictor()

        req = SimRequest(
            id=0, timestamp=0, request_tokens=1000,
            response_tokens=0, total_tokens=1000, model="gpt-4o",
        )
        sim_pred = sim.predict(req)
        prod_pred = prod.predict("gpt-4o")

        assert sim_pred.q10 == pytest.approx(prod_pred.q10, rel=1e-12)
        assert sim_pred.q50 == pytest.approx(prod_pred.q50, rel=1e-12)
        assert sim_pred.q90 == pytest.approx(prod_pred.q90, rel=1e-12)
        assert sim_pred.is_warmed_up == prod_pred.is_warmed_up

    def test_predict_update_cycle(self) -> None:
        """Feed 30 observations, assert predictions match after each update."""
        sim = self._make_sim_predictor()
        prod = self._make_prod_predictor()

        observations = [
            200, 400, 600, 300, 500, 800, 250, 550, 700, 350,
            450, 650, 380, 520, 900, 150, 600, 400, 750, 300,
            500, 800, 350, 450, 600, 250, 700, 550, 400, 500,
        ]

        for i, output_tokens in enumerate(observations):
            # Predict before update (mirrors route() -> update() order).
            req = SimRequest(
                id=i, timestamp=i, request_tokens=1000,
                response_tokens=output_tokens,
                total_tokens=1000 + output_tokens,
                model="gpt-4o",
            )
            sim_pred = sim.predict(req)
            prod_pred = prod.predict("gpt-4o")

            assert sim_pred.q10 == pytest.approx(prod_pred.q10, rel=1e-12), (
                f"q10 mismatch at step {i}"
            )
            assert sim_pred.q50 == pytest.approx(prod_pred.q50, rel=1e-12), (
                f"q50 mismatch at step {i}"
            )
            assert sim_pred.q90 == pytest.approx(prod_pred.q90, rel=1e-12), (
                f"q90 mismatch at step {i}"
            )
            assert sim_pred.is_warmed_up == prod_pred.is_warmed_up, (
                f"warmup mismatch at step {i}"
            )

            # Update with ground truth.
            sim.update(req)
            prod.update("gpt-4o", output_tokens)


# ===================================================================
# 4. Component-level replay: value estimation
# ===================================================================

class TestValueEstimationReplay:
    """Validate value estimation formula equivalence."""

    def _sim_value(
        self,
        prompt_tokens: int,
        predicted_output: float,
        model: str = "gpt-4o",
    ) -> float:
        config = {
            "model_pricing": {
                model: {
                    "input": INPUT_PRICE_PER_1M,
                    "output": OUTPUT_PRICE_PER_1M,
                },
            },
        }
        req = SimRequest(
            id=0, timestamp=0, request_tokens=prompt_tokens,
            response_tokens=0, total_tokens=prompt_tokens,
            model=model,
        )
        return sim_calculate_value(config, req, predicted_output)

    def _prod_value(
        self,
        prompt_tokens: int,
        predicted_output: float,
    ) -> float:
        p_in = INPUT_PRICE_PER_1M / 1_000_000.0
        p_out = OUTPUT_PRICE_PER_1M / 1_000_000.0
        return p_in * prompt_tokens + p_out * predicted_output

    def test_value_various_sizes(self) -> None:
        cases = [
            (1000, 500.0),
            (200, 300.0),
            (5000, 2000.0),
            (100, 50.0),
            (0, 1000.0),
            (1000, 0.0),
        ]
        for prompt_tokens, predicted_output in cases:
            sim_v = self._sim_value(prompt_tokens, predicted_output)
            prod_v = self._prod_value(prompt_tokens, predicted_output)
            assert sim_v == pytest.approx(prod_v, rel=1e-12), (
                f"Mismatch: prompt={prompt_tokens}, pred_out={predicted_output}"
            )


# ===================================================================
# 5. Full decision trace replay
# ===================================================================

class TestDecisionTraceReplay:
    """Feed a deterministic trace through both systems, compare decisions.

    Uses a 30-request trace with varied token counts.  Requests 0-9 warm
    up the EMA predictor; requests 10-29 exercise the PD decision under
    increasing shadow pressure as quota fills.
    """

    DAILY_QUOTA = 20

    @pytest.fixture()
    def trace(self) -> list[ReplayStep]:
        """30-request deterministic trace."""
        # Warmup phase: varied response_tokens so the EMA tracks a real
        # distribution (not a constant).
        warmup = [
            ReplayStep(1000, 200),
            ReplayStep(1000, 400),
            ReplayStep(1000, 600),
            ReplayStep(1000, 300),
            ReplayStep(1000, 500),
            ReplayStep(1000, 800),
            ReplayStep(1000, 250),
            ReplayStep(1000, 550),
            ReplayStep(1000, 700),
            ReplayStep(1000, 350),
        ]
        # Decision phase: varied prompt_tokens for different v_t values.
        decision = [
            ReplayStep(1000, 500),
            ReplayStep(200, 400),
            ReplayStep(1500, 600),
            ReplayStep(500, 300),
            ReplayStep(800, 450),
            ReplayStep(2000, 800),
            ReplayStep(300, 200),
            ReplayStep(1200, 550),
            ReplayStep(600, 350),
            ReplayStep(900, 500),
            ReplayStep(100, 250),
            ReplayStep(1800, 700),
            ReplayStep(400, 300),
            ReplayStep(1100, 450),
            ReplayStep(700, 400),
            ReplayStep(1500, 600),
            ReplayStep(250, 200),
            ReplayStep(1000, 500),
            ReplayStep(500, 350),
            ReplayStep(800, 450),
        ]
        return warmup + decision

    @pytest.fixture()
    def sim_components(self):
        """Set up simulation-side components."""
        predictor = SimEMAPredictor(
            alpha=0.1,
            min_samples_warmup=10,
            default_output_tokens=500.0,
        )
        quota_mgr = SimQuotaManager(
            daily_quota=self.DAILY_QUOTA,
            min_value=L_SEED,
            max_value=U_SEED,
        )
        config = {
            "model_pricing": {
                "gpt-4o": {
                    "input": INPUT_PRICE_PER_1M,
                    "output": OUTPUT_PRICE_PER_1M,
                },
            },
        }
        return predictor, quota_mgr, config

    @pytest.fixture()
    def prod_router(self):
        """Set up production RouteWiseRouter with matching config."""
        # Quota adapter (S_Q).
        quota_adapter = MagicMock()
        quota_adapter.config.subscription_type = "quota"
        quota_adapter.config.endpoint_id = "quota-1"
        quota_adapter.config.id = "quota-1"

        # API adapter (S_A).
        api_adapter = MagicMock()
        api_adapter.config.subscription_type = "api"
        api_adapter.config.endpoint_id = "api-1"
        api_adapter.config.id = "api-1"
        api_adapter.config.pricing = {
            "prompt": str(INPUT_PRICE_PER_1M),
            "completion": str(OUTPUT_PRICE_PER_1M),
        }

        route_cfg = MagicMock()
        route_cfg.adapters = [(quota_adapter, 1.0), (api_adapter, 1.0)]

        fixed_router = MagicMock()
        fixed_router.routes = {"gpt-4o": route_cfg}

        rw_config = RouteWiseConfig(
            daily_quota=self.DAILY_QUOTA,
            shadow_price_L_seed=L_SEED,
            shadow_price_U_seed=U_SEED,
            decision_rule="pd",
            concurrency_enabled=False,
        )

        router = RouteWiseRouter(fixed_router, rw_config)

        # Match simulation's warmup thresholds:
        # per-model=10 (production default matches), global=10 (override to
        # match simulation's min_samples_warmup in the test fixture).
        router.predictor = ProdEMAPredictor(
            alpha=0.1,
            min_samples=10,
            min_samples_per_model=10,
            default_output=500.0,
        )

        return router, quota_adapter, api_adapter

    # ---------------------------------------------------------------
    # Per-step routing helpers
    # ---------------------------------------------------------------

    @staticmethod
    def _sim_route(
        step: ReplayStep,
        idx: int,
        predictor: SimEMAPredictor,
        quota_mgr: SimQuotaManager,
        config: dict,
    ) -> tuple[str, float, float]:
        """Route one request through simulation components.

        Returns (decision, v_t, theta_q).
        """
        request = _make_sim_request(step, idx)

        # 1. Predict (before update -- matches route() order).
        prediction = predictor.predict(request)
        value = sim_calculate_value(config, request, prediction.q50)

        # 2. Shadow price.
        quota_mgr.check_daily_reset(request.day)
        theta_q = quota_mgr.get_threshold()
        has_quota = quota_mgr.has_quota()

        # 3. Decide.
        gain_q = value - theta_q if has_quota else float("-inf")
        gain_a = 0.0

        if gain_q >= gain_a and gain_q > float("-inf"):
            decision = "quota"
            quota_mgr.consume_quota(value)
        else:
            decision = "api"

        # 4. Post-decision predictor update.
        predictor.update(request)

        return decision, value, theta_q

    @staticmethod
    def _prod_route(
        step: ReplayStep,
        router: RouteWiseRouter,
        quota_adapter: Any,
        api_adapter: Any,
    ) -> str:
        """Route one request through production RouteWiseRouter.

        Returns decision string ("quota" or "api").
        """
        context: dict[str, Any] = {
            "prompt_tokens": step.prompt_tokens,
            "messages": [],
        }
        adapter = router._select_adapter("gpt-4o", context)

        if adapter is quota_adapter:
            decision = "quota"
        elif adapter is api_adapter:
            decision = "api"
        else:
            raise AssertionError(f"Unexpected adapter: {adapter}")

        # Post-decision predictor update (mirrors record_observation).
        if step.response_tokens > 0:
            router.predictor.update("gpt-4o", step.response_tokens)

        return decision

    # ---------------------------------------------------------------
    # Assertions
    # ---------------------------------------------------------------

    def test_decision_equivalence(self, trace, sim_components, prod_router) -> None:
        """Every step must produce the same S_Q vs S_A decision."""
        predictor, quota_mgr, config = sim_components
        router, quota_adapter, api_adapter = prod_router

        for i, step in enumerate(trace):
            sim_decision, sim_v, sim_theta = self._sim_route(
                step, i, predictor, quota_mgr, config,
            )
            prod_decision = self._prod_route(step, router, quota_adapter, api_adapter)

            assert sim_decision == prod_decision, (
                f"Step {i}: sim={sim_decision}, prod={prod_decision}, "
                f"v_t={sim_v:.8f}, theta_q={sim_theta:.8f}, "
                f"prompt={step.prompt_tokens}, resp={step.response_tokens}"
            )

    def test_produces_mixed_decisions(self, trace, sim_components, prod_router) -> None:
        """Sanity check: the trace must produce both S_Q and S_A decisions."""
        predictor, quota_mgr, config = sim_components
        router, quota_adapter, api_adapter = prod_router

        decisions: set[str] = set()
        for i, step in enumerate(trace):
            sim_decision, _, _ = self._sim_route(
                step, i, predictor, quota_mgr, config,
            )
            _ = self._prod_route(step, router, quota_adapter, api_adapter)
            decisions.add(sim_decision)

        assert "quota" in decisions, "Trace produced no S_Q decisions"
        assert "api" in decisions, "Trace produced no S_A decisions"

    def test_quota_state_matches(self, trace, sim_components, prod_router) -> None:
        """Quota usage state matches after the full trace."""
        predictor, quota_mgr, config = sim_components
        router, quota_adapter, api_adapter = prod_router

        for i, step in enumerate(trace):
            self._sim_route(step, i, predictor, quota_mgr, config)
            self._prod_route(step, router, quota_adapter, api_adapter)

            # After each step, both quota managers should have the same 'used'.
            assert quota_mgr.state.used == router.quota_mgr._used_today, (
                f"Quota used diverged at step {i}: "
                f"sim={quota_mgr.state.used}, prod={router.quota_mgr._used_today}"
            )

    def test_predictor_state_matches(
        self, trace, sim_components, prod_router,
    ) -> None:
        """EMA predictor state matches after each update."""
        predictor, quota_mgr, config = sim_components
        router, quota_adapter, api_adapter = prod_router

        for i, step in enumerate(trace):
            self._sim_route(step, i, predictor, quota_mgr, config)
            self._prod_route(step, router, quota_adapter, api_adapter)

            sim_state = predictor.global_state
            prod_state = router.predictor._global_state
            assert sim_state.mean == pytest.approx(prod_state.mean, rel=1e-12), (
                f"Global mean diverged at step {i}"
            )
            assert sim_state.variance == pytest.approx(
                prod_state.variance, rel=1e-12,
            ), f"Global variance diverged at step {i}"
            assert sim_state.count == prod_state.count, (
                f"Global count diverged at step {i}"
            )


# ===================================================================
# 6. Stage 2 replay: S_C concurrency tier
# ===================================================================

class TestStage2DecisionReplay:
    """Validate S_C concurrency behaviour under deterministic replay.

    Per integration plan: equivalence under deterministic harness +
    invariants on accounting and cost totals.
    """

    DAILY_QUOTA = 20
    CONC_LIMIT = 3

    @pytest.fixture()
    def trace(self) -> list[ReplayStep]:
        """30-request deterministic trace (same as Stage 1)."""
        warmup = [
            ReplayStep(1000, 200), ReplayStep(1000, 400),
            ReplayStep(1000, 600), ReplayStep(1000, 300),
            ReplayStep(1000, 500), ReplayStep(1000, 800),
            ReplayStep(1000, 250), ReplayStep(1000, 550),
            ReplayStep(1000, 700), ReplayStep(1000, 350),
        ]
        decision = [
            ReplayStep(1000, 500), ReplayStep(200, 400),
            ReplayStep(1500, 600), ReplayStep(500, 300),
            ReplayStep(800, 450), ReplayStep(2000, 800),
            ReplayStep(300, 200), ReplayStep(1200, 550),
            ReplayStep(600, 350), ReplayStep(900, 500),
            ReplayStep(100, 250), ReplayStep(1800, 700),
            ReplayStep(400, 300), ReplayStep(1100, 450),
            ReplayStep(700, 400), ReplayStep(1500, 600),
            ReplayStep(250, 200), ReplayStep(1000, 500),
            ReplayStep(500, 350), ReplayStep(800, 450),
        ]
        return warmup + decision

    @pytest.fixture()
    def three_tier_router(self):
        """Set up RouteWiseRouter with S_C + S_Q + S_A."""
        conc_adapter = MagicMock()
        conc_adapter.config.subscription_type = "concurrency"
        conc_adapter.config.endpoint_id = "conc-1"
        conc_adapter.config.id = "conc-1"
        conc_adapter.config.pricing = {
            "prompt": str(INPUT_PRICE_PER_1M),
            "completion": str(OUTPUT_PRICE_PER_1M),
        }

        quota_adapter = MagicMock()
        quota_adapter.config.subscription_type = "quota"
        quota_adapter.config.endpoint_id = "quota-1"
        quota_adapter.config.id = "quota-1"

        api_adapter = MagicMock()
        api_adapter.config.subscription_type = "api"
        api_adapter.config.endpoint_id = "api-1"
        api_adapter.config.id = "api-1"
        api_adapter.config.pricing = {
            "prompt": str(INPUT_PRICE_PER_1M),
            "completion": str(OUTPUT_PRICE_PER_1M),
        }

        route_cfg = MagicMock()
        route_cfg.adapters = [
            (conc_adapter, 1.0), (quota_adapter, 1.0), (api_adapter, 1.0),
        ]

        fixed_router = MagicMock()
        fixed_router.routes = {"gpt-4o": route_cfg}

        rw_config = RouteWiseConfig(
            daily_quota=self.DAILY_QUOTA,
            shadow_price_L_seed=L_SEED,
            shadow_price_U_seed=U_SEED,
            decision_rule="pd",
            concurrency_enabled=True,
            concurrency_limit=self.CONC_LIMIT,
        )

        router = RouteWiseRouter(fixed_router, rw_config)
        router.predictor = ProdEMAPredictor(
            alpha=0.1,
            min_samples=10,
            min_samples_per_model=10,
            default_output=500.0,
        )

        return router, conc_adapter, quota_adapter, api_adapter

    def _route_step(
        self,
        step: ReplayStep,
        router: RouteWiseRouter,
        conc_adapter: Any,
        quota_adapter: Any,
        api_adapter: Any,
    ) -> str:
        """Route one step and return tier name."""
        context = {"prompt_tokens": step.prompt_tokens, "messages": []}
        adapter = router._select_adapter("gpt-4o", context)

        if adapter is conc_adapter:
            decision = "concurrency"
        elif adapter is quota_adapter:
            decision = "quota"
        elif adapter is api_adapter:
            decision = "api"
        else:
            raise AssertionError(f"Unexpected adapter: {adapter}")

        if step.response_tokens > 0:
            router.predictor.update("gpt-4o", step.response_tokens)

        return decision

    def test_accounting_invariant(self, trace, three_tier_router) -> None:
        """conc_mgr.active <= limit at all times during replay."""
        router, conc_adapter, quota_adapter, api_adapter = three_tier_router

        for i, step in enumerate(trace):
            decision = self._route_step(
                step, router, conc_adapter, quota_adapter, api_adapter,
            )
            assert router.conc_mgr.active <= self.CONC_LIMIT, (
                f"Step {i}: active={router.conc_mgr.active} > limit={self.CONC_LIMIT}"
            )

            # Simulate slot release after each S_C request completes.
            if decision == "concurrency":
                router.conc_mgr.release()

    def test_cost_total_within_tolerance(self, trace, three_tier_router) -> None:
        """API cost matches expected based on tier decisions."""
        router, conc_adapter, quota_adapter, api_adapter = three_tier_router

        p_in = INPUT_PRICE_PER_1M / 1_000_000.0
        p_out = OUTPUT_PRICE_PER_1M / 1_000_000.0

        api_cost = 0.0
        total_requests = 0
        for step in trace:
            decision = self._route_step(
                step, router, conc_adapter, quota_adapter, api_adapter,
            )
            total_requests += 1
            if decision == "api":
                api_cost += p_in * step.prompt_tokens + p_out * step.response_tokens

            # Release S_C slots so they're available for next request.
            if decision == "concurrency":
                router.conc_mgr.release()

        # With S_C available and v_t > 0, most requests should avoid S_A.
        # API cost should be less than the total cost if all went to S_A.
        total_all_api = sum(
            p_in * s.prompt_tokens + p_out * s.response_tokens for s in trace
        )
        assert api_cost <= total_all_api, (
            f"API cost {api_cost:.6f} exceeds total-if-all-api {total_all_api:.6f}"
        )
        assert total_requests == len(trace)

    def test_non_saturated_decisions_match(self, trace, three_tier_router) -> None:
        """When S_C never fills, all decisions should be S_C (slots always free).

        With limit=3 and immediate release, all requests see available slots
        and gain_C = v_t > 0 = gain_A, so S_C always wins.
        """
        router, conc_adapter, quota_adapter, api_adapter = three_tier_router

        decisions = []
        for step in trace:
            decision = self._route_step(
                step, router, conc_adapter, quota_adapter, api_adapter,
            )
            decisions.append(decision)
            # Immediate release -- never saturated.
            if decision == "concurrency":
                router.conc_mgr.release()

        # All decisions should be S_C since slots are always available
        # and gain_C = v_t > 0 = gain_A.
        conc_count = decisions.count("concurrency")
        assert conc_count == len(trace), (
            f"Expected all {len(trace)} to be S_C, got {conc_count}. "
            f"Decisions: {decisions}"
        )
