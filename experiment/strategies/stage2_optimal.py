"""ILP-based optimal routing strategy for dual subscriptions (Phase 2).

This module implements the offline optimal solution for routing requests across:
- Daily-quota subscription (S_Q): Fixed daily quota, zero marginal cost
- Concurrency-limited subscription (S_C): Fixed concurrency, zero marginal cost
- Pay-per-token API (S_A): Unlimited capacity, per-token pricing

The optimization is formulated as a Mixed Integer Linear Program (MILP) that
jointly optimizes assignment and scheduling to minimize total API cost.
"""

import logging

import numpy as np
import pulp

from experiment.data.schema import Request, RoutingDecision
from experiment.strategies.base import RoutingStrategy

logger = logging.getLogger(__name__)


class ILPOptimalStrategy(RoutingStrategy):
    """ILP-based optimal strategy for dual subscriptions.

    This strategy solves a joint assignment and scheduling problem using
    Integer Linear Programming to find the globally optimal routing that
    minimizes total cost across two subscription types and an API fallback.

    Time discretization:
    - Divides time into slots of size delta (e.g., 1.0 second)
    - Each request occupies multiple consecutive slots based on its latency
    - Concurrency constraint enforced per slot

    Decision variables:
    - x_i^Q, x_i^C, x_i^A: Binary assignment to daily quota, concurrency, or API
    - tau_i^t: Binary start time for request i at slot t (if assigned to S_C)
    - z_i,t: Binary occupancy of request i at slot t

    Attributes:
        delta: Time slot size in seconds (default: 1.0)
        daily_quota: Daily quota limit (Q)
        concurrency_limit: Concurrency limit (C)
        assignments: Precomputed routing assignments
        schedules: Precomputed start times for concurrency requests
    """

    def __init__(
        self,
        *args,
        delta: float = 1.0,
        daily_quota: int = 5000,
        concurrency_limit: int = 8,
        max_start_delay_slots: int | None = None,
        **kwargs,
    ):
        """Initialize ILP optimal strategy.

        Args:
            delta: Time slot size in seconds
            daily_quota: Daily quota limit
            concurrency_limit: Maximum concurrent requests (total slots)
            max_start_delay_slots: Maximum delay slots for request start time
        """
        super().__init__(*args, **kwargs)
        self.delta = delta
        self.daily_quota = daily_quota
        self.concurrency_limit = concurrency_limit
        self.assignments: dict[int, str] = {}  # request_id -> provider type
        self.schedules: dict[int, tuple[int, int]] = {}  # request_id -> (start_time, finish_time)
        self.max_start_delay_slots = max_start_delay_slots

        # Load model compatibility from config
        self._load_subscription_config()

    def _load_subscription_config(self) -> None:
        """Load model compatibility from subscription config."""
        subscriptions = self.config.get("subscriptions", {})

        # S_Q (Chutes) - Daily Quota
        chutes_config = subscriptions.get("chutes", {})
        self.sq_supported_models: set[str] = set(chutes_config.get("supported_models", []))

        # S_C (Featherless) - Concurrency
        featherless_config = subscriptions.get("featherless", {})
        featherless_models = featherless_config.get("supported_models", {})

        # Build supported models set and multiplier dict
        self.sc_supported_models: set[str] = set()
        self.sc_multipliers: dict[str, int] = {}

        if isinstance(featherless_models, dict):
            # New format: {model: {multiplier: N}}
            for model, props in featherless_models.items():
                self.sc_supported_models.add(model)
                self.sc_multipliers[model] = props.get("multiplier", 1)
        elif isinstance(featherless_models, list):
            # Old format: [model1, model2, ...]
            self.sc_supported_models = set(featherless_models)
            for model in featherless_models:
                self.sc_multipliers[model] = 1

        # Log configuration
        if self.sq_supported_models or self.sc_supported_models:
            logger.info("Model compatibility loaded:")
            logger.info(f"  S_Q (Chutes): {len(self.sq_supported_models)} models")
            logger.info(
                f"  S_C (Featherless): {len(self.sc_supported_models)} models, "
                f"multipliers: {self.sc_multipliers}"
            )

    @property
    def name(self) -> str:
        """Return strategy name."""
        return "ILP-Optimal"

    def precompute(self, requests: list[Request]) -> None:
        """Precompute optimal routing using ILP.

        Args:
            requests: All requests in the dataset
        """
        if not requests:
            return

        logger.info(f"Precomputing ILP-optimal routing for {len(requests)} requests")
        logger.info(
            f"Parameters: Q={self.daily_quota}, C={self.concurrency_limit}, delta={self.delta}s"
        )

        # For large datasets, solve per day to keep problem tractable
        days = {}
        for req in requests:
            if req.day not in days:
                days[req.day] = []
            days[req.day].append(req)

        logger.info(f"Solving ILP for {len(days)} days")

        for day_idx, day_requests in sorted(days.items()):
            logger.info(f"Solving day {day_idx} with {len(day_requests)} requests")
            self._solve_day_ilp(day_requests)

        logger.info(
            f"ILP optimization complete: "
            f"{sum(1 for v in self.assignments.values() if v == 'daily')} daily quota, "
            f"{sum(1 for v in self.assignments.values() if v == 'concurrency')} concurrency, "
            f"{sum(1 for v in self.assignments.values() if v == 'api')} API"
        )

    def _solve_day_ilp(self, day_requests: list[Request]) -> None:
        """Solve ILP for a single day.

        Args:
            day_requests: Requests for this day
        """
        n = len(day_requests)
        if n == 0:
            return

        # Time discretization
        min_time = min(r.timestamp for r in day_requests)

        # Precompute request properties
        api_costs = self._calculate_api_costs(day_requests)
        latencies = np.array([r.latency_seconds for r in day_requests])
        slots_needed = np.ceil(latencies / self.delta).astype(int)
        arrival_slots = (
            (np.array([r.timestamp for r in day_requests]) - min_time) / self.delta
        ).astype(int)

        # Horizon: cover the latest possible finish time (no hard deadlines provided)
        latest_finish_slot = int(np.max(arrival_slots + slots_needed))
        T = latest_finish_slot + 1

        # Create ILP problem
        prob = pulp.LpProblem("DualSubscriptionRouting", pulp.LpMinimize)

        # Get model names for compatibility checking
        models = [r.model for r in day_requests]

        # Precompute compatibility flags
        sq_compatible = [
            (not self.sq_supported_models) or (models[i] in self.sq_supported_models)
            for i in range(n)
        ]
        sc_compatible = [
            (not self.sc_supported_models) or (models[i] in self.sc_supported_models)
            for i in range(n)
        ]

        # Get concurrency multipliers (default 1 if not specified)
        multipliers = [
            self.sc_multipliers.get(models[i], 1) if sc_compatible[i] else 1 for i in range(n)
        ]

        # Log compatibility stats
        sq_count = sum(sq_compatible)
        sc_count = sum(sc_compatible)
        logger.info(f"  Model compatibility: {sq_count}/{n} for S_Q, {sc_count}/{n} for S_C")

        # Decision variables
        x_Q = [pulp.LpVariable(f"x_Q_{i}", cat="Binary") for i in range(n)]
        x_C = [pulp.LpVariable(f"x_C_{i}", cat="Binary") for i in range(n)]
        x_A = [pulp.LpVariable(f"x_A_{i}", cat="Binary") for i in range(n)]

        # Start time variables (only for concurrency)
        tau = {}
        start_windows: dict[int, range] = {}
        active_windows: dict[int, range] = {}
        for i in range(n):
            latest_start = T - slots_needed[i]
            if self.max_start_delay_slots is not None:
                latest_start = min(latest_start, arrival_slots[i] + self.max_start_delay_slots)
            if latest_start < arrival_slots[i]:
                start_windows[i] = range(0, 0)
                active_windows[i] = range(0, 0)
                continue

            start_windows[i] = range(arrival_slots[i], latest_start + 1)
            active_windows[i] = range(arrival_slots[i], latest_start + slots_needed[i])

            for t in start_windows[i]:
                tau[i, t] = pulp.LpVariable(f"tau_{i}_{t}", cat="Binary")

        # Occupancy variables
        z = {}
        for i in range(n):
            for t in active_windows[i]:
                z[i, t] = pulp.LpVariable(f"z_{i}_{t}", cat="Binary")

        # Objective: Minimize API cost
        prob += pulp.lpSum(x_A[i] * api_costs[i] for i in range(n))

        # Constraint 1: Unique assignment
        for i in range(n):
            prob += x_Q[i] + x_C[i] + x_A[i] == 1

        # Constraint 1b: Model compatibility for S_Q
        for i in range(n):
            if not sq_compatible[i]:
                prob += x_Q[i] == 0

        # Constraint 1c: Model compatibility for S_C
        for i in range(n):
            if not sc_compatible[i]:
                prob += x_C[i] == 0

        # Constraint 2: Daily quota (only count compatible requests)
        prob += pulp.lpSum(x_Q[i] for i in range(n) if sq_compatible[i]) <= self.daily_quota

        # Constraint 3: Start time linking
        for i in range(n):
            prob += pulp.lpSum(tau.get((i, t), 0) for t in start_windows[i]) == x_C[i]

        # Constraint 4: Occupancy linearization
        for i in range(n):
            # Exact count of occupied slots
            prob += (
                pulp.lpSum(z.get((i, t), 0) for t in active_windows[i]) == slots_needed[i] * x_C[i]
            )

            # Window cover (upper bound)
            for t in active_windows[i]:
                window_start = max(arrival_slots[i], t - slots_needed[i] + 1)
                window_end = min(t, start_windows[i].stop - 1) if start_windows[i] else t
                prob += z.get((i, t), 0) <= pulp.lpSum(
                    tau.get((i, t_prime), 0) for t_prime in range(window_start, window_end + 1)
                )

        # Constraint 5: Concurrency capacity (with multipliers)
        # Each request consumes multiplier[i] slots when active
        for t in range(T):
            prob += (
                pulp.lpSum(z.get((i, t), 0) * multipliers[i] for i in range(n) if (i, t) in z)
                <= self.concurrency_limit
            )

        # Solve
        logger.info(f"Solving ILP with {n} requests, {T} time slots...")
        solver = pulp.PULP_CBC_CMD(msg=0, timeLimit=60)  # 60 second timeout
        prob.solve(solver)

        if prob.status != pulp.LpStatusOptimal:
            logger.warning(
                f"ILP solver did not find optimal solution (status={pulp.LpStatus[prob.status]}), using greedy fallback"
            )
            self._greedy_fallback(day_requests, api_costs)
            return

        # Extract solution
        for i, req in enumerate(day_requests):
            if pulp.value(x_Q[i]) > 0.5:
                self.assignments[req.id] = "daily"
            elif pulp.value(x_C[i]) > 0.5:
                self.assignments[req.id] = "concurrency"
                # Find start time
                for t in start_windows[i]:
                    if (i, t) in tau and pulp.value(tau[i, t]) > 0.5:
                        start_time = int(min_time + t * self.delta)
                        finish_time = int(start_time + slots_needed[i] * self.delta)
                        self.schedules[req.id] = (start_time, finish_time)
                        break
            else:
                self.assignments[req.id] = "api"

        logger.info(f"Day solved: cost={pulp.value(prob.objective):.2f}")

    def _greedy_fallback(self, day_requests: list[Request], api_costs: np.ndarray) -> None:
        """Greedy fallback when ILP fails or times out.

        Args:
            day_requests: Requests for this day
            api_costs: Precomputed API costs
        """
        n = len(day_requests)

        # Check model compatibility
        sq_compatible = [
            (not self.sq_supported_models) or (day_requests[i].model in self.sq_supported_models)
            for i in range(n)
        ]
        sc_compatible = [
            (not self.sc_supported_models) or (day_requests[i].model in self.sc_supported_models)
            for i in range(n)
        ]

        # Stage 1: Allocate to daily quota (top Q by API cost, only compatible models)
        compatible_indices = [i for i in range(n) if sq_compatible[i]]
        compatible_costs = api_costs[compatible_indices] if compatible_indices else np.array([])

        if len(compatible_indices) <= self.daily_quota:
            top_q_indices = set(compatible_indices)
        else:
            top_k = min(self.daily_quota, len(compatible_indices))
            top_indices_in_compatible = np.argpartition(compatible_costs, -top_k)[-top_k:]
            top_q_indices = {compatible_indices[i] for i in top_indices_in_compatible}

        for idx in top_q_indices:
            self.assignments[day_requests[idx].id] = "daily"

        # Stage 2: Allocate remaining to concurrency (event-driven scheduling)
        remaining_indices = [i for i in range(n) if i not in top_q_indices]
        remaining = [(day_requests[i], i) for i in remaining_indices]
        remaining.sort(key=lambda x: x[0].timestamp)

        # Simple event-driven scheduling with multipliers
        active_slots = []  # (finish_time, slots_used)
        for req, idx in remaining:
            # Remove finished requests
            active_slots = [(ft, slots) for ft, slots in active_slots if ft > req.timestamp]
            current_usage = sum(slots for _, slots in active_slots)

            # Check if this model can use S_C
            if not sc_compatible[idx]:
                self.assignments[req.id] = "api"
                continue

            multiplier = self.sc_multipliers.get(req.model, 1)

            if current_usage + multiplier <= self.concurrency_limit:
                # Can use concurrency
                self.assignments[req.id] = "concurrency"
                finish_time = req.timestamp + int(req.latency_seconds)
                active_slots.append((finish_time, multiplier))
            else:
                # Overflow to API
                self.assignments[req.id] = "api"

    def _calculate_api_costs(self, requests: list[Request]) -> np.ndarray:
        """Calculate API costs for all requests.

        Supports multi-model pricing when model_pricing is configured.

        Args:
            requests: List of requests

        Returns:
            Array of API costs
        """
        n = len(requests)
        request_tokens = np.array([r.request_tokens for r in requests])
        response_tokens = np.array([r.response_tokens for r in requests])
        models = [r.model for r in requests]

        # Check if multi-model pricing is available
        model_pricing = self.config.get("model_pricing", {})

        if model_pricing and any(models):
            # Multi-model pricing: calculate cost per model
            costs = np.zeros(n)
            for i in range(n):
                model = models[i]
                if not model:
                    raise ValueError(f"Request at index {i} has no model specified")

                pricing = model_pricing.get(model)
                if pricing is None:
                    raise ValueError(
                        f"Model '{model}' not found in model_pricing. "
                        f"Please add pricing in config/experiment.yaml. "
                        f"Available: {list(model_pricing.keys())}"
                    )

                # Pricing is per 1M tokens
                input_price = pricing.get("input")
                output_price = pricing.get("output")
                if input_price is None or output_price is None:
                    raise ValueError(f"Model '{model}' has incomplete pricing")

                costs[i] = (
                    request_tokens[i] / 1_000_000.0 * input_price
                    + response_tokens[i] / 1_000_000.0 * output_price
                )
            return costs
        else:
            # Single-model pricing: use default API provider
            api_provider_id = self.config["simulation"].get(
                "default_api_fallback", "openai-chatgpt"
            )
            provider = self.config["providers"][api_provider_id]

            input_price = provider.input_price_per_1k
            output_price = provider.output_price_per_1k

            return request_tokens / 1000.0 * input_price + response_tokens / 1000.0 * output_price

    def route(self, request: Request) -> RoutingDecision:
        """Route request using precomputed assignment.

        Args:
            request: Request to route

        Returns:
            Routing decision
        """
        # Reset quota if entering new day
        self.quota_manager.reset_if_new_day(request.day)

        # Use precomputed assignment (keyed by request.id for uniqueness)
        provider_type = self.assignments.get(request.id, "api")

        if provider_type == "daily":
            # Use daily quota subscription
            self.quota_manager.use_quota()
            self.subscription_used += 1

            return RoutingDecision(
                request=request,
                provider="daily-quota",
                cost=0.0,
                quota_used=1,
                timestamp=request.timestamp,
            )
        elif provider_type == "concurrency":
            # Use concurrency subscription
            self.subscription_used += 1
            schedule = self.schedules.get(request.id)
            if schedule:
                start_time, finish_time = schedule
            else:
                start_time = request.timestamp
                finish_time = int(start_time + request.latency_seconds)

            return RoutingDecision(
                request=request,
                provider="concurrency",
                cost=0.0,
                quota_used=0,
                timestamp=request.timestamp,
                start_time=start_time,
                finish_time=finish_time,
            )
        else:
            # Use API with model-specific pricing
            model_pricing = self.config.get("model_pricing", {})
            if model_pricing and request.model:
                cost = self.cost_calculator.calculate_cost_by_model(request)
                provider = "api"
            else:
                provider, cost = self.cost_calculator.get_cheapest_api_provider(request)
            self.api_used += 1

            return RoutingDecision(
                request=request,
                provider=provider,
                cost=cost,
                quota_used=0,
                timestamp=request.timestamp,
            )
