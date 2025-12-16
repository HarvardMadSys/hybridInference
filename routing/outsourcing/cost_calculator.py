"""API cost calculation for outsourcing decisions.

Ported from vidur's outsourcing module for cost-based prioritization.
"""


class APICostCalculator:
    """Calculate API costs for outsourcing requests."""

    def __init__(
        self,
        input_price_per_million: float = 1.25,
        output_price_per_million: float = 10.00,
    ):
        """
        Initialize the cost calculator.

        Args:
            input_price_per_million: Price per million input tokens (USD)
            output_price_per_million: Price per million output tokens (USD)
        """
        self._input_price_per_million = input_price_per_million
        self._output_price_per_million = output_price_per_million

    def calculate_cost(self, input_tokens: int, output_tokens: int) -> float:
        """
        Calculate API cost based on token counts.

        Args:
            input_tokens: Number of input tokens
            output_tokens: Number of output tokens

        Returns:
            Total API cost in USD
        """
        input_cost = (input_tokens / 1_000_000) * self._input_price_per_million
        output_cost = (output_tokens / 1_000_000) * self._output_price_per_million
        return input_cost + output_cost
