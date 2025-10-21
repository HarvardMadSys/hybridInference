"""Usage example for the outsourcing system."""

import sys
import time
from pathlib import Path

# Allow running as a script
if __name__ == "__main__":
    # Add parent directory to path for imports
    parent_dir = Path(__file__).resolve().parent.parent.parent
    sys.path.insert(0, str(parent_dir))

from routing.outsourcing.decision import OutsourcingEngine
from routing.outsourcing.flop_calculator import SimpleFLOPCalculator


def example_usage():
    """Example of how to integrate outsourcing into a serving loop."""
    # 1. Create adapters for serving engine
    sglang_scheduler = 
    from routing.outsourcing.adapters import SGLangWaitingQueueAdapter
    waiting_queue = SGLangWaitingQueueAdapter(sglang_scheduler)
    # waiting_queue = None  # Replace with actual adapter

    flop_calculator = SimpleFLOPCalculator(
        hidden_dim=4096,
        num_layers=32,
        device_tflops=312.0,  # A100
    )

    # 2. Initialize outsourcing engine
    outsourcing = OutsourcingEngine(
        waiting_queue=waiting_queue,
        flop_calculator=flop_calculator,
        max_batch_size=256,
        prefill_slo_base_seconds=4.05,
        utilization_target=0.8,
    )

    # 3. In serving loop (before scheduling)
    current_time = time.time()

    # Check if outsourcing is needed
    decision = outsourcing.should_outsource(current_time)

    if decision.should_outsource:
        print(f"Outsourcing {decision.metrics['outsource_count']} requests: {decision.reason}")

        # Remove from local queue and get request info
        outsourced_requests = outsourcing.apply_outsourcing(decision)

        # Send to external service (e.g., OpenAI API)
        for req in outsourced_requests:
            print(
                f"  - Outsourcing request {req.request_id}: "
                f"{req.num_prompt_tokens} prompt tokens, "
                f"{req.num_output_tokens} expected output tokens"
            )
            # send_to_external_service(req)


if __name__ == "__main__":
    example_usage()
