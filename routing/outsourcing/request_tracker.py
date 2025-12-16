"""Request tracking and metrics collection for outsourcing.

Ported from vidur's outsourcing module.
"""

from typing import Callable

from routing.outsourcing.request import OutsourcingRequestInfo


class RequestTracker:
    """Track outsourced requests and collect metrics."""

    def __init__(self, model_id: str, cost_calculator: Callable):
        """
        Initialize the request tracker.

        Args:
            model_id: ID of the model/replica
            cost_calculator: Function to calculate API cost (input_tokens, output_tokens) -> cost
        """
        self._model_id = model_id
        self._cost_calculator = cost_calculator
        self._outsourced_request_details: list[dict] = []

    def track_outsourced_request(
        self,
        request: OutsourcingRequestInfo,
        current_time: float,
    ) -> None:
        """
        Track details of an outsourced request for later reporting.

        Args:
            request: The outsourced request
            current_time: Current time
        """
        input_tokens = request.num_prompt_tokens
        output_tokens = request.num_output_tokens
        api_cost = self._cost_calculator(input_tokens, output_tokens)

        self._outsourced_request_details.append(
            {
                "request_id": request.request_id,
                "outsourced_at": current_time,
                "arrived_at": request.arrival_time,
                "queue_time": request.queue_time,
                "num_prompt_tokens": input_tokens,
                "num_output_tokens": output_tokens,
                "num_processed_tokens": request.num_processed_tokens,
                "num_cached_tokens": request.num_cached_tokens,
                "api_cost_usd": api_cost,
                "model_id": str(self._model_id),
            }
        )

    def get_outsourced_request_details(self) -> list[dict]:
        """Return the list of outsourced request details."""
        return self._outsourced_request_details

    def get_outsourcing_statistics(self) -> dict:
        """Calculate and return outsourcing statistics."""
        if not self._outsourced_request_details:
            return {
                "total_outsourced": 0,
                "total_api_cost_usd": 0.0,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "model_id": str(self._model_id),
            }

        total = len(self._outsourced_request_details)
        total_cost = sum(
            d["api_cost_usd"] for d in self._outsourced_request_details
        )
        total_input = sum(
            d["num_prompt_tokens"] for d in self._outsourced_request_details
        )
        total_output = sum(
            d["num_output_tokens"] for d in self._outsourced_request_details
        )

        return {
            "total_outsourced": total,
            "total_api_cost_usd": total_cost,
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
            "model_id": str(self._model_id),
        }
