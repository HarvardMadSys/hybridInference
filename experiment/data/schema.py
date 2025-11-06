"""Data models for offline routing experiment."""

from dataclasses import dataclass
from enum import Enum


class ProviderType(Enum):
    """Provider types."""

    SUBSCRIPTION = "subscription"
    API = "api"


@dataclass
class Request:
    """Single request from historical data.

    Supports two data sources:
    1. BurstGPT dataset: only has model, tokens (no provider/cost info)
    2. HybridInference logs: has provider, actual_cost (real routing data)

    Attributes:
        timestamp: Seconds from epoch
        request_tokens: Input tokens
        response_tokens: Output tokens
        total_tokens: Total tokens (should equal request + response)
        model: Optional model name (e.g., "ChatGPT", "GPT-4")
        provider: Optional actual provider used (e.g., "chutes-subscription")
        actual_cost: Optional actual cost incurred (for validation)
    """

    timestamp: int
    request_tokens: int
    response_tokens: int
    total_tokens: int

    # Optional fields for different data sources
    model: str | None = None
    provider: str | None = None
    actual_cost: float | None = None

    @property
    def day(self) -> int:
        """Calculate which day this request belongs to.

        Assumes timestamp is seconds from epoch, with 86400 seconds per day.
        """
        return self.timestamp // 86400

    @property
    def time_of_day(self) -> int:
        """Time within the day (0-86399 seconds)."""
        return self.timestamp % 86400


@dataclass
class ProviderConfig:
    """Provider configuration.

    Attributes:
        name: Provider display name
        type: Provider type (subscription or API)
        monthly_fee: Monthly fee for subscription (0 for API)
        daily_quota: Daily quota for subscription (0 for API)
        input_price_per_1k: Input token price per 1K tokens (0 for subscription)
        output_price_per_1k: Output token price per 1K tokens (0 for subscription)
    """

    name: str
    type: ProviderType
    monthly_fee: float = 0.0
    daily_quota: int = 0
    input_price_per_1k: float = 0.0
    output_price_per_1k: float = 0.0

    def is_subscription(self) -> bool:
        """Check if this is a subscription provider."""
        return self.type == ProviderType.SUBSCRIPTION

    def is_api(self) -> bool:
        """Check if this is an API provider."""
        return self.type == ProviderType.API


@dataclass
class RoutingDecision:
    """Routing decision made by a strategy.

    Attributes:
        request: The request being routed
        provider: Provider name chosen
        cost: Marginal cost (0 for subscription within quota)
        quota_used: 1 for subscription, 0 for API
        timestamp: When the decision was made
    """

    request: Request
    provider: str
    cost: float
    quota_used: int
    timestamp: int

    @property
    def is_subscription(self) -> bool:
        """Check if this decision uses subscription."""
        return self.quota_used > 0
