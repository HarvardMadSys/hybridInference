"""Outsourcing decision engine and decision dataclass."""

from dataclasses import dataclass, field

from .flop_calculator import FLOPCalculatorInterface
from .queue import WaitingQueueInterface
from .request import OutsourcingRequestInfo


@dataclass
class OutsourcingDecision:
    """Result of an outsourcing decision cycle."""

    should_outsource: bool
    requests_to_outsource: list[str]  # Request IDs
    requests_to_keep: list[str]  # Request IDs
    reason: str  # Human-readable explanation
    metrics: dict = field(default_factory=dict)


class OutsourcingEngine:
    """Main engine that makes outsourcing decisions.

    Uses the abstractions above to remain engine-agnostic.
    """

    def __init__(
        self,
        waiting_queue: WaitingQueueInterface,
        flop_calculator: FLOPCalculatorInterface,
        max_batch_size: int = 256,
        prefill_slo_base_seconds: float = 4.05,
        prefill_slo_slack_factor: float = 10.0,
        utilization_target: float = 0.8,
    ):
        self.waiting_queue = waiting_queue
        self.flop_calculator = flop_calculator
        self.max_batch_size = max_batch_size
        self.prefill_slo_base_seconds = prefill_slo_base_seconds
        self.prefill_slo_slack_factor = prefill_slo_slack_factor
        self.utilization_target = utilization_target

        self.outsourced_request_ids: set[str] = set()

    def should_outsource(self, current_time: float) -> OutsourcingDecision:
        """Main entry point: decide if outsourcing is needed.

        Call this before each scheduling cycle.
        """
        waiting_requests = self.waiting_queue.get_all_waiting()

        if not waiting_requests:
            return OutsourcingDecision(
                should_outsource=False,
                requests_to_outsource=[],
                requests_to_keep=[],
                reason="No waiting requests",
            )

        # Check for SLO violations
        at_risk_requests = self._check_slo_violations(waiting_requests, current_time)

        if not at_risk_requests:
            return OutsourcingDecision(
                should_outsource=False,
                requests_to_outsource=[],
                requests_to_keep=[r.request_id for r in waiting_requests],
                reason="No SLO violations detected",
            )

        # Run knapsack to decide what to keep vs outsource
        keep_ids, outsource_ids = self._knapsack_selection_greedy(waiting_requests, current_time)

        return OutsourcingDecision(
            should_outsource=len(outsource_ids) > 0,
            requests_to_outsource=outsource_ids,
            requests_to_keep=keep_ids,
            reason=f"SLO violations detected for {len(at_risk_requests)} requests",
            metrics={
                "at_risk_count": len(at_risk_requests),
                "total_waiting": len(waiting_requests),
                "outsource_count": len(outsource_ids),
            },
        )

    def apply_outsourcing(self, decision: OutsourcingDecision) -> list[OutsourcingRequestInfo]:
        """Execute the outsourcing decision by removing requests from queue.

        Returns the outsourced requests for handoff to external service.
        """
        if not decision.should_outsource:
            return []

        outsourced = self.waiting_queue.remove_requests(set(decision.requests_to_outsource))

        for req in outsourced:
            self.outsourced_request_ids.add(req.request_id)

        return outsourced

    def _check_slo_violations(
        self, waiting_requests: list[OutsourcingRequestInfo], current_time: float
    ) -> list[OutsourcingRequestInfo]:
        """Check which waiting requests face imminent TTFT violations.

        Uses FLOP-based estimation for accuracy.
        """
        effective_flops = self.flop_calculator.get_effective_flops_per_second(
            self.utilization_target
        )

        at_risk = []

        # Compute cumulative FLOP load for FCFS queue
        cumulative_flops = 0.0
        for req in waiting_requests:
            # FLOPs needed ahead of this request
            ahead_flops = cumulative_flops

            # FLOPs needed for this request's remaining prefill
            req_flops = self.flop_calculator.compute_prefill_flops(req, req.remaining_prompt_tokens)

            # Total time to TTFT
            total_flops = ahead_flops + req_flops
            estimated_ttft = total_flops / effective_flops if effective_flops > 0 else float("inf")

            # Determine SLO (explicit or derived)
            if req.prefill_slo_seconds is not None:
                slo = req.prefill_slo_seconds
            else:
                # Derive default SLO based on request size
                slo = self.prefill_slo_base_seconds + (
                    self.prefill_slo_slack_factor * req_flops / effective_flops
                )

            # Check violation
            time_left = (req.arrival_time + slo) - current_time
            if estimated_ttft > time_left:
                at_risk.append(req)

            # Update cumulative for next request
            cumulative_flops += req_flops

        return at_risk

    def _knapsack_selection_greedy(
        self, waiting_requests: list[OutsourcingRequestInfo], current_time: float
    ) -> tuple[list[str], list[str]]:
        """Greedy knapsack: keep highest value/FLOP ratio requests locally.

        Returns: (keep_request_ids, outsource_request_ids)
        """
        # Build knapsack items
        items = []
        for req in waiting_requests:
            # Weight = total FLOPs remaining
            weight = 0.0
            if req.remaining_prompt_tokens > 0:
                weight += self.flop_calculator.compute_prefill_flops(
                    req, req.remaining_prompt_tokens
                )
            if req.remaining_output_tokens > 0:
                # Decode FLOPs scale with KV cache size, approximate per-token
                weight += self.flop_calculator.compute_decode_flops(
                    req, req.remaining_output_tokens
                )

            # Value = revenue potential
            value = req.estimated_value

            items.append(
                {
                    "id": req.request_id,
                    "weight": max(1.0, weight),
                    "value": max(1e-10, value),
                    "ratio": value / max(1.0, weight),
                }
            )

        # Budget: ensure we can outsource at least the cheapest request
        min_weight = min(item["weight"] for item in items) if items else 0
        budget = max(0, sum(item["weight"] for item in items) - min_weight)

        # Greedy by value/weight ratio
        items.sort(key=lambda x: x["ratio"], reverse=True)

        keep = []
        total_weight = 0.0
        for item in items:
            if total_weight + item["weight"] <= budget:
                keep.append(item["id"])
                total_weight += item["weight"]

        keep_set = set(keep)
        outsource = [item["id"] for item in items if item["id"] not in keep_set]

        return keep, outsource
