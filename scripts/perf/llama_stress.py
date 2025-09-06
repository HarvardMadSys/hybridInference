#!/usr/bin/env python3
"""Lightweight Llama API stress tool (no other providers).

Usage:
  python scripts/perf/llama_stress.py \
    --base-url "$LLAMA_BASE_URL" \
    --api-key "$LLAMA_API_KEY" \
    --model llama-3.3-70b-instruct \
    --rps 5 --duration 30

Notes:
- Only calls Llama API (base_url + "/inference").
- Does not touch DeepSeek, Gemini, or local vLLM.
- Prints a concise summary at the end.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from typing import Any

from client.metrics import MetricsCollector
from client.runner import BenchmarkRunner
from serving.adapters.base import ModelConfig
from serving.adapters.llama import LlamaAdapter
from serving.base import LLMRequest, LLMResponse


async def _request_handler(
    adapter: LlamaAdapter, request: LLMRequest, request_id: str
) -> LLMResponse:
    """Send a single request to Llama API via adapter and adapt the response."""
    messages = [{"role": "user", "content": request.prompt}]
    resp = await adapter.chat_completion(
        messages,
        temperature=request.temperature,
        max_tokens=request.max_tokens,
    )
    text = resp["choices"][0]["message"].get("content", "")
    return LLMResponse(text=text, model=resp.get("model", request.model), provider="llama")


async def _request_generator(prompt: str, model: str) -> Any:
    while True:
        yield LLMRequest(prompt=prompt, model=model)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Llama API stress tool")
    p.add_argument("--base-url", default=os.getenv("LLAMA_BASE_URL"), help="Llama API base URL")
    p.add_argument("--api-key", default=os.getenv("LLAMA_API_KEY"), help="Llama API key")
    p.add_argument("--model", default="llama-3.3-70b-instruct", help="Model id")
    p.add_argument("--prompt", default="Hello, please respond briefly.", help="User prompt")
    p.add_argument("--rps", type=float, default=2.0, help="Requests per second")
    p.add_argument("--duration", type=float, default=30.0, help="Test duration in seconds")
    p.add_argument("--max-requests", type=int, default=None, help="Optional max requests")
    return p.parse_args()


async def main() -> None:
    args = _parse_args()
    if not args.base_url or not args.api_key:
        raise SystemExit("LLAMA_BASE_URL and LLAMA_API_KEY are required (via args or env)")

    # Configure Llama adapter only (no other providers)
    cfg = ModelConfig(
        id=args.model,
        name=args.model,
        provider="llama",
        base_url=str(args.base_url).rstrip("/"),
        api_key=args.api_key,
        supports_tools=False,
        supports_structured_output=False,
    )
    adapter = LlamaAdapter(cfg)

    runner = BenchmarkRunner(requests_per_second=args.rps)
    gen = _request_generator(prompt=args.prompt, model=args.model)

    async def handler(req: LLMRequest, req_id: str) -> LLMResponse:
        return await _request_handler(adapter, req, req_id)

    metrics: MetricsCollector = await runner.run(
        request_generator=gen,
        request_handler=handler,
        duration_limit=args.duration,
        max_requests=args.max_requests,
    )

    summary = metrics.get_summary()
    print("\n=== Llama Stress Summary ===")
    print(f"RPS target: {args.rps}, duration: {args.duration}s")
    print(f"Requests: {summary['total_requests']}")
    print(f"Success: {summary['success_count']}, Failures: {summary['failure_count']}")
    lat = summary.get("latency_ms", {})
    if lat:
        print(
            f"Latency ms: p50={lat.get('p50', 0):.1f}, p95={lat.get('p95', 0):.1f}, p99={lat.get('p99', 0):.1f}"
        )


if __name__ == "__main__":
    asyncio.run(main())
