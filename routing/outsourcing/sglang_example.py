"""Example of using the outsourcing system with SGLang."""

import sys
import time
from pathlib import Path

# Allow running as a script
if __name__ == "__main__":
    parent_dir = Path(__file__).resolve().parent.parent.parent
    sys.path.insert(0, str(parent_dir))

from routing.outsourcing import (
    OutsourcingEngine,
    SGLangWaitingQueueAdapter,
    SimpleFLOPCalculator,
)


def example_with_sglang_integration():
    """Example showing SGLang integration with outsourcing."""
    
    print("=" * 70)
    print("SGLang Outsourcing Integration Example")
    print("=" * 70)
    
    # Step 1: Initialize your SGLang scheduler
    # In practice, you would have something like:
    # from sglang import Scheduler
    # scheduler = Scheduler(model_path="meta-llama/Llama-3.1-8B-Instruct", ...)
    
    # For this example, we'll use a mock scheduler
    class MockSGLangScheduler:
        """Mock SGLang scheduler for demonstration."""
        
        def __init__(self):
            self.waiting_queue = []
            
        def add_request(self, request):
            """Add a request to the waiting queue."""
            self.waiting_queue.append(request)
    
    class MockSGLangRequest:
        """Mock SGLang request for demonstration."""
        
        def __init__(self, request_id, prompt_tokens, max_new_tokens, 
                     created_time=None, metadata=None):
            self.request_id = request_id
            self.prompt_tokens = prompt_tokens
            self.created_time = created_time or time.time()
            self.num_processed_tokens = 0
            self.is_prefill_complete = False
            self.metadata = metadata or {}
            
            # Mock sampling params
            class SamplingParams:
                def __init__(self, max_new_tokens):
                    self.max_new_tokens = max_new_tokens
            
            self.sampling_params = SamplingParams(max_new_tokens)
    
    # Create mock scheduler with some waiting requests
    scheduler = MockSGLangScheduler()
    
    # Add some requests with varying sizes
    print("\nAdding requests to SGLang scheduler...")
    scheduler.add_request(MockSGLangRequest(
        "req-1", 
        prompt_tokens=list(range(100)),  # 100 tokens
        max_new_tokens=50,
        created_time=time.time() - 2.0,  # Arrived 2 seconds ago
        metadata={'prefill_slo_seconds': 5.0}
    ))
    
    scheduler.add_request(MockSGLangRequest(
        "req-2",
        prompt_tokens=list(range(2000)),  # 2000 tokens (large)
        max_new_tokens=500,
        created_time=time.time() - 1.5,  # Arrived 1.5 seconds ago
        metadata={'prefill_slo_seconds': 3.0}  # Tight SLO!
    ))
    
    scheduler.add_request(MockSGLangRequest(
        "req-3",
        prompt_tokens=list(range(500)),  # 500 tokens
        max_new_tokens=200,
        created_time=time.time() - 1.0,
    ))
    
    scheduler.add_request(MockSGLangRequest(
        "req-4",
        prompt_tokens=list(range(150)),  # 150 tokens
        max_new_tokens=100,
        created_time=time.time() - 0.5,
        metadata={'prefill_slo_seconds': 10.0}  # Relaxed SLO
    ))
    
    print(f"  Added {len(scheduler.waiting_queue)} requests to queue")
    
    # Step 2: Create the SGLang adapter
    print("\nCreating SGLang queue adapter...")
    queue_adapter = SGLangWaitingQueueAdapter(scheduler)
    
    # Verify adapter can read the queue
    waiting = queue_adapter.get_all_waiting()
    print(f"  Adapter found {len(waiting)} waiting requests")
    for req in waiting:
        print(f"    - {req.request_id}: {req.num_prompt_tokens} prompt tokens, "
              f"{req.num_output_tokens} output tokens, "
              f"queue time: {req.queue_time:.2f}s")
    
    # Step 3: Create FLOP calculator
    print("\nCreating FLOP calculator for Llama-3.1-8B...")
    flop_calculator = SimpleFLOPCalculator(
        hidden_dim=4096,
        num_layers=32,
        num_attention_heads=32,
        device_tflops=312.0,  # A100 GPU
    )
    
    # Step 4: Initialize outsourcing engine
    print("\nInitializing outsourcing engine...")
    outsourcing_engine = OutsourcingEngine(
        waiting_queue=queue_adapter,
        flop_calculator=flop_calculator,
        max_batch_size=256,
        prefill_slo_base_seconds=4.05,
        prefill_slo_slack_factor=10.0,
        utilization_target=0.8,
    )
    
    # Step 5: Make outsourcing decision
    print("\nMaking outsourcing decision...")
    current_time = time.time()
    decision = outsourcing_engine.should_outsource(current_time)
    
    print(f"\n  Decision: {'OUTSOURCE' if decision.should_outsource else 'KEEP LOCAL'}")
    print(f"  Reason: {decision.reason}")
    print(f"  Metrics: {decision.metrics}")
    
    if decision.should_outsource:
        print(f"\n  Requests to outsource ({len(decision.requests_to_outsource)}):")
        for req_id in decision.requests_to_outsource:
            print(f"    - {req_id}")
        
        print(f"\n  Requests to keep local ({len(decision.requests_to_keep)}):")
        for req_id in decision.requests_to_keep:
            print(f"    - {req_id}")
    
    # Step 6: Apply outsourcing decision
    if decision.should_outsource:
        print("\nApplying outsourcing decision...")
        outsourced_requests = outsourcing_engine.apply_outsourcing(decision)
        
        print(f"  Removed {len(outsourced_requests)} requests from SGLang queue")
        print(f"  SGLang queue now has {queue_adapter.get_length()} requests")
        
        # In production, you would send these to an external service
        print("\n  Outsourced request details:")
        for req in outsourced_requests:
            print(f"    {req.request_id}:")
            print(f"      - Prompt tokens: {req.num_prompt_tokens}")
            print(f"      - Output tokens: {req.num_output_tokens}")
            print(f"      - Estimated value: ${req.estimated_value:.6f}")
            print(f"      - Queue time: {req.queue_time:.2f}s")
            # Here you would call: send_to_external_api(req)
    
    print("\n" + "=" * 70)
    print("Example complete!")
    print("=" * 70)


def example_monitoring_loop():
    """Example showing continuous monitoring in a serving loop."""
    
    print("\n" + "=" * 70)
    print("Continuous Monitoring Example")
    print("=" * 70)
    print("\nThis shows how outsourcing would work in a serving loop.")
    print("(Simplified for demonstration)\n")
    
    # Setup (same as before)
    class MockScheduler:
        def __init__(self):
            self.waiting_queue = []
    
    scheduler = MockScheduler()
    queue_adapter = SGLangWaitingQueueAdapter(scheduler)
    flop_calculator = SimpleFLOPCalculator(hidden_dim=4096, num_layers=32, device_tflops=312.0)
    outsourcing_engine = OutsourcingEngine(
        waiting_queue=queue_adapter,
        flop_calculator=flop_calculator,
        utilization_target=0.8,
    )
    
    # Simulated serving loop
    for iteration in range(3):
        print(f"\n--- Iteration {iteration + 1} ---")
        
        # 1. Before scheduling, check for outsourcing
        decision = outsourcing_engine.should_outsource(time.time())
        
        if decision.should_outsource:
            print(f"  ⚠️  SLO violations detected!")
            outsourced = outsourcing_engine.apply_outsourcing(decision)
            print(f"  ↗️  Outsourced {len(outsourced)} requests")
        else:
            print(f"  ✓ No outsourcing needed")
        
        # 2. Continue with normal scheduling
        print(f"  📊 Queue size: {queue_adapter.get_length()}")
        
        time.sleep(0.5)
    
    print("\n" + "=" * 70)


if __name__ == "__main__":
    # Run the main example
    example_with_sglang_integration()
    
    # Run the monitoring loop example
    example_monitoring_loop()
