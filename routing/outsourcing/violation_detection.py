"""TTFT (Time-to-First-Token) violation detection for outsourcing decisions.

Ported from vidur's outsourcing module with integration for SGLang metrics.
"""

from typing import Callable

from routing.outsourcing.flop_calculator import FLOPCalculatorInterface
from routing.outsourcing.request import OutsourcingRequestInfo
from serving.utils.logging import get_logger

logger = get_logger(__name__)


class TTFTViolationDetector:
    """Detect imminent TTFT SLO violations."""

    def __init__(
        self,
        flop_calculator: FLOPCalculatorInterface,
        mode: str = "all",
        prefill_throughput: float = 1000.0,
        max_micro_batch_size: int = 256,
        utilization_target: float = 0.8,
    ):
        """
        Initialize the violation detector.

        Args:
            flop_calculator: FLOP calculator for throughput estimation
            mode: Detection mode ("all" or "head")
            prefill_throughput: Estimated prefill throughput (tokens/sec).
                               Can be updated later via set_prefill_throughput().
            max_micro_batch_size: Maximum micro-batch size
            utilization_target: Target utilization for effective FLOPS calculation
        """
        self._flop_calculator = flop_calculator
        self._mode = mode
        self._prefill_throughput = prefill_throughput
        self._max_micro_batch_size = max_micro_batch_size
        self._utilization_target = utilization_target
        self._detector_func = self._get_detector_function(mode)

    def set_prefill_throughput(self, throughput: float) -> None:
        """
        Update the prefill throughput estimate.
        
        Args:
            throughput: New prefill throughput estimate (tokens/sec)
        """
        self._prefill_throughput = throughput

    def _get_detector_function(self, mode: str) -> Callable:
        """Get the appropriate detector function based on mode."""
        detectors = {
            "all": self._check_all_violations,
            "head": self._check_head_violation,
        }
        if mode not in detectors:
            raise ValueError(
                f"Unknown TTFT violation mode: {mode}. "
                f"Choose from: {list(detectors.keys())}"
            )
        return detectors[mode]

    def check_violations(
        self,
        waiting_requests: list[OutsourcingRequestInfo],
        current_time: float,
    ) -> bool:
        """
        Check if there are any TTFT violations.

        Args:
            waiting_requests: List of waiting requests
            current_time: Current simulation time

        Returns:
            True if violations detected, False otherwise
        """
        return self._detector_func(waiting_requests, current_time)

    def _check_all_violations(
        self,
        waiting_requests: list[OutsourcingRequestInfo],
        current_time: float,
    ) -> bool:
        """
        Check EVERY waiting request for imminent TTFT violation under FCFS.
        Returns True if any request is at risk of SLO violation.
        """
        if not waiting_requests:
            return False

        # Get effective FLOPS per second
        effective_flops = self._flop_calculator.get_effective_flops_per_second(
            self._utilization_target
        )
        if effective_flops <= 0:
            return len(waiting_requests) > self._max_micro_batch_size

        # Compute remaining prefill FLOPs for each request
        rem_flops = []
        for r in waiting_requests:
            rem_tokens = r.remaining_prompt_tokens
            flops = self._flop_calculator.compute_prefill_flops(r, rem_tokens)
            rem_flops.append(flops)

        # Prefix sum: FLOP work ahead of each request in FCFS order
        ahead = [0.0] * len(waiting_requests)
        acc = 0.0
        for i in range(len(waiting_requests)):
            ahead[i] = acc
            acc += rem_flops[i]

        # Evaluate every request with an SLO
        at_risk = set()
        saw_any_slo = False
        for i, r in enumerate(waiting_requests):
            if r.prefill_slo_seconds is None:
                continue
            saw_any_slo = True

            est_ttft = (ahead[i] + rem_flops[i]) / effective_flops
            
            time_left = r.prefill_deadline - current_time if r.prefill_deadline else float("inf")
            if est_ttft > time_left:
                at_risk.add(r.request_id)
                logger.debug(
                    f"Request {r.request_id}: est_ttft={est_ttft:.2f}s > "
                    f"time_left={time_left:.2f}s (VIOLATION)"
                )

        # If none had an explicit SLO, fall back to a simple pressure heuristic
        if not saw_any_slo:
            if len(waiting_requests) > self._max_micro_batch_size:
                at_risk.update(r.request_id for r in waiting_requests[: self._max_micro_batch_size])

        return len(at_risk) > 0

    def _check_head_violation(
        self,
        waiting_requests: list[OutsourcingRequestInfo],
        current_time: float,
    ) -> bool:
        """
        Check if the head request has imminent TTFT violation.
        Returns True if head request is at risk of SLO violation.
        """
        if not waiting_requests:
            return False

        head = waiting_requests[0]
        if head.prefill_slo_seconds is None:
            # Fallback to queue length heuristic
            return len(waiting_requests) > self._max_micro_batch_size

        est_ttft = self._estimate_fcfs_ttft(head, waiting_requests)
        
        time_left = head.prefill_deadline - current_time if head.prefill_deadline else float("inf")
        return est_ttft > time_left

    def _estimate_fcfs_ttft(
        self,
        req: OutsourcingRequestInfo,
        waiting_requests: list[OutsourcingRequestInfo],
    ) -> float:
        """
        Estimate Time-to-First-Token under FCFS assumption.
        Returns: queueing delay + own prefill time (in seconds).
        """
        effective_flops = self._flop_calculator.get_effective_flops_per_second(
            self._utilization_target
        )
        if effective_flops <= 0:
            return float("inf")

        # Sum remaining prefill FLOPs of all waiting requests ahead of this one
        ahead_flops = 0.0
        for r in waiting_requests:
            if r.request_id == req.request_id:
                break
            rem_tokens = r.remaining_prompt_tokens
            ahead_flops += self._flop_calculator.compute_prefill_flops(r, rem_tokens)

        # Own prefill work
        rem_self = req.remaining_prompt_tokens
        self_flops = self._flop_calculator.compute_prefill_flops(req, rem_self)

        # Convert to seconds
        est = (ahead_flops + self_flops) / effective_flops
        return est
