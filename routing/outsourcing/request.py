"""Request abstraction for outsourcing decisions."""

from dataclasses import dataclass, field
from enum import Enum


class RequestStatus(Enum):
    """Status of a request in the serving pipeline."""

    WAITING = "waiting"  # In queue, not yet scheduled
    RUNNING = "running"  # Currently being processed
    COMPLETED = "completed"  # Finished successfully
    OUTSOURCED = "outsourced"  # Sent to external service


@dataclass
class OutsourcingRequestInfo:
    """Request information needed for outsourcing decisions.

    This is a lightweight view that can be constructed from any serving engine's
    request object.
    """

    # Identity
    request_id: str

    # Timing information
    arrival_time: float  # When request entered the system
    queue_time: float = 0.0  # Time spent in queue so far

    # Token information
    num_prompt_tokens: int = 0  # Input/prefill tokens
    num_output_tokens: int = 0  # Expected output tokens (may be estimate)
    num_processed_tokens: int = 0  # Tokens already processed
    num_cached_tokens: int = 0  # Tokens already in KV cache (prefix cache hit)

    # SLO information (optional)
    prefill_slo_seconds: float | None = None  # TTFT deadline
    total_slo_seconds: float | None = None  # End-to-end deadline

    # Status
    status: RequestStatus = RequestStatus.WAITING
    is_prefill_complete: bool = False

    # Pricing/value (for knapsack)
    input_price_per_token: float = 1.25 / 1_000_000
    output_price_per_token: float = 10.0 / 1_000_000

    # Metadata
    metadata: dict = field(default_factory=dict)

    @property
    def estimated_value(self) -> float:
        """Revenue potential of this request."""
        return (
            self.num_prompt_tokens * self.input_price_per_token
            + self.num_output_tokens * self.output_price_per_token
        )

    @property
    def remaining_prompt_tokens(self) -> int:
        """Tokens left to process in prefill phase.

        This accounts for:
        - Tokens already processed (num_processed_tokens)
        - Tokens already in KV cache from prefix cache hit (num_cached_tokens)
        """
        if self.is_prefill_complete:
            return 0
        # Subtract both processed tokens and cached tokens
        return max(0, self.num_prompt_tokens - self.num_processed_tokens - self.num_cached_tokens)

    @property
    def remaining_output_tokens(self) -> int:
        """Tokens left to generate in decode phase."""
        if not self.is_prefill_complete:
            return self.num_output_tokens  # Haven't started decode yet
        decode_done = max(0, self.num_processed_tokens - self.num_prompt_tokens)
        return max(0, self.num_output_tokens - decode_done)

    @property
    def prefill_deadline(self) -> float | None:
        """Absolute timestamp when prefill must complete."""
        if self.prefill_slo_seconds is None:
            return None
        return self.arrival_time + self.prefill_slo_seconds

    @property
    def total_deadline(self) -> float | None:
        """Absolute timestamp when entire request must complete."""
        if self.total_slo_seconds is None:
            return None
        return self.arrival_time + self.total_slo_seconds
