"""Cost calculator for different providers."""

import logging

from experiment.data.schema import ProviderConfig, Request

logger = logging.getLogger(__name__)


class CostCalculator:
    """Calculate request costs for different providers.

    Attributes:
        providers: Dictionary mapping provider ID to ProviderConfig
    """

    def __init__(self, providers: dict[str, ProviderConfig]):
        """Initialize cost calculator.

        Args:
            providers: Dictionary of provider configurations
        """
        self.providers = providers

    def calculate_api_cost(self, request: Request, provider_name: str) -> float:
        """Calculate API cost for a request.

        Args:
            request: The request to price
            provider_name: Name of API provider

        Returns:
            Cost in dollars

        Raises:
            ValueError: If provider is not an API provider
        """
        if provider_name not in self.providers:
            raise ValueError(f"Provider {provider_name} not found")

        provider = self.providers[provider_name]

        if not provider.is_api():
            raise ValueError(f"{provider_name} is not an API provider")

        # Calculate cost: (tokens / 1000) * price_per_1k
        input_cost = request.request_tokens / 1000.0 * provider.input_price_per_1k
        output_cost = request.response_tokens / 1000.0 * provider.output_price_per_1k

        return input_cost + output_cost

    def calculate_subscription_cost(
        self, provider_name: str, num_subscriptions: int = 1, days: int = 30
    ) -> float:
        """Calculate total subscription cost.

        Args:
            provider_name: Name of subscription provider
            num_subscriptions: Number of subscription accounts
            days: Number of days (for prorating)

        Returns:
            Total subscription cost

        Raises:
            ValueError: If provider is not a subscription provider
        """
        if provider_name not in self.providers:
            raise ValueError(f"Provider {provider_name} not found")

        provider = self.providers[provider_name]

        if not provider.is_subscription():
            raise ValueError(f"{provider_name} is not a subscription provider")

        # Prorate if less than a month
        monthly_cost = num_subscriptions * provider.monthly_fee
        return monthly_cost * (days / 30.0)

    def get_cheapest_api_provider(self, request: Request) -> tuple[str, float]:
        """Find cheapest API provider for a request.

        Args:
            request: The request to price

        Returns:
            Tuple of (provider_name, cost)

        Raises:
            ValueError: If no API providers are configured
        """
        api_providers = [
            (name, provider) for name, provider in self.providers.items() if provider.is_api()
        ]

        if not api_providers:
            raise ValueError("No API providers configured")

        costs = [(name, self.calculate_api_cost(request, name)) for name, _ in api_providers]

        return min(costs, key=lambda x: x[1])
