"""Optimal routing strategy using NumPy acceleration."""

import logging

import numpy as np

from experiment.data.schema import Request, RoutingDecision
from experiment.strategies.base import RoutingStrategy

logger = logging.getLogger(__name__)


class OptimalStrategy(RoutingStrategy):
    """Offline optimal strategy using NumPy vectorization.

    This strategy has perfect future knowledge and computes the globally
    optimal routing that minimizes total cost. It serves as an upper bound
    for comparing online strategies.

    The algorithm:
    1. For each day, calculate the API cost for each request
    2. Select the top K requests with highest API cost to use subscription
    3. Route remaining requests to the cheapest API

    This is essentially solving a daily knapsack problem where:
    - Items = requests
    - Capacity = daily quota
    - Value = API cost savings

    Attributes:
        assignments: Precomputed routing assignments (timestamp -> provider)
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.assignments: dict[int, str] = {}

    def name(self) -> str:
        """Return strategy name.

        Returns:
            Strategy name
        """
        return "Optimal"

    def precompute(self, requests: list[Request]) -> None:
        """Precompute optimal routing using NumPy vectorization.

        This is the core of the Optimal strategy: we can see all future
        requests, so we can make globally optimal decisions.

        Args:
            requests: All requests in the dataset
        """
        if not requests:
            return

        logger.info(f"Precomputing optimal routing for {len(requests)} requests")

        # Extract data to NumPy arrays for vectorized operations
        timestamps = np.array([r.timestamp for r in requests])
        days = np.array([r.day for r in requests])
        request_tokens = np.array([r.request_tokens for r in requests])
        response_tokens = np.array([r.response_tokens for r in requests])

        # Vectorized cost calculation
        costs = self._calculate_costs_vectorized(request_tokens, response_tokens)

        # Process each day independently
        unique_days = np.unique(days)
        logger.info(f"Processing {len(unique_days)} days")

        for day in unique_days:
            # Use boolean indexing to filter requests for this day
            day_mask = days == day
            day_costs = costs[day_mask]
            day_timestamps = timestamps[day_mask]

            # Key optimization: use argpartition instead of full sort
            # argpartition is O(n), sort is O(n log n)
            quota = self.quota_manager.total_daily_quota

            if len(day_costs) <= quota:
                # All requests can use subscription
                top_k_indices = np.arange(len(day_costs))
            else:
                # Find top K requests with highest cost
                # argpartition partitions array into two parts:
                # - First K elements are the largest (but not sorted)
                # - Remaining elements are smaller
                top_k_indices = np.argpartition(day_costs, -quota)[-quota:]

            # Assign subscription to top K requests
            for idx in top_k_indices:
                timestamp = day_timestamps[idx]
                self.assignments[int(timestamp)] = "subscription"

            # Assign API to remaining requests
            all_indices = set(range(len(day_costs)))
            api_indices = all_indices - set(top_k_indices)
            for idx in api_indices:
                timestamp = day_timestamps[idx]
                self.assignments[int(timestamp)] = "api"

        logger.info(
            f"Precomputation complete: "
            f"{sum(1 for v in self.assignments.values() if v == 'subscription')} "
            f"subscription, "
            f"{sum(1 for v in self.assignments.values() if v == 'api')} API"
        )

    def _calculate_costs_vectorized(
        self, request_tokens: np.ndarray, response_tokens: np.ndarray
    ) -> np.ndarray:
        """Vectorized cost calculation using NumPy.

        Calculates API cost for all requests in one operation.

        Args:
            request_tokens: Array of input token counts
            response_tokens: Array of output token counts

        Returns:
            Array of API costs
        """
        # Get default API provider pricing
        api_provider_id = self.config["simulation"].get("default_api_fallback", "openai-chatgpt")
        provider = self.config["providers"][api_provider_id]

        input_price = provider.input_price_per_1k
        output_price = provider.output_price_per_1k

        # Vectorized calculation (all requests at once)
        input_costs = request_tokens / 1000.0 * input_price
        output_costs = response_tokens / 1000.0 * output_price

        return input_costs + output_costs

    def route(self, request: Request) -> RoutingDecision:
        """Route request using precomputed assignment.

        Args:
            request: Request to route

        Returns:
            Routing decision
        """
        # Reset quota if entering new day
        self.quota_manager.reset_if_new_day(request.day)

        # Use precomputed assignment
        provider_type = self.assignments.get(request.timestamp, "api")

        if provider_type == "subscription":
            # Use subscription
            self.quota_manager.use_quota()
            self.subscription_used += 1

            return RoutingDecision(
                request=request,
                provider="subscription",
                cost=0.0,  # Marginal cost is 0
                quota_used=1,
                timestamp=request.timestamp,
            )
        else:
            # Use cheapest API
            provider, cost = self.cost_calculator.get_cheapest_api_provider(request)
            self.api_used += 1

            return RoutingDecision(
                request=request,
                provider=provider,
                cost=cost,
                quota_used=0,
                timestamp=request.timestamp,
            )
