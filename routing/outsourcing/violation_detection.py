"""TTFT (Time-to-First-Token) violation detection for outsourcing decisions.

Ported from vidur's outsourcing module with integration for SGLang metrics.
"""

from collections.abc import Callable
from dataclasses import dataclass, field

from routing.outsourcing.flop_calculator import FLOPCalculatorInterface
from routing.outsourcing.request import OutsourcingRequestInfo
from serving.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class ViolationCheckResult:
    """Structured result from TTFT violation detection.

    Implements ``__bool__`` so existing ``if check_violations(...)`` code
    continues to work without changes.
    """

    has_violation: bool
    trigger: str = "none"  # "none" | "flop_model" | "observed_ttft" | "queue_pressure"
    per_request_estimates: list[dict] = field(default_factory=list)
    sglang_pending_count: int = 0
    observed_ttft: float | None = None

    def __bool__(self) -> bool:
        return self.has_violation


class TTFTViolationDetector:
    """Detect imminent TTFT SLO violations."""

    def __init__(
        self,
        flop_calculator: FLOPCalculatorInterface,
        mode: str = "all",
        prefill_throughput: float = 1000.0,
        max_micro_batch_size: int = 256,
        utilization_target: float = 0.8,
        avg_prompt_tokens_estimate: int = 512,
        default_slo_seconds: float | None = None,
    ):
        """Initialize the violation detector.

        Args:
            flop_calculator: FLOP calculator for throughput estimation
            mode: Detection mode ("all" or "head")
            prefill_throughput: Estimated prefill throughput (tokens/sec).
                               Can be updated later via set_prefill_throughput().
            max_micro_batch_size: Maximum micro-batch size
            utilization_target: Target utilization for effective FLOPS calculation
            avg_prompt_tokens_estimate: Average prompt token count used to estimate
                phantom ahead FLOPs from SGLang pending requests.
            default_slo_seconds: Fallback SLO for the observed_ttft fast-path when
                a request has no explicit prefill_slo_seconds.  Typically set to
                the engine's prefill_slo_base_seconds.
        """
        self._flop_calculator = flop_calculator
        self._mode = mode
        self._prefill_throughput = prefill_throughput
        self._max_micro_batch_size = max_micro_batch_size
        self._utilization_target = utilization_target
        self._avg_prompt_tokens_estimate = avg_prompt_tokens_estimate
        self._default_slo_seconds = default_slo_seconds
        self._detector_func = self._get_detector_function(mode)

    def set_prefill_throughput(self, throughput: float) -> None:
        """Update the prefill throughput estimate.

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
                f"Unknown TTFT violation mode: {mode}. " f"Choose from: {list(detectors.keys())}"
            )
        return detectors[mode]

    def check_violations(
        self,
        waiting_requests: list[OutsourcingRequestInfo],
        current_time: float,
        sglang_pending_count: int = 0,
        observed_ttft: float | None = None,
    ) -> ViolationCheckResult:
        """Check if there are any TTFT violations.

        Args:
            waiting_requests: List of waiting requests
            current_time: Current simulation time
            sglang_pending_count: Number of requests pending in SGLang's actual
                queue (from Prometheus metrics).  Used to inject phantom ahead
                FLOPs so the first shadow-queue request sees realistic load.
            observed_ttft: Latest observed average TTFT from SGLang metrics
                (seconds).  If this already exceeds 80% of any request's SLO,
                we short-circuit and return True immediately.

        Returns:
            ViolationCheckResult with violation status, trigger, and per-request estimates.
            Supports ``bool()`` for backward compatibility.
        """
        # Fast-path: if the observed TTFT already nears any request's SLO,
        # skip the full FLOP computation and flag a violation immediately.
        if observed_ttft is not None and waiting_requests:
            for r in waiting_requests:
                slo = r.prefill_slo_seconds or self._default_slo_seconds
                if slo is not None and observed_ttft > slo * 0.8:
                    logger.debug(
                        "Fast-path violation: observed_ttft=%.3fs > 80%% of SLO %.3fs for %s",
                        observed_ttft,
                        slo,
                        r.request_id,
                    )
                    return ViolationCheckResult(
                        has_violation=True,
                        trigger="observed_ttft",
                        per_request_estimates=[],
                        sglang_pending_count=sglang_pending_count,
                        observed_ttft=observed_ttft,
                    )

        return self._detector_func(waiting_requests, current_time, sglang_pending_count)

    def _check_all_violations(
        self,
        waiting_requests: list[OutsourcingRequestInfo],
        current_time: float,
        sglang_pending_count: int = 0,
    ) -> ViolationCheckResult:
        """Check EVERY waiting request for imminent TTFT violation under FCFS.

        Returns ViolationCheckResult with per-request TTFT estimates.
        """
        _no_violation = ViolationCheckResult(
            has_violation=False,
            trigger="none",
            sglang_pending_count=sglang_pending_count,
        )
        if not waiting_requests:
            return _no_violation

        # Get effective FLOPS per second
        effective_flops = self._flop_calculator.get_effective_flops_per_second(
            self._utilization_target
        )
        if effective_flops <= 0:
            is_overloaded = len(waiting_requests) > self._max_micro_batch_size
            return ViolationCheckResult(
                has_violation=is_overloaded,
                trigger="queue_pressure" if is_overloaded else "none",
                sglang_pending_count=sglang_pending_count,
            )

        # Compute remaining prefill FLOPs for each request
        rem_flops = []
        for r in waiting_requests:
            rem_tokens = r.remaining_prompt_tokens
            flops = self._flop_calculator.compute_prefill_flops(r, rem_tokens)
            rem_flops.append(flops)

        # Phantom ahead FLOPs from SGLang's actual pending queue.
        sglang_ahead_flops = 0.0
        if sglang_pending_count > 0:
            _phantom = OutsourcingRequestInfo(
                request_id="_phantom",
                arrival_time=0.0,
                num_prompt_tokens=self._avg_prompt_tokens_estimate,
            )
            per_req_flops = self._flop_calculator.compute_prefill_flops(
                _phantom, self._avg_prompt_tokens_estimate
            )
            sglang_ahead_flops = sglang_pending_count * per_req_flops

        # Prefix sum: FLOP work ahead of each request in FCFS order
        ahead = [0.0] * len(waiting_requests)
        acc = sglang_ahead_flops
        for i in range(len(waiting_requests)):
            ahead[i] = acc
            acc += rem_flops[i]

        # Evaluate every request and collect per-request estimates
        at_risk: set[str] = set()
        saw_any_slo = False
        per_request_estimates: list[dict] = []

        for i, r in enumerate(waiting_requests):
            est_ttft = (ahead[i] + rem_flops[i]) / effective_flops
            time_left = r.prefill_deadline - current_time if r.prefill_deadline else float("inf")
            is_at_risk = False

            if r.prefill_slo_seconds is not None:
                saw_any_slo = True
                if est_ttft > time_left:
                    at_risk.add(r.request_id)
                    is_at_risk = True
                    logger.debug(
                        f"Request {r.request_id}: est_ttft={est_ttft:.2f}s > "
                        f"time_left={time_left:.2f}s (VIOLATION)"
                    )

            per_request_estimates.append(
                {
                    "request_id": r.request_id,
                    "est_ttft": est_ttft,
                    "time_left": time_left,
                    "at_risk": is_at_risk,
                }
            )

        # If none had an explicit SLO, fall back to a simple pressure heuristic.
        # Also update per_request_estimates to keep at_risk consistent with has_violation.
        if not saw_any_slo and len(waiting_requests) > self._max_micro_batch_size:
            pressure_ids = {r.request_id for r in waiting_requests[: self._max_micro_batch_size]}
            at_risk.update(pressure_ids)
            for est in per_request_estimates:
                if est["request_id"] in pressure_ids:
                    est["at_risk"] = True

        has_violation = len(at_risk) > 0
        trigger = "none"
        if has_violation:
            trigger = "queue_pressure" if not saw_any_slo else "flop_model"

        return ViolationCheckResult(
            has_violation=has_violation,
            trigger=trigger,
            per_request_estimates=per_request_estimates,
            sglang_pending_count=sglang_pending_count,
        )

    def _check_head_violation(
        self,
        waiting_requests: list[OutsourcingRequestInfo],
        current_time: float,
        sglang_pending_count: int = 0,
    ) -> ViolationCheckResult:
        """Check if the head request has imminent TTFT violation.

        Returns ViolationCheckResult with head request estimate.
        """
        _no_violation = ViolationCheckResult(
            has_violation=False,
            trigger="none",
            sglang_pending_count=sglang_pending_count,
        )
        if not waiting_requests:
            return _no_violation

        head = waiting_requests[0]
        if head.prefill_slo_seconds is None:
            is_overloaded = len(waiting_requests) > self._max_micro_batch_size
            return ViolationCheckResult(
                has_violation=is_overloaded,
                trigger="queue_pressure" if is_overloaded else "none",
                sglang_pending_count=sglang_pending_count,
            )

        est_ttft = self._estimate_fcfs_ttft(head, waiting_requests, sglang_pending_count)
        time_left = head.prefill_deadline - current_time if head.prefill_deadline else float("inf")
        has_violation = est_ttft > time_left

        return ViolationCheckResult(
            has_violation=has_violation,
            trigger="flop_model" if has_violation else "none",
            per_request_estimates=[
                {
                    "request_id": head.request_id,
                    "est_ttft": est_ttft,
                    "time_left": time_left,
                    "at_risk": has_violation,
                }
            ],
            sglang_pending_count=sglang_pending_count,
        )

    def _estimate_fcfs_ttft(
        self,
        req: OutsourcingRequestInfo,
        waiting_requests: list[OutsourcingRequestInfo],
        sglang_pending_count: int = 0,
    ) -> float:
        """Estimate Time-to-First-Token under FCFS assumption.

        Returns: queueing delay + own prefill time (in seconds).
        """
        effective_flops = self._flop_calculator.get_effective_flops_per_second(
            self._utilization_target
        )
        if effective_flops <= 0:
            return float("inf")

        # Phantom ahead FLOPs from SGLang's actual pending queue
        ahead_flops = 0.0
        if sglang_pending_count > 0:
            _phantom = OutsourcingRequestInfo(
                request_id="_phantom",
                arrival_time=0.0,
                num_prompt_tokens=self._avg_prompt_tokens_estimate,
            )
            per_req_flops = self._flop_calculator.compute_prefill_flops(
                _phantom, self._avg_prompt_tokens_estimate
            )
            ahead_flops = sglang_pending_count * per_req_flops

        # Sum remaining prefill FLOPs of all waiting requests ahead of this one
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
