"""Usage example for the outsourcing system.

This example demonstrates three scenarios:
1. Basic queue and outsourcing engine usage
2. Full integration with serving adapters
3. Production-ready router with SLO-aware outsourcing
"""

import asyncio
import sys
import time
from pathlib import Path

# Allow running as a script
if __name__ == "__main__":
    # Add parent directory to path for imports
    parent_dir = Path(__file__).resolve().parent.parent.parent
    sys.path.insert(0, str(parent_dir))

from routing.outsourcing.adapters import SGLangWaitingQueueAdapter
from routing.outsourcing.decision import OutsourcingEngine
from routing.outsourcing.flop_calculator import SimpleFLOPCalculator


def example_usage():
    """Example of how to integrate outsourcing into a serving loop."""
    print("=" * 70)
    print("Outsourcing System Example")
    print("=" * 70)

    # 1. Create the waiting queue adapter with metrics endpoint
    print("\n1. Creating SGLang waiting queue adapter...")
    waiting_queue = SGLangWaitingQueueAdapter(metrics_url="http://localhost:30000/metrics")
    print(f"   Queue initialized (length: {waiting_queue.get_length()})")

    # 2. Add some mock requests to the queue
    print("\n2. Adding sample requests to the queue...")

    # Add a small request
    waiting_queue.add_request(
        request_id="req-001",
        num_prompt_tokens=100,
        num_output_tokens=50,
        prefill_slo_seconds=5.0,
        metadata={"priority": "high"},
    )

    # Add a large request with tight SLO
    waiting_queue.add_request(
        request_id="req-002",
        num_prompt_tokens=2000,
        num_output_tokens=500,
        prefill_slo_seconds=3.0,  # Tight SLO!
        metadata={"priority": "medium"},
    )

    # Add a medium request
    waiting_queue.add_request(
        request_id="req-003",
        num_prompt_tokens=500,
        num_output_tokens=200,
        prefill_slo_seconds=8.0,
        metadata={"priority": "low"},
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
            print(
                f"   - {req.request_id}: {req.num_prompt_tokens} prompt tokens, "
                f"{req.num_output_tokens} output tokens"
            )
    else:
        print("   (Queue is empty)")

    print("\n" + "=" * 70)
    print("Example complete!")
    print("=" * 70)


def example_serving_integration():
    """Example showing integration with serving adapters.

    This demonstrates how requests flow through the system:
    1. Request arrives → waiting queue
    2. Outsourcing decision → local SGLang or external API
    3. Adapter executes the request
    """
    print("\n" + "=" * 70)
    print("Serving Integration Example")
    print("=" * 70)

    # Setup waiting queue and outsourcing engine
    print("\n1. Setting up outsourcing system...")
    waiting_queue = SGLangWaitingQueueAdapter(metrics_url="http://localhost:30000/metrics")

    flop_calculator = SimpleFLOPCalculator(
        hidden_dim=4096,
        num_layers=32,
        num_attention_heads=32,
        device_tflops=312.0,
    )

    outsourcing_engine = OutsourcingEngine(
        waiting_queue=waiting_queue,
        flop_calculator=flop_calculator,
        max_batch_size=256,
        prefill_slo_base_seconds=4.05,
        utilization_target=0.8,
    )

    print("   ✓ Outsourcing engine ready")

    # Simulate incoming requests
    print("\n2. Simulating incoming requests...")
    requests = [
        {
            "id": "req-001",
            "prompt": "What is machine learning?",
            "max_tokens": 100,
            "slo": 5.0,
        },
        {
            "id": "req-002",
            "prompt": "Explain quantum computing in detail with examples and applications",
            "max_tokens": 1000,
            "slo": 2.0,  # Tight SLO - likely to be outsourced
        },
        {
            "id": "req-003",
            "prompt": "Hello, how are you?",
            "max_tokens": 50,
            "slo": 10.0,
        },
    ]

    # Add requests to queue
    for req in requests:
        # Estimate tokens (simple char-based estimate)
        num_prompt_tokens = len(req["prompt"]) // 4

        waiting_queue.add_request(
            request_id=req["id"],
            num_prompt_tokens=num_prompt_tokens,
            num_output_tokens=req["max_tokens"],
            prefill_slo_seconds=req["slo"],
            metadata={"prompt": req["prompt"]},
        )
        print(f"   Added {req['id']}: {num_prompt_tokens} tokens, SLO={req['slo']}s")

    # Make outsourcing decision
    print("\n3. Making outsourcing decision...")
    decision = outsourcing_engine.should_outsource(time.time())

    print(f"   Decision: {'OUTSOURCE' if decision.should_outsource else 'KEEP ALL LOCAL'}")
    print(f"   Reason: {decision.reason}")

    if decision.should_outsource:
        print(f"\n   → Outsourcing {len(decision.requests_to_outsource)} requests:")
        for req_id in decision.requests_to_outsource:
            req = next(r for r in requests if r["id"] == req_id)
            print(f"      {req_id} → External API (OpenAI/Claude)")
            print(f"         Reason: Tight SLO ({req['slo']}s) would be violated")

        print(f"\n   → Keeping {len(decision.requests_to_keep)} requests local:")
        for req_id in decision.requests_to_keep:
            print(f"      {req_id} → SGLang (local)")

        # Apply the decision
        outsourced = outsourcing_engine.apply_outsourcing(decision)

        print("\n4. Routing summary:")
        print(f"   - SGLang queue: {waiting_queue.get_length()} requests")
        print(f"   - Outsourced: {len(outsourced)} requests")
        print("\n   In production, outsourced requests would be sent to:")
        print("   - OpenAI API (gpt-4o-mini, gpt-4o)")
        print("   - Anthropic API (claude-3-5-sonnet)")
        print("   - Or other external providers")
    else:
        print("\n   All requests can be handled locally by SGLang")

    print("\n" + "=" * 70)


async def example_full_router():
    """Example showing the complete OutsourcingRouter in action.

    This is the production-ready setup that you would use in your serving layer.
    """
    print("\n" + "=" * 70)
    print("Full Router Integration Example")
    print("=" * 70)

    print("\n1. Setting up components...")

    # Import serving components
    from routing.executor import RouteExecutor
    from routing.outsourcing_integration import OutsourcingRouter
    from serving.adapters.base import ModelConfig
    from serving.adapters.openai import OpenAIAdapter
    from serving.adapters.openai_compat import OpenAICompatAdapter

    # Create adapters
    sglang_config = ModelConfig(
        id="local-llama",
        name="Llama-3.1-8B (Local)",
        provider="sglang",
        base_url="http://localhost:30000",
        context_length=8192,
        max_output_length=4096,
    )
    sglang_adapter = OpenAICompatAdapter(sglang_config)

    openai_config = ModelConfig(
        id="gpt-4o-mini",
        name="GPT-4o Mini",
        provider="openai",
        base_url="https://api.openai.com/v1",
        api_key="sk-...",  # Replace with actual key
    )
    openai_adapter = OpenAIAdapter(openai_config)

    print("   ✓ Created SGLang (OpenAI-compatible) and OpenAI adapters")

    # Create route executor
    route_executor = RouteExecutor()
    route_executor.register_route("local-llama", [(sglang_adapter, 1.0)])
    route_executor.register_route("gpt-4o-mini", [(openai_adapter, 1.0)])

    print("   ✓ Registered routes")

    # Create outsourcing components
    waiting_queue = SGLangWaitingQueueAdapter(metrics_url="http://localhost:30000/metrics")

    flop_calculator = SimpleFLOPCalculator(
        hidden_dim=4096,
        num_layers=32,
        num_attention_heads=32,
        device_tflops=312.0,
    )

    outsourcing_engine = OutsourcingEngine(
        waiting_queue=waiting_queue,
        flop_calculator=flop_calculator,
        max_batch_size=256,
        prefill_slo_base_seconds=4.05,
        utilization_target=0.8,
    )

    print("   ✓ Created outsourcing engine")

    # Create the outsourcing router
    router = OutsourcingRouter(
        route_executor=route_executor,
        outsourcing_engine=outsourcing_engine,
        waiting_queue=waiting_queue,
        local_model_id="local-llama",
        external_model_id="gpt-4o-mini",
    )

    print("   ✓ Created outsourcing router")

    print("\n2. Processing requests through the router...")

    # Example messages
    messages = [{"role": "user", "content": "What is the capital of France?"}]

    try:
        # This would normally go to SGLang, but if the queue is backed up,
        # it might be outsourced to OpenAI
        print("\n   Sending request...")
        print(f"   Messages: {messages}")

        # In production, you would do:
        # response = await router.chat_completion(
        #     messages=messages,
        #     prefill_slo_seconds=3.0,
        #     max_tokens=100,
        # )

        print("\n   ✓ Request would be routed based on queue state")
        print("   - If SGLang queue is healthy: → SGLang (local)")
        print("   - If SLO would be violated: → OpenAI (external)")

    except Exception as e:
        print(f"\n   (Skipping actual API call: {e})")

    # Show router stats
    print("\n3. Router statistics:")
    stats = router.get_stats()
    for key, value in stats.items():
        if key != "sglang_metrics":
            print(f"   - {key}: {value}")

    print("\n" + "=" * 70)
    print("Integration Complete!")
    print("=" * 70)
    print("\nIn production, you would:")
    print("1. Start SGLang server: python -m sglang.launch_server ...")
    print("2. Initialize the OutsourcingRouter in your serving layer")
    print("3. Route all requests through router.chat_completion()")
    print("4. Monitor metrics at /metrics endpoint")
    print("5. Adjust outsourcing parameters based on cost/performance")
    print("=" * 70)


if __name__ == "__main__":
    # Run the basic example
    example_usage()

    # Run the serving integration example
    example_serving_integration()

    # Run the full router example (async)
    asyncio.run(example_full_router())
