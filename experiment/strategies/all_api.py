"""All-API baseline strategy.

This strategy represents the scenario without any subscription service.
All requests are routed to the API provider (e.g., OpenAI).
Used as the primary baseline to measure cost savings from using subscriptions.
"""

from experiment.data.schema import Request, RoutingDecision
from experiment.strategies.base import RoutingStrategy


class AllAPIStrategy(RoutingStrategy):
    """Baseline: Route all requests to API provider.

    This represents the scenario without Chutes subscription.
    All requests use the pay-per-token API (e.g., OpenAI).

    For single model scenario:
    - No subscription cost ($0)
    - All requests pay API cost based on tokens

    This is the primary baseline to measure:
    - Cost savings from using subscription
    - ROI of subscription service
    """

    @property
    def name(self) -> str:
        """Return the name of this strategy."""
        return "All-API"

    def route(self, request: Request) -> RoutingDecision:
        """Route to API provider (never use subscription).

        Args:
            request: Incoming request

        Returns:
            Routing decision (always API, never subscription)
        """
        # For single model, there's only one API provider
        # Get the default API provider from config
        api_provider = self.config.get("simulation", {}).get("default_api_fallback")

        if not api_provider:
            # Fallback: find first API provider
            api_providers = [
                name
                for name, provider in self.cost_calculator.providers.items()
                if provider.is_api()
            ]
            if not api_providers:
                raise ValueError("No API provider configured")
            api_provider = api_providers[0]

        # Calculate API cost
        cost = self.cost_calculator.calculate_api_cost(request, api_provider)

        # Update statistics
        self.api_used += 1

        return RoutingDecision(
            request=request,
            provider=api_provider,
            cost=cost,
            quota_used=0,  # Never use subscription quota
            timestamp=request.timestamp,
        )
