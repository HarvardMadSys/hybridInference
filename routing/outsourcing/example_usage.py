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
from routing.outsourcing.adapters import SGLangWaitingQueueAdapter

def example_usage():
    """Example of how to integrate outsourcing into a serving loop."""
    
    print("=" * 70)
    print("Outsourcing System Example")
    print("=" * 70)
    
    # 1. Create the waiting queue adapter with metrics endpoint
    print("\n1. Creating SGLang waiting queue adapter...")
    waiting_queue = SGLangWaitingQueueAdapter(
        metrics_url="http://localhost:30000/metrics"
    )
    print(f"   Queue initialized (length: {waiting_queue.get_length()})")

    # 2. Add some mock requests to the queue
    print("\n2. Adding sample requests to the queue...")
    
    # Add a small request
    waiting_queue.add_request(
        request_id="req-001",
        num_prompt_tokens=100,
        num_output_tokens=50,
        prefill_slo_seconds=5.0,
        metadata={'priority': 'high'}
    )
    
    # Add a large request with tight SLO
    waiting_queue.add_request(
        request_id="req-002",
        num_prompt_tokens=2000,
        num_output_tokens=500,
        prefill_slo_seconds=3.0,  # Tight SLO!
        metadata={'priority': 'medium'}
    )
    
    # Add a medium request
    waiting_queue.add_request(
        request_id="req-003",
        num_prompt_tokens=500,
        num_output_tokens=200,
        prefill_slo_seconds=8.0,
        metadata={'priority': 'low'}
    )
    
    print(f"   Added 3 requests (queue length: {waiting_queue.get_length()})")

    # 3. Fetch and display metrics (if available)
    print("\n3. Fetching SGLang metrics...")
    metrics = waiting_queue.get_metrics(safe=True)
    if metrics:
        print("   Live metrics from SGLang:")
        for key, value in metrics.items():
            print(f"     - {key}: {value}")
    else:
        print("   (Metrics endpoint not available - using mock queue)")

    # 4. Create FLOP calculator
    print("\n4. Setting up FLOP calculator for Llama-3.1-8B on A100...")
    flop_calculator = SimpleFLOPCalculator(
        hidden_dim=4096,
        num_layers=32,
        num_attention_heads=32,
        device_tflops=312.0,  # A100
    )

    # 5. Initialize outsourcing engine
    print("\n5. Initializing outsourcing engine...")
    outsourcing = OutsourcingEngine(
        waiting_queue=waiting_queue,
        flop_calculator=flop_calculator,
        max_batch_size=256,
        prefill_slo_base_seconds=4.05,
        utilization_target=0.8,
    )
    print("   Engine ready")

    # 6. Make outsourcing decision
    print("\n6. Making outsourcing decision...")
    current_time = time.time()
    decision = outsourcing.should_outsource(current_time)

    print(f"\n   Decision: {'OUTSOURCE' if decision.should_outsource else 'KEEP LOCAL'}")
    print(f"   Reason: {decision.reason}")
    print(f"   Metrics: {decision.metrics}")

    if decision.should_outsource:
        print(f"\n   Requests to outsource ({len(decision.requests_to_outsource)}):")
        for req_id in decision.requests_to_outsource:
            print(f"     - {req_id}")
        
        print(f"\n   Requests to keep local ({len(decision.requests_to_keep)}):")
        for req_id in decision.requests_to_keep:
            print(f"     - {req_id}")

        # 7. Apply outsourcing decision
        print("\n7. Applying outsourcing decision...")
        outsourced_requests = outsourcing.apply_outsourcing(decision)

        print(f"   Removed {len(outsourced_requests)} requests from queue")
        print(f"   Queue now has {waiting_queue.get_length()} requests")

        # Send to external service (e.g., OpenAI API)
        print("\n   Outsourced request details:")
        for req in outsourced_requests:
            print(f"     {req.request_id}:")
            print(f"       - Prompt tokens: {req.num_prompt_tokens}")
            print(f"       - Output tokens: {req.num_output_tokens}")
            print(f"       - Queue time: {req.queue_time:.2f}s")
            print(f"       - Estimated value: ${req.estimated_value:.6f}")
            # In production: send_to_external_service(req)
    else:
        print("\n   No outsourcing needed - all requests can be served locally")

    # 8. Show remaining queue
    print("\n8. Remaining queue state:")
    remaining = waiting_queue.get_all_waiting()
    if remaining:
        for req in remaining:
            print(f"   - {req.request_id}: {req.num_prompt_tokens} prompt tokens, "
                  f"{req.num_output_tokens} output tokens")
    else:
        print("   (Queue is empty)")
    
    print("\n" + "=" * 70)
    print("Example complete!")
    print("=" * 70)


if __name__ == "__main__":
    example_usage()
