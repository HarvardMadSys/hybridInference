"""Outsourcing decision engine and decision dataclass.

Updated to use vidur-style outsourcing components:
- APICostCalculator for pricing
- CandidateSelector for request filtering
- KnapsackSolver for optimization
- TTFTViolationDetector for SLO checking
- RequestTracker for metrics
"""

from dataclasses import dataclass, field

from .candidate_selection import CandidateSelector
from .cost_calculator import APICostCalculator
from .flop_calculator import FLOPCalculatorInterface
from .knapsack import KnapsackSolver
from .queue import WaitingQueueInterface
from .request import OutsourcingRequestInfo
from .request_tracker import RequestTracker
from .violation_detection import TTFTViolationDetector


@dataclass
class OutsourcingDecision:
    """Result of an outsourcing decision cycle."""

    should_outsource: bool
    requests_to_outsource: list[str]  # Request IDs
    requests_to_keep: list[str]  # Request IDs
    reason: str  # Human-readable explanation
    metrics: dict = field(default_factory=dict)


class OutsourcingEngine:
    """Main engine that makes outsourcing decisions using vidur-style components.

    Uses the abstractions to remain engine-agnostic while providing sophisticated
    outsourcing decisions based on:
    - SLO violation detection
    - FLOP-based knapsack optimization
    - Cost-aware prioritization
    """

    def __init__(
        self,
        waiting_queue: WaitingQueueInterface,
        flop_calculator: FLOPCalculatorInterface,
        model_id: str = "default",
        max_batch_size: int = 256,
        prefill_slo_base_seconds: float = 4.05,
        prefill_slo_slack_factor: float = 10.0,
        utilization_target: float = 0.8,
        input_price_per_million: float = 1.25,
        output_price_per_million: float = 10.00,
        knapsack_strategy: str = "dp_scaled",
        violation_detection_mode: str = "all",
        decode_weight_ratio: float = 0.6,
        budget_horizon_iterations: int = 2,
        enable_iterative_outsourcing: bool = True,
        max_outsourcing_iterations: int = 100,
        debug_outsourcing: bool = False,
    ):
        """Initialize the outsourcing engine with vidur-style components.

        Args:
            waiting_queue: Queue interface for accessing waiting requests
            flop_calculator: FLOP calculator for throughput estimation
            model_id: Model identifier for tracking
            max_batch_size: Maximum batch size for violation detection
            prefill_slo_base_seconds: Base SLO for prefill (fallback)
            prefill_slo_slack_factor: Slack factor for derived SLOs
            utilization_target: Target utilization for effective FLOPS
            input_price_per_million: API input price per million tokens
            output_price_per_million: API output price per million tokens
            knapsack_strategy: Strategy for knapsack solver ("dp_scaled", "fractional", etc.)
            violation_detection_mode: Mode for violation detection ("all" or "head")
            decode_weight_ratio: Weight ratio for decode vs prefill in knapsack
            budget_horizon_iterations: Budget horizon for knapsack
            enable_iterative_outsourcing: If True, use vidur-style iterative loop; if False, single-pass
            max_outsourcing_iterations: Maximum iterations for iterative outsourcing (safety limit)
            debug_outsourcing: Enable debug logging
        """
        self.waiting_queue = waiting_queue
        self.flop_calculator = flop_calculator
        self.model_id = model_id
        self.max_batch_size = max_batch_size
        self.prefill_slo_base_seconds = prefill_slo_base_seconds
        self.prefill_slo_slack_factor = prefill_slo_slack_factor
        self.utilization_target = utilization_target
        self.decode_weight_ratio = decode_weight_ratio
        self.budget_horizon_iterations = budget_horizon_iterations
        self.enable_iterative_outsourcing = enable_iterative_outsourcing
        self.max_outsourcing_iterations = max_outsourcing_iterations
        self.debug_outsourcing = debug_outsourcing

        self.outsourced_request_ids: set[str] = set()

        # Initialize vidur-style components
        self._cost_calculator = APICostCalculator(
            input_price_per_million=input_price_per_million,
            output_price_per_million=output_price_per_million,
        )

        self._request_tracker = RequestTracker(
            model_id=model_id,
            cost_calculator=self._cost_calculator.calculate_cost,
        )

        self._candidate_selector = CandidateSelector()

        self._knapsack_solver = KnapsackSolver(strategy=knapsack_strategy)

        self._violation_detector = TTFTViolationDetector(
            flop_calculator=flop_calculator,
            mode=violation_detection_mode,
            max_micro_batch_size=max_batch_size,
            utilization_target=utilization_target,
        )

    def should_outsource(self, current_time: float) -> OutsourcingDecision:
        """Main entry point: decide if outsourcing is needed.

        Uses vidur-style iterative outsourcing (if enabled) or single-pass decision.
        Call this before each scheduling cycle.
        """
        if self.enable_iterative_outsourcing:
            return self._iterative_outsourcing(current_time)
        else:
            return self._single_pass_outsourcing(current_time)

    def _iterative_outsourcing(self, current_time: float) -> OutsourcingDecision:
        """Iteratively outsource one request at a time until TTFT violations are resolved.
        
        This is the vidur approach: conservative outsourcing that only removes the minimum
        needed to resolve violations.
        """
        waiting_requests = self.waiting_queue.get_all_waiting()

        if not waiting_requests:
            return OutsourcingDecision(
                should_outsource=False,
                requests_to_outsource=[],
                requests_to_keep=[],
                reason="No waiting requests",
            )

        all_outsourced = []
        iteration = 0

        while iteration < self.max_outsourcing_iterations:
            # Re-fetch waiting requests (some may have been removed)
            waiting_requests = self.waiting_queue.get_all_waiting()
            
            if not waiting_requests:
                break

            # Collect candidates (exclude already outsourced)
            candidates = self._candidate_selector.collect_candidates(
                waiting_requests=waiting_requests,
                outsourced_req_ids=self.outsourced_request_ids,
            )

            if not candidates:
                if self.debug_outsourcing:
                    from serving.utils.logging import get_logger
                    logger = get_logger(__name__)
                    logger.info(f"[{self.model_id}] No more outsourcing candidates, stopping at iteration {iteration}")
                break

            # Check for TTFT violations
            has_violations = self._violation_detector.check_violations(
                candidates, current_time
            )

            if not has_violations:
                # No violations detected, we're done
                if iteration > 0 and self.debug_outsourcing:
                    from serving.utils.logging import get_logger
                    logger = get_logger(__name__)
                    logger.info(f"[{self.model_id}] TTFT violations resolved after {iteration} outsourcing iteration(s)")
                break

            if iteration == 0 and self.debug_outsourcing:
                from serving.utils.logging import get_logger
                logger = get_logger(__name__)
                logger.info(f"[{self.model_id}] TTFT violation detected at t={current_time:.2f}")

            # Build knapsack items
            items = [self._build_knapsack_item(r) for r in candidates]

            # Calculate total weight needed to keep all candidates local
            total_weight = sum(item["weight"] for item in items)

            # Set budget to total_weight - 1 to force outsourcing of at least one request
            # This ensures we select all but one request to keep local
            budget = max(1, total_weight - 1)

            # Solve knapsack - this will select requests to KEEP local
            keep_ids, outsource_ids = self._knapsack_solver.solve(items, budget)

            # If knapsack couldn't outsource anything (shouldn't happen with budget = total - 1)
            # fall back to outsourcing the lowest value request
            if not outsource_ids:
                # Sort by value (lowest first) and outsource the cheapest one
                sorted_items = sorted(items, key=lambda x: x["value"])
                outsource_ids = [sorted_items[0]["id"]]
                if self.debug_outsourcing:
                    from serving.utils.logging import get_logger
                    logger = get_logger(__name__)
                    logger.info(f"[{self.model_id}] Knapsack didn't outsource, manually selecting lowest-value request")

            # Outsource only ONE request (the first one selected)
            # This is more conservative than outsourcing all at once
            single_outsource = [outsource_ids[0]] if outsource_ids else []

            if single_outsource:
                if self.debug_outsourcing:
                    from serving.utils.logging import get_logger
                    logger = get_logger(__name__)
                    logger.info(f"[{self.model_id}] Iteration {iteration + 1}: Outsourcing 1 request: {single_outsource[0]}")
                
                # Remove from queue and track
                outsourced = self.waiting_queue.remove_requests(set(single_outsource))
                for req in outsourced:
                    self.outsourced_request_ids.add(req.request_id)
                    self._request_tracker.track_outsourced_request(req, current_time)
                    all_outsourced.append(req.request_id)
                
                iteration += 1
            else:
                # No request to outsource, break
                if self.debug_outsourcing:
                    from serving.utils.logging import get_logger
                    logger = get_logger(__name__)
                    logger.info(f"[{self.model_id}] No request selected for outsourcing at iteration {iteration}")
                break

        # Safety limit reached
        if iteration >= self.max_outsourcing_iterations and self.debug_outsourcing:
            from serving.utils.logging import get_logger
            logger = get_logger(__name__)
            logger.warning(f"[{self.model_id}] Reached max outsourcing iterations ({self.max_outsourcing_iterations}), violations may still exist")

        # Get final waiting requests for keep list
        final_waiting = self.waiting_queue.get_all_waiting()
        keep_ids = [r.request_id for r in final_waiting if r.request_id not in self.outsourced_request_ids]

        if all_outsourced:
            return OutsourcingDecision(
                should_outsource=True,
                requests_to_outsource=all_outsourced,
                requests_to_keep=keep_ids,
                reason=f"Iterative outsourcing: {len(all_outsourced)} request(s) in {iteration} iteration(s)",
                metrics={
                    "iterations": iteration,
                    "total_waiting": len(waiting_requests),
                    "outsource_count": len(all_outsourced),
                    "keep_count": len(keep_ids),
                },
            )
        else:
            return OutsourcingDecision(
                should_outsource=False,
                requests_to_outsource=[],
                requests_to_keep=keep_ids,
                reason="No SLO violations detected",
            )

    def _single_pass_outsourcing(self, current_time: float) -> OutsourcingDecision:
        """Make outsourcing decision in single pass (original hybridInference approach)."""
        waiting_requests = self.waiting_queue.get_all_waiting()

        if not waiting_requests:
            return OutsourcingDecision(
                should_outsource=False,
                requests_to_outsource=[],
                requests_to_keep=[],
                reason="No waiting requests",
            )

        # Collect candidates (exclude already outsourced)
        candidates = self._candidate_selector.collect_candidates(
            waiting_requests=waiting_requests,
            outsourced_req_ids=self.outsourced_request_ids,
        )

        if not candidates:
            return OutsourcingDecision(
                should_outsource=False,
                requests_to_outsource=[],
                requests_to_keep=[],
                reason="No candidates for outsourcing",
            )

        # Check for TTFT violations using the violation detector
        has_violations = self._violation_detector.check_violations(
            candidates, current_time
        )

        if not has_violations:
            return OutsourcingDecision(
                should_outsource=False,
                requests_to_outsource=[],
                requests_to_keep=[r.request_id for r in candidates],
                reason="No SLO violations detected",
            )

        # Run knapsack to decide what to keep vs outsource
        keep_ids, outsource_ids = self._knapsack_selection(candidates, current_time)

        return OutsourcingDecision(
            should_outsource=len(outsource_ids) > 0,
            requests_to_outsource=outsource_ids,
            requests_to_keep=keep_ids,
            reason=f"SLO violations detected, outsourcing {len(outsource_ids)} requests",
            metrics={
                "total_waiting": len(waiting_requests),
                "candidates": len(candidates),
                "outsource_count": len(outsource_ids),
                "keep_count": len(keep_ids),
            },
        )

    def apply_outsourcing(self, decision: OutsourcingDecision) -> list[OutsourcingRequestInfo]:
        """Execute the outsourcing decision by removing requests from queue.

        Note: For iterative outsourcing, requests are already removed during the loop.
        This method is primarily for single-pass mode or for compatibility.

        Returns the outsourced requests for handoff to external service.
        """
        if not decision.should_outsource:
            return []

        # For iterative mode, requests are already removed and tracked
        if self.enable_iterative_outsourcing:
            # Just return empty list since tracking already happened
            return []

        # For single-pass mode, remove and track requests
        outsourced = self.waiting_queue.remove_requests(set(decision.requests_to_outsource))

        # Track outsourced requests
        current_time = outsourced[0].arrival_time if outsourced else 0.0  # Approximation
        for req in outsourced:
            self.outsourced_request_ids.add(req.request_id)
            self._request_tracker.track_outsourced_request(req, current_time)

        return outsourced

    def get_outsourced_request_details(self) -> list[dict]:
        """Return the list of outsourced request details."""
        return self._request_tracker.get_outsourced_request_details()

    def get_outsourcing_statistics(self) -> dict:
        """Calculate and return outsourcing statistics."""
        return self._request_tracker.get_outsourcing_statistics()

    def _build_knapsack_item(self, req: OutsourcingRequestInfo) -> dict:
        """Build a knapsack item for a request.
        
        Weight: total FLOPs remaining (prefill + weighted decode)
        Value: cost savings from keeping local (API cost avoided)
        """
        # Weight = remaining FLOPs (prefill + weighted decode)
        prefill_flops = 0.0
        if req.remaining_prompt_tokens > 0:
            prefill_flops = self.flop_calculator.compute_prefill_flops(
                req, req.remaining_prompt_tokens
            )

        decode_flops = 0.0
        if req.remaining_output_tokens > 0:
            decode_flops = self.flop_calculator.compute_decode_flops(
                req, req.remaining_output_tokens
            )

        weight = int(prefill_flops + self.decode_weight_ratio * decode_flops)

        # Value = cost savings from NOT outsourcing (API cost avoided)
        api_cost = self._cost_calculator.calculate_cost(
            req.remaining_prompt_tokens, req.remaining_output_tokens
        )
        value = int(api_cost * 1000)  # Scale to avoid float issues in DP

        return {"id": req.request_id, "weight": max(1, weight), "value": max(1, value)}

    def _knapsack_selection(
        self, candidates: list[OutsourcingRequestInfo], current_time: float
    ) -> tuple[list[str], list[str]]:
        """Knapsack-based selection using vidur's approach.

        Weight: total FLOPs remaining (prefill + weighted decode)
        Value: cost savings from keeping local (API cost avoided)

        Returns: (keep_request_ids, outsource_request_ids)
        """
        if not candidates:
            return [], []

        # Get model dimensions for FLOP calculation
        effective_flops = self.flop_calculator.get_effective_flops_per_second(
            self.utilization_target
        )

        # Build knapsack items
        items = [self._build_knapsack_item(req) for req in candidates]

        # Budget: ensure we can outsource at least one request
        # Set budget to total_weight - min_weight to force outsourcing
        total_weight = sum(item["weight"] for item in items)
        min_weight = min(item["weight"] for item in items) if items else 0
        budget = max(1, total_weight - min_weight)

        # Solve knapsack - this will select requests to KEEP local
        keep_ids, outsource_ids = self._knapsack_solver.solve(items, budget)

        return list(keep_ids), outsource_ids
