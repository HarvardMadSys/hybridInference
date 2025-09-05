#!/usr/bin/env python3
"""Test script for rate limiting functionality."""

import asyncio
import time
from typing import Any

import aiohttp


async def send_request(
    session: aiohttp.ClientSession,
    url: str,
    model: str,
    messages: list[dict[str, Any]],
    max_tokens: int = 100,
) -> dict[str, Any]:
    """Send a single chat completion request."""
    payload = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": 0.7}

    try:
        async with session.post(url, json=payload) as response:
            result = await response.json()
            return {
                "status": response.status,
                "headers": dict(response.headers),
                "body": result,
                "timestamp": time.time(),
            }
    except Exception as e:
        return {"status": -1, "error": str(e), "timestamp": time.time()}


async def test_rate_limiting(base_url: str = "http://localhost:8080"):
    """Test rate limiting with burst requests."""

    # Test configuration
    test_cases = [
        {
            "name": "Gemini Flash 2.5 - Burst Test",
            "model": "gemini-2.5-flash",
            "requests": 10,
            "tokens_per_request": 100000,  # 100K tokens per request
            "expected_accepts": 10,  # Should accept all within 1M/min limit
        },
        {
            "name": "Gemini Flash 2.5 - Over Limit Test",
            "model": "gemini-2.5-flash",
            "requests": 15,
            "tokens_per_request": 100000,  # 100K tokens per request
            "expected_accepts": 10,  # Should reject after 1M tokens
        },
        {
            "name": "DeepSeek - Daily Limit Test",
            "model": "deepseek-chat",
            "requests": 5,
            "tokens_per_request": 250000,  # 250K tokens per request
            "expected_accepts": 4,  # Should reject after 1M tokens
        },
    ]

    async with aiohttp.ClientSession() as session:
        print("\n" + "=" * 60)
        print("RATE LIMITING TEST SUITE")
        print("=" * 60)

        for test in test_cases:
            print(f"\n[TEST] {test['name']}")
            print(f"  Model: {test['model']}")
            print(f"  Requests: {test['requests']}")
            print(f"  Tokens per request: {test['tokens_per_request']:,}")
            print(f"  Total tokens: {test['requests'] * test['tokens_per_request']:,}")

            # Generate messages with appropriate size
            # Roughly 4 chars = 1 token
            content_size = test["tokens_per_request"] * 4 - 100
            messages = [{"role": "user", "content": "a" * content_size}]

            # Send burst of requests
            tasks = []
            for _i in range(test["requests"]):
                task = send_request(
                    session,
                    f"{base_url}/v1/chat/completions",
                    test["model"],
                    messages,
                    max_tokens=100,
                )
                tasks.append(task)

            results = await asyncio.gather(*tasks)

            # Analyze results
            accepted = sum(1 for r in results if r["status"] == 200)
            rejected = sum(1 for r in results if r["status"] == 429)
            errors = sum(1 for r in results if r["status"] not in [200, 429])

            print("\n  Results:")
            print(f"    Accepted: {accepted}/{test['requests']}")
            print(f"    Rejected: {rejected}/{test['requests']}")
            print(f"    Errors: {errors}/{test['requests']}")

            # Check rate limit headers
            for i, result in enumerate(results[:3]):  # Show first 3
                if result["status"] == 429:
                    print(f"\n    Request {i + 1} rejected:")
                    if "body" in result:
                        error = result["body"].get("error", {})
                        print(f"      Message: {error.get('message', 'N/A')}")
                        print(f"      Retry after: {error.get('retry_after', 'N/A')}s")

            # Test assertion
            if accepted <= test["expected_accepts"]:
                print(f"  ✓ Test passed (accepted {accepted} <= {test['expected_accepts']})")
            else:
                print(f"  ✗ Test failed (accepted {accepted} > {test['expected_accepts']})")

            # Check rate limit status
            async with session.get(f"{base_url}/rate-limits/{test['model']}") as response:
                if response.status == 200:
                    status = await response.json()
                    print("\n  Rate Limit Status:")
                    print(f"    Tokens available: {status.get('tokens_available', 'N/A'):,}")
                    print(f"    Queue size: {status.get('queue_size', 0)}")
                    print(f"    Circuit breaker: {status.get('circuit_breaker_state', 'N/A')}")

            # Wait before next test
            if test != test_cases[-1]:
                print("\n  Waiting 5 seconds before next test...")
                await asyncio.sleep(5)

        print("\n" + "=" * 60)
        print("TEST SUITE COMPLETE")
        print("=" * 60)


async def test_persistence(base_url: str = "http://localhost:8080"):
    """Test that rate limits persist across restarts."""
    print("\n[TEST] Persistence Test")
    print("  1. Send requests to use up some quota")
    print("  2. Restart server manually")
    print("  3. Check that quota is still consumed")

    async with aiohttp.ClientSession() as session:
        # Check initial status
        model = "gemini-2.5-flash"
        async with session.get(f"{base_url}/rate-limits/{model}") as response:
            if response.status == 200:
                status = await response.json()
                initial_tokens = status.get("tokens_available", 0)
                print(f"\n  Initial tokens available: {initial_tokens:,}")

        # Send a large request
        messages = [{"role": "user", "content": "a" * 100000}]
        result = await send_request(
            session, f"{base_url}/v1/chat/completions", model, messages, max_tokens=1000
        )

        print(f"  Request sent: status {result['status']}")

        # Check status after request
        async with session.get(f"{base_url}/rate-limits/{model}") as response:
            if response.status == 200:
                status = await response.json()
                after_tokens = status.get("tokens_available", 0)
                print(f"  Tokens after request: {after_tokens:,}")
                print(f"  Tokens consumed: {initial_tokens - after_tokens:,}")

        print("\n  Now restart the server and run this test again to verify persistence.")


async def test_queue_behavior(base_url: str = "http://localhost:8080"):
    """Test request queueing behavior."""
    print("\n[TEST] Queue Behavior Test")

    async with aiohttp.ClientSession() as session:
        model = "gemini-2.5-flash"

        # Send many small requests concurrently
        print("  Sending 20 concurrent requests...")
        messages = [{"role": "user", "content": "Hello"}]

        tasks = []
        for _i in range(20):
            task = send_request(
                session, f"{base_url}/v1/chat/completions", model, messages, max_tokens=10
            )
            tasks.append(task)

        start_time = time.time()
        results = await asyncio.gather(*tasks)
        duration = time.time() - start_time

        accepted = sum(1 for r in results if r["status"] == 200)
        queued = sum(1 for r in results if r.get("body", {}).get("was_queued"))

        print("\n  Results:")
        print(f"    Duration: {duration:.2f}s")
        print(f"    Accepted: {accepted}/20")
        print(f"    Queued: {queued}")

        # Check final queue status
        async with session.get(f"{base_url}/rate-limits/{model}") as response:
            if response.status == 200:
                status = await response.json()
                print(f"    Final queue size: {status.get('queue_size', 0)}")


async def main():
    """Run all tests."""
    base_url = "http://localhost:8080"

    print("Starting Rate Limiter Tests")
    print(f"Server URL: {base_url}")

    # Run tests
    await test_rate_limiting(base_url)
    await test_queue_behavior(base_url)
    await test_persistence(base_url)


if __name__ == "__main__":
    asyncio.run(main())
