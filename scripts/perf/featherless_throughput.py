#!/usr/bin/env python3
"""Throughput estimator for OpenAI-compatible endpoints (e.g., Featherless).

This tool measures end-to-end latency and average completion tokens under a fixed
concurrency and then estimates daily throughput using the approximation:

  Requests per second (RPS) ≈ concurrency / mean_latency_seconds
  Requests per day ≈ RPS x 86400
  Tokens per day ≈ Requests per day x avg_completion_tokens

Usage examples:
  python scripts/perf/featherless_throughput.py \
    --base-url https://api.featherless.ai/v1 \
    --api-key $FEATHERLESS_API_KEY \
    --model zai-org/GLM-4.6 \
    --concurrent 1 --duration 120

Notes:
- Uses standard OpenAI chat completions API format.
- Falls back to estimating tokens as len(text)/4 if usage is not provided.
- Prints p50/p95/p99 latency, averages, and daily throughput estimates.
- Includes a warmup period to avoid cold start affecting measurements.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import time
from typing import Any

import aiohttp


def _approx_tokens_from_text(text: str) -> int:
    """Approximate token count from text using a rough heuristic.

    Args:
      text: The text content to estimate tokens for.

    Returns:
      Approximate token count (characters/4), minimum 1.
    """
    if not text:
        return 0
    return max(1, len(text) // 4)


async def _make_request(
    session: aiohttp.ClientSession,
    base_url: str,
    model: str,
    prompt: str,
    request_id: str,
    api_key: str | None = None,
    temperature: float = 0.7,
    max_tokens: int = 256,
    verbose: bool = False,
) -> dict[str, Any]:
    """Make a single non-streaming chat/completions request.

    Args:
      session: Shared HTTP session.
      base_url: API base URL (should include '/v1').
      model: Model identifier.
      prompt: User message content.
      request_id: Unique request ID for logging/trace.
      api_key: Optional API key for 'Authorization: Bearer'.
      temperature: Sampling temperature.
      max_tokens: Max completion tokens.
      verbose: Verbose error prints.

    Returns:
      Result dict containing success flag, latency_ms, status, usage, and content.
    """
    url = f"{base_url}/chat/completions"

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }

    headers = {
        "Content-Type": "application/json",
        "X-Request-ID": request_id,
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    start_time = time.time()
    try:
        async with session.post(url, json=payload, headers=headers) as response:
            latency_ms = (time.time() - start_time) * 1000.0
            status = response.status
            if status == 200:
                data = await response.json()
                usage = data.get("usage", {})
                choices = data.get("choices") or []
                content = ""
                if choices:
                    msg = choices[0].get("message", {})
                    content = msg.get("content", "")
                return {
                    "success": True,
                    "latency_ms": latency_ms,
                    "status": status,
                    "usage": usage,
                    "content": content,
                }
            else:
                text = await response.text()
                if verbose or status == 429:  # Always log rate limiting
                    print(f"Request {request_id} failed: {status} - {text[:200]}")
                return {
                    "success": False,
                    "latency_ms": latency_ms,
                    "status": status,
                    "error": text[:500],
                }
    except Exception as exc:  # pylint: disable=broad-except
        latency_ms = (time.time() - start_time) * 1000.0
        if verbose:
            print(f"Request {request_id} exception: {exc}")
        return {
            "success": False,
            "latency_ms": latency_ms,
            "status": 0,
            "error": str(exc),
        }


async def _worker(
    session: aiohttp.ClientSession,
    base_url: str,
    model: str,
    prompt: str,
    api_key: str | None,
    worker_id: int,
    end_time: float,
    temperature: float,
    max_tokens: int,
    verbose: bool,
    results: list[dict[str, Any]],
    warmup: bool = False,
) -> None:
    """Worker that issues back-to-back requests until end_time.

    Args:
      session: Shared HTTP session.
      base_url: API base URL.
      model: Model identifier.
      prompt: User message content.
      api_key: Optional API key.
      worker_id: Worker identifier.
      end_time: Unix timestamp when to stop.
      temperature: Sampling temperature.
      max_tokens: Max completion tokens.
      verbose: Verbose logging.
      results: Shared list to append results to.
      warmup: If True, results are not appended (warmup phase).
    """
    seq = 0
    while time.time() < end_time:
        req_id = f"w{worker_id:02d}_{seq:06d}"
        r = await _make_request(
            session=session,
            base_url=base_url,
            model=model,
            prompt=prompt,
            request_id=req_id,
            api_key=api_key,
            temperature=temperature,
            max_tokens=max_tokens,
            verbose=verbose,
        )
        if not warmup:
            results.append(r)
        seq += 1


async def run_test(
    base_url: str,
    model: str,
    prompt: str,
    api_key: str | None,
    concurrent: int,
    duration: float,
    temperature: float,
    max_tokens: int,
    verbose: bool,
    warmup_duration: float = 10.0,
) -> dict[str, Any]:
    """Run throughput measurement under fixed concurrency.

    Args:
      base_url: API base URL (should include '/v1').
      model: Model to test.
      prompt: Prompt text.
      api_key: Optional API key.
      concurrent: Number of concurrent workers (e.g., 2).
      duration: Test duration in seconds.
      temperature: Sampling temperature.
      max_tokens: Max completion tokens.
      verbose: Verbose progress.
      warmup_duration: Warmup period in seconds to avoid cold start effects.

    Returns:
      Aggregated metrics and estimates.
    """
    connector = aiohttp.TCPConnector(limit=concurrent)
    timeout = aiohttp.ClientTimeout(total=None, connect=30)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        # Warmup phase
        if warmup_duration > 0:
            print(f"Running warmup for {warmup_duration}s...")
            warmup_end = time.time() + warmup_duration
            warmup_tasks = [
                asyncio.create_task(
                    _worker(
                        session=session,
                        base_url=base_url,
                        model=model,
                        prompt=prompt,
                        api_key=api_key,
                        worker_id=i,
                        end_time=warmup_end,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        verbose=verbose,
                        results=[],
                        warmup=True,
                    )
                )
                for i in range(concurrent)
            ]
            await asyncio.gather(*warmup_tasks, return_exceptions=True)
            print("Warmup complete. Starting measurement...")

        start_time = time.time()
        end_time = start_time + duration
        results: list[dict[str, Any]] = []

        tasks = [
            asyncio.create_task(
                _worker(
                    session=session,
                    base_url=base_url,
                    model=model,
                    prompt=prompt,
                    api_key=api_key,
                    worker_id=i,
                    end_time=end_time,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    verbose=verbose,
                    results=results,
                    warmup=False,
                )
            )
            for i in range(concurrent)
        ]

        await asyncio.gather(*tasks, return_exceptions=True)

        # Aggregate metrics
        elapsed = time.time() - start_time
        successes = [r for r in results if r.get("success")]
        failures = [r for r in results if not r.get("success")]

        latencies_ms = [float(r["latency_ms"]) for r in successes]
        latencies_ms.sort()

        def _pctl(values: list[float], q: float) -> float:
            if not values:
                return 0.0
            idx = min(len(values) - 1, max(0, int(len(values) * q)))
            return values[idx]

        p50 = _pctl(latencies_ms, 0.50)
        p95 = _pctl(latencies_ms, 0.95)
        p99 = _pctl(latencies_ms, 0.99)
        avg_lat_ms = statistics.mean(latencies_ms) if latencies_ms else 0.0
        min_lat_ms = min(latencies_ms) if latencies_ms else 0.0
        max_lat_ms = max(latencies_ms) if latencies_ms else 0.0

        # Completion tokens
        completion_tokens: list[int] = []
        for r in successes:
            usage = r.get("usage") or {}
            ct = usage.get("completion_tokens")
            if isinstance(ct, int) and ct >= 0:
                completion_tokens.append(ct)
            else:
                # Fallback to rough estimate from text length
                text = r.get("content", "")
                completion_tokens.append(_approx_tokens_from_text(text))

        avg_completion_tokens = int(statistics.mean(completion_tokens)) if completion_tokens else 0

        # Throughput estimates
        mean_latency_seconds = avg_lat_ms / 1000.0 if avg_lat_ms > 0 else 0.0
        rps_est = (concurrent / mean_latency_seconds) if mean_latency_seconds > 0 else 0.0
        reqs_per_day = rps_est * 86400.0
        tokens_per_day = reqs_per_day * float(avg_completion_tokens)

        # Error summary
        error_summary: dict[str, int] = {}
        for r in failures:
            status = r.get("status", 0)
            key = f"Status {status}"
            error_summary[key] = error_summary.get(key, 0) + 1

        return {
            "duration_seconds": elapsed,
            "total_requests": len(results),
            "successful_requests": len(successes),
            "failed_requests": len(failures),
            "success_rate": (len(successes) / max(1, len(results))) * 100.0,
            "latency_ms": {
                "min": min_lat_ms,
                "max": max_lat_ms,
                "avg": avg_lat_ms,
                "p50": p50,
                "p95": p95,
                "p99": p99,
            },
            "avg_completion_tokens": avg_completion_tokens,
            "concurrency": concurrent,
            "rps_estimate": rps_est,
            "requests_per_day": reqs_per_day,
            "tokens_per_day": tokens_per_day,
            "error_summary": error_summary,
        }


def _print_results(model: str, results: dict[str, Any]) -> None:
    """Print a human-readable summary of results."""
    print("\n=== Throughput Estimation Results ===")
    print(f"Model: {model}")
    print(f"Duration: {results['duration_seconds']:.1f}s")
    print(f"Total Requests: {results['total_requests']}")
    print(f"Success: {results['successful_requests']} ({results['success_rate']:.1f}%)")
    print(f"Failures: {results['failed_requests']}")

    lat = results["latency_ms"]
    print("Latency (ms):")
    print(f"  P50: {lat['p50']:.1f}")
    print(f"  P95: {lat['p95']:.1f}")
    print(f"  P99: {lat['p99']:.1f}")
    print(f"  Min: {lat['min']:.1f}")
    print(f"  Max: {lat['max']:.1f}")
    print(f"  Avg: {lat['avg']:.1f}")

    print(f"Average completion tokens: {results['avg_completion_tokens']}")
    print(f"Concurrency: {results['concurrency']}")
    print(f"Estimated RPS: {results['rps_estimate']:.3f}")
    print(f"Estimated requests per day: {results['requests_per_day']:.0f}")
    print(f"Estimated tokens per day: {results['tokens_per_day']:.0f}")

    if results.get("error_summary"):
        print("\nError Summary:")
        for k, v in sorted(results["error_summary"].items(), key=lambda x: -x[1]):
            print(f"  {k}: {v}")


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Estimate daily throughput for OpenAI-compatible endpoints"
    )
    parser.add_argument(
        "--base-url",
        default="http://localhost:8080/v1",
        help="API base URL (should include '/v1')",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="Optional API key for Authorization: Bearer",
    )
    parser.add_argument(
        "--model",
        default="glm-4.6-featherless",
        help="Model identifier (e.g., 'zai-org/GLM-4.6' or local alias)",
    )
    parser.add_argument(
        "--prompt",
        default="Please answer briefly about your capabilities.",
        help="Prompt to send",
    )
    parser.add_argument(
        "--concurrent",
        type=int,
        default=2,
        help="Number of concurrent workers (default: 2)",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=120.0,
        help="Test duration in seconds (default: 120)",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.7,
        help="Sampling temperature (default: 0.7)",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=256,
        help="Max completion tokens (default: 256)",
    )
    parser.add_argument(
        "--warmup",
        type=float,
        default=10.0,
        help="Warmup duration in seconds to avoid cold start (default: 10)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Verbose logging of failures",
    )
    return parser.parse_args()


async def main() -> None:
    """Entrypoint for the estimator."""
    args = parse_args()
    print("=== Throughput Estimator ===")
    print(f"Base URL: {args.base_url}")
    print(f"Model: {args.model}")
    print(f"Concurrent: {args.concurrent}")
    print(f"Duration: {args.duration}s")
    print(f"Warmup: {args.warmup}s")

    results = await run_test(
        base_url=args.base_url,
        model=args.model,
        prompt=args.prompt,
        api_key=args.api_key,
        concurrent=args.concurrent,
        duration=args.duration,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
        verbose=args.verbose,
        warmup_duration=args.warmup,
    )

    _print_results(args.model, results)


if __name__ == "__main__":
    asyncio.run(main())
