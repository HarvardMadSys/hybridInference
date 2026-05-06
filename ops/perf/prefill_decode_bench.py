#!/usr/bin/env python3
"""Prefill/decode throughput benchmark for vLLM and SGLang.

Uses non-streaming requests to isolate prefill from decode:
  - Prefill latency: measured with max_tokens=1 (prefill + 1 decode step)
  - Decode throughput: (output_tokens - 1) / (total_latency - prefill_latency)
  - Prefill throughput: prompt_tokens / prefill_latency

Usage:
  # Benchmark qwen3.6-35b on sglang
  python ops/perf/prefill_decode_bench.py \\
    --base-url http://localhost:8001/v1 \\
    --model Qwen/Qwen3.6-35B-A3B-FP8 \\
    --sweep

  # Benchmark glm-4.7-flash
  python ops/perf/prefill_decode_bench.py \\
    --base-url http://localhost:8001/v1 \\
    --model zai-org/GLM-4.7-Flash \\
    --sweep

  # Single config
  python ops/perf/prefill_decode_bench.py \\
    --base-url http://localhost:8001/v1 \\
    --model Qwen/Qwen3.6-35B-A3B-FP8 \\
    --input-tokens 1024 --output-tokens 256
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from dataclasses import dataclass
from typing import Any

import aiohttp


@dataclass
class BenchResult:
    request_id: str
    model: str
    target_input_tokens: int
    target_output_tokens: int

    total_latency_ms: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    success: bool = True
    error: str | None = None


@dataclass
class PrefillDecodeMetrics:
    label: str
    prompt_tokens: float = 0.0
    output_tokens: float = 0.0
    prefill_ms: float = 0.0
    prefill_throughput: float = 0.0
    decode_ms: float = 0.0
    decode_throughput: float = 0.0
    total_ms: float = 0.0


def generate_prompt(target_tokens: int) -> str:
    word_count = max(1, int(target_tokens * 0.75))
    base = (
        "The history of artificial intelligence spans several decades, "
        "beginning with the foundational work of Alan Turing and John McCarthy "
        "in the 1950s. Over the years, the field has evolved from rule-based "
        "systems to statistical learning and deep neural networks. "
    )
    repeat_count = max(1, word_count // len(base.split()))
    prompt = " ".join([base] * repeat_count)
    words = prompt.split()
    if len(words) > word_count:
        words = words[:word_count]
    while len(words) < word_count:
        words.append("artificial intelligence machine learning deep learning")
    return " ".join(words)[: word_count * 6]


async def non_stream_request(
    session: aiohttp.ClientSession,
    base_url: str,
    model: str,
    prompt: str,
    max_tokens: int,
    request_id: str,
) -> BenchResult:
    url = f"{base_url}/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.0,
        "stream": False,
    }
    headers = {"Content-Type": "application/json", "X-Request-ID": request_id}

    result = BenchResult(
        request_id=request_id,
        model=model,
        target_input_tokens=len(prompt.split()),
        target_output_tokens=max_tokens,
    )

    start = time.monotonic()
    try:
        async with session.post(url, json=payload, headers=headers) as resp:
            latency = (time.monotonic() - start) * 1000.0
            result.total_latency_ms = latency

            if resp.status != 200:
                body = await resp.text()
                result.success = False
                result.error = f"HTTP {resp.status}: {body[:300]}"
                return result

            data = await resp.json()
            usage = data.get("usage", {})
            result.prompt_tokens = usage.get("prompt_tokens", 0) or 0
            result.completion_tokens = usage.get("completion_tokens", 0) or 0

    except Exception as exc:
        result.total_latency_ms = (time.monotonic() - start) * 1000.0
        result.success = False
        result.error = str(exc)

    return result


async def run_prefill_decode_benchmark(
    session: aiohttp.ClientSession,
    base_url: str,
    model: str,
    prompt: str,
    output_tokens: int,
    num_requests: int,
    concurrent: int,
    warmup: int = 1,
) -> PrefillDecodeMetrics:
    sem = asyncio.Semaphore(concurrent)
    label = f"in={len(prompt.split())} out={output_tokens} conc={concurrent}"

    async def bounded_prefill(idx: int) -> BenchResult:
        async with sem:
            return await non_stream_request(session, base_url, model, prompt, 1, f"prefill_{idx}")

    async def bounded_full(idx: int) -> BenchResult:
        async with sem:
            return await non_stream_request(
                session, base_url, model, prompt, output_tokens, f"full_{idx}"
            )

    if warmup > 0:
        _ = await asyncio.gather(
            *[bounded_prefill(i) for i in range(warmup)],
            *[bounded_full(i) for i in range(warmup)],
        )

    prefill_results = await asyncio.gather(*[bounded_prefill(i) for i in range(num_requests)])
    full_results = await asyncio.gather(*[bounded_full(i) for i in range(num_requests)])

    prefill_ok = [r for r in prefill_results if r.success and r.total_latency_ms > 0]
    full_ok = [r for r in full_results if r.success and r.total_latency_ms > 0]

    if not prefill_ok or not full_ok:
        return PrefillDecodeMetrics(label=label)

    avg_prefill_ms = statistics.mean([r.total_latency_ms for r in prefill_ok])
    avg_full_ms = statistics.mean([r.total_latency_ms for r in full_ok])
    avg_prompt_tokens = statistics.mean([r.prompt_tokens for r in full_ok])
    avg_completion_tokens = statistics.mean([r.completion_tokens for r in full_ok])

    decode_time_ms = avg_full_ms - avg_prefill_ms
    if decode_time_ms < 0:
        decode_time_ms = 0.1

    prefill_tps = avg_prompt_tokens / (avg_prefill_ms / 1000.0) if avg_prefill_ms > 0 else 0
    decode_tps = (
        (avg_completion_tokens - 1) / (decode_time_ms / 1000.0) if decode_time_ms > 0 else 0
    )

    return PrefillDecodeMetrics(
        label=label,
        prompt_tokens=avg_prompt_tokens,
        output_tokens=avg_completion_tokens,
        prefill_ms=avg_prefill_ms,
        prefill_throughput=prefill_tps,
        decode_ms=decode_time_ms,
        decode_throughput=decode_tps,
        total_ms=avg_full_ms,
    )


INPUT_TOKEN_SWEEP = [128, 512, 1024, 2048, 4096]
OUTPUT_TOKEN_SWEEP = [64, 128, 256, 512]


async def run_sweep(
    base_url: str,
    model: str,
    num_requests: int,
    concurrent: int,
    output_file: str | None = None,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    connector = aiohttp.TCPConnector(limit=concurrent + 2)
    timeout = aiohttp.ClientTimeout(total=None, connect=30, sock_read=300)

    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        for in_tok in INPUT_TOKEN_SWEEP:
            prompt = generate_prompt(in_tok)
            for out_tok in OUTPUT_TOKEN_SWEEP:
                label = f"{model} | in={in_tok} out={out_tok} conc={concurrent}"
                print(f"\n>>> {label}")
                m = await run_prefill_decode_benchmark(
                    session=session,
                    base_url=base_url,
                    model=model,
                    prompt=prompt,
                    output_tokens=out_tok,
                    num_requests=num_requests,
                    concurrent=concurrent,
                )
                print(f"  Prompt tokens:    {m.prompt_tokens:.0f}")
                print(f"  Output tokens:    {m.output_tokens:.0f}")
                print(f"  Prefill latency:  {m.prefill_ms:.1f} ms")
                print(f"  Prefill throughput: {m.prefill_throughput:.0f} tok/s")
                print(f"  Decode latency:   {m.decode_ms:.1f} ms")
                print(f"  Decode throughput:  {m.decode_throughput:.1f} tok/s")
                print(f"  Total latency:    {m.total_ms:.1f} ms")

                results.append(
                    {
                        "label": label,
                        "model": model,
                        "input_tokens_target": in_tok,
                        "output_tokens_target": out_tok,
                        "concurrency": concurrent,
                        "prompt_tokens_avg": m.prompt_tokens,
                        "output_tokens_avg": m.output_tokens,
                        "prefill_ms": m.prefill_ms,
                        "prefill_throughput_tok_s": m.prefill_throughput,
                        "decode_ms": m.decode_ms,
                        "decode_throughput_tok_s": m.decode_throughput,
                        "total_latency_ms": m.total_ms,
                    }
                )

    if output_file:
        with open(output_file, "w") as f:
            json.dump(results, f, indent=2)
        print(f"\nResults saved to {output_file}")

    return results


def print_comparison_table(results: list[dict[str, Any]]) -> None:
    print(f"\n{'=' * 110}")
    print("  COMPARISON TABLE")
    print(f"{'=' * 110}")
    header = (
        f"{'Config':<45} | {'Prefill ms':>10} | {'Prefill t/s':>12} | "
        f"{'Decode t/s':>12} | {'Decode ms':>10} | {'Total ms':>10}"
    )
    print(header)
    print("-" * len(header))
    for r in results:
        label = r["label"]
        if len(label) > 44:
            label = label[:41] + "..."
        print(
            f"{label:<45} | "
            f"{r['prefill_ms']:>9.1f}ms | "
            f"{r['prefill_throughput_tok_s']:>10.0f}t/s | "
            f"{r['decode_throughput_tok_s']:>10.1f}t/s | "
            f"{r['decode_ms']:>9.1f}ms | "
            f"{r['total_latency_ms']:>9.1f}ms"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prefill/decode throughput benchmark for vLLM and SGLang"
    )
    parser.add_argument(
        "--base-url",
        default="http://localhost:8001/v1",
        help="API base URL (e.g. http://localhost:8001/v1)",
    )
    parser.add_argument(
        "--model",
        default="Qwen/Qwen3.6-35B-A3B-FP8",
        help="Model identifier",
    )
    parser.add_argument(
        "--sweep",
        action="store_true",
        help="Run full sweep of input/output token combinations",
    )
    parser.add_argument(
        "--input-tokens",
        type=int,
        default=1024,
        help="Approximate input prompt tokens (single config mode)",
    )
    parser.add_argument(
        "--output-tokens",
        type=int,
        default=256,
        help="Max output tokens (single config mode)",
    )
    parser.add_argument(
        "--num-requests",
        type=int,
        default=5,
        help="Number of requests per configuration (default: 5)",
    )
    parser.add_argument(
        "--concurrent",
        type=int,
        default=1,
        help="Concurrency level",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output JSON file for results",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()

    print("=" * 60)
    print("  Prefill/Decode Throughput Benchmark")
    print("=" * 60)
    print(f"  Base URL: {args.base_url}")
    print(f"  Model:    {args.model}")

    if args.sweep:
        print(f"  Mode:     Sweep ({len(INPUT_TOKEN_SWEEP)}x{len(OUTPUT_TOKEN_SWEEP)} configs)")
        results = await run_sweep(
            base_url=args.base_url,
            model=args.model,
            num_requests=args.num_requests,
            concurrent=args.concurrent,
            output_file=args.output,
        )
        print_comparison_table(results)
    else:
        print("  Mode:     Single config")
        print(f"  Input:    ~{args.input_tokens} tokens")
        print(f"  Output:   {args.output_tokens} tokens")
        print(f"  Requests: {args.num_requests}")
        print(f"  Concurrency: {args.concurrent}")

        prompt = generate_prompt(args.input_tokens)
        connector = aiohttp.TCPConnector(limit=args.concurrent + 2)
        timeout = aiohttp.ClientTimeout(total=None, connect=30, sock_read=300)

        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            m = await run_prefill_decode_benchmark(
                session=session,
                base_url=args.base_url,
                model=args.model,
                prompt=prompt,
                output_tokens=args.output_tokens,
                num_requests=args.num_requests,
                concurrent=args.concurrent,
                warmup=2,
            )
            print(f"\n  Prompt tokens:    {m.prompt_tokens:.0f}")
            print(f"  Output tokens:    {m.output_tokens:.0f}")
            print(f"  Prefill latency:  {m.prefill_ms:.1f} ms")
            print(f"  Prefill throughput: {m.prefill_throughput:.0f} tok/s")
            print(f"  Decode latency:   {m.decode_ms:.1f} ms")
            print(f"  Decode throughput:  {m.decode_throughput:.1f} tok/s")
            print(f"  Total latency:    {m.total_ms:.1f} ms")

            if args.output:
                with open(args.output, "w") as f:
                    json.dump(
                        {
                            "label": f"{args.model} | in={args.input_tokens} out={args.output_tokens}",
                            "prompt_tokens_avg": m.prompt_tokens,
                            "output_tokens_avg": m.output_tokens,
                            "prefill_ms": m.prefill_ms,
                            "prefill_throughput_tok_s": m.prefill_throughput,
                            "decode_ms": m.decode_ms,
                            "decode_throughput_tok_s": m.decode_throughput,
                            "total_latency_ms": m.total_ms,
                        },
                        f,
                        indent=2,
                    )


if __name__ == "__main__":
    asyncio.run(main())
