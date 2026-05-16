#!/usr/bin/env python3
"""Benchmark TTFT and decoding throughput for featherless models.

Usage:
    FEATHERLESS_API_KEY=your_key python -m apps.backend.benchmark.provider.featherless.benchmark

Or run directly:
    python apps/backend/benchmark/provider/featherless/benchmark.py
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
import time
from typing import Any

import requests


def measure_ttft_and_throughput(
    base_url: str,
    model: str,
    api_key: str,
    prompt: str,
    max_tokens: int,
    temperature: float = 0.7,
    verbose: bool = False,
) -> dict[str, Any]:
    """Measure TTFT and decoding throughput using streaming.

    Args:
        base_url: API base URL (e.g., https://api.featherless.ai/v1).
        model: Model identifier.
        api_key: API key for Authorization header.
        prompt: User prompt.
        max_tokens: Max tokens to generate.
        temperature: Sampling temperature.
        verbose: Print progress.

    Returns:
        Dict with ttft_sec, total_tokens, generation_time_sec, throughput_tps.
    """
    url = f"{base_url}/chat/completions"

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": True,
    }

    start_time = time.time()
    first_token_time: float | None = None
    tokens: list[str] = []
    usage: dict[str, Any] | None = None

    with requests.post(url, json=payload, headers=headers, stream=True, timeout=300) as resp:
        if resp.status_code != 200:
            error_text = resp.text[:500]
            raise RuntimeError(f"Request failed: {resp.status_code} - {error_text}")

        for line in resp.iter_lines():
            if not line:
                continue
            line_str = line.decode("utf-8")
            if not line_str.startswith("data: "):
                continue
            data_str = line_str[6:]
            if data_str.strip() == "[DONE]":
                break

            try:
                data = json.loads(data_str)
            except json.JSONDecodeError:
                continue

            choices = data.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta", {})
            content = delta.get("content")
            if content:
                if first_token_time is None:
                    first_token_time = time.time()
                tokens.append(content)

            if usage is None:
                usage = data.get("usage")

    total_time = time.time() - start_time

    if first_token_time is None:
        raise RuntimeError("No tokens received")

    ttft_sec = first_token_time - start_time
    generation_time_sec = total_time - ttft_sec
    content = "".join(tokens)

    if usage and usage.get("completion_tokens"):
        token_count = usage["completion_tokens"]
    else:
        token_count = max(1, len(content) // 2)

    throughput_tps = token_count / generation_time_sec if generation_time_sec > 0 else 0.0

    if verbose:
        print(
            f"  TTFT: {ttft_sec:.3f}s, tokens: {token_count}, gen_time: {generation_time_sec:.3f}s, throughput: {throughput_tps:.2f} tps"
        )

    return {
        "ttft_sec": ttft_sec,
        "completion_tokens": token_count,
        "generation_time_sec": generation_time_sec,
        "throughput_tps": throughput_tps,
    }


def run_benchmark(
    base_url: str,
    models: list[tuple[str, str]],
    api_key: str,
    prompt: str,
    max_tokens: int,
    runs: int = 3,
    verbose: bool = False,
) -> list[dict[str, Any]]:
    """Run benchmark for all models.

    Args:
        base_url: API base URL.
        models: List of (provider, model_name) tuples.
        api_key: API key.
        prompt: Prompt text.
        max_tokens: Max tokens to generate.
        runs: Number of runs per model.
        verbose: Print progress.

    Returns:
        List of result dicts.
    """
    results: list[dict[str, Any]] = []

    for provider, model in models:
        print(f"Benchmarking {provider}/{model}...")
        model_results: list[dict[str, Any]] = []

        # Warmup
        try:
            measure_ttft_and_throughput(
                base_url=base_url,
                model=model,
                api_key=api_key,
                prompt=prompt,
                max_tokens=max_tokens,
                verbose=False,
            )
        except Exception as e:
            print(f"  Warmup failed: {e}")

        # Actual runs
        for i in range(runs):
            try:
                r = measure_ttft_and_throughput(
                    base_url=base_url,
                    model=model,
                    api_key=api_key,
                    prompt=prompt,
                    max_tokens=max_tokens,
                    verbose=verbose,
                )
                model_results.append(r)
                time.sleep(1)  # Brief delay between runs
            except Exception as e:
                print(f"  Run {i + 1} failed: {e}")

        if model_results:
            avg_ttft = statistics.mean(r["ttft_sec"] for r in model_results)
            avg_tokens = int(statistics.mean(r["completion_tokens"] for r in model_results))
            avg_gen_time = statistics.mean(r["generation_time_sec"] for r in model_results)
            avg_throughput = statistics.mean(r["throughput_tps"] for r in model_results)

            results.append(
                {
                    "provider": provider,
                    "model": model,
                    "ttft_sec": avg_ttft,
                    "completion_tokens": avg_tokens,
                    "generation_time_sec": avg_gen_time,
                    "throughput_tps": avg_throughput,
                    "runs": len(model_results),
                }
            )
            print(f"  Avg: TTFT={avg_ttft:.3f}s, throughput={avg_throughput:.2f} tps")
        else:
            print(f"  No successful runs")

    return results


def print_summary(results: list[dict[str, Any]]) -> None:
    """Print a summary table."""
    print("\n" + "=" * 90)
    print("BENCHMARK RESULTS")
    print("=" * 90)
    print(
        f"{'Provider':<12} {'Model':<25} {'TTFT (s)':<10} {'Tokens':<8} {'Gen Time (s)':<14} {'Throughput (tps)':<16}"
    )
    print("-" * 90)

    for r in results:
        print(
            f"{r['provider']:<12} {r['model']:<25} {r['ttft_sec']:<10.3f} {r['completion_tokens']:<8} {r['generation_time_sec']:<14.3f} {r['throughput_tps']:<16.2f}"
        )

    print("=" * 90)

    # Group by provider for comparison
    providers = sorted(set(r["provider"] for r in results))
    print("\n--- Summary by Provider (avg throughput) ---")
    for provider in providers:
        provider_results = [r for r in results if r["provider"] == provider]
        if provider_results:
            avg_tp = statistics.mean(r["throughput_tps"] for r in provider_results)
            print(f"{provider}: {avg_tp:.2f} tps (avg across {len(provider_results)} models)")


def save_csv(results: list[dict[str, Any]], output_path: str) -> None:
    """Save results to CSV."""
    fieldnames = [
        "provider",
        "model",
        "ttft_sec",
        "completion_tokens",
        "generation_time_sec",
        "throughput_tps",
        "runs",
    ]

    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in results:
            writer.writerow(r)

    print(f"\nResults saved to {output_path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark TTFT and throughput on featherless")
    parser.add_argument(
        "--base-url",
        default="https://api.featherless.ai/v1",
        help="API base URL",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("FEATHERLESS_API_KEY"),
        help="API key (or FEATHERLESS_API_KEY env var)",
    )
    parser.add_argument(
        "--prompt",
        default="Write a long story",
        help="Prompt to send",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=2048,
        help="Max tokens to generate",
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=3,
        help="Number of runs per model",
    )
    parser.add_argument(
        "--output",
        default="benchmark_results.csv",
        help="Output CSV file",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Verbose output",
    )
    return parser.parse_args()


MODELS = [
    ("minimax", "MiniMax-M2.5"),
    ("minimax", "MiniMax-M2.7"),
    ("kimi", "kimi-k2.5"),
    ("kimi", "kimi-k2.6"),
    ("qwen", "qwen3.6-35B-A3B"),
    ("qwen", "qwen3.6-27B"),
    ("glm", "GLM-4.7"),
    ("glm", "GLM-5"),
    ("glm", "GLM-5.1"),
]


def main() -> None:
    args = parse_args()

    if not args.api_key:
        raise ValueError("API key required. Set FEATHERLESS_API_KEY or use --api-key")

    print("=== Featherless TTFT & Throughput Benchmark ===")
    print(f"Base URL: {args.base_url}")
    print(f"Prompt: {args.prompt}")
    print(f"Max tokens: {args.max_tokens}")
    print(f"Runs per model: {args.runs}")
    print()

    results = run_benchmark(
        base_url=args.base_url,
        models=MODELS,
        api_key=args.api_key,
        prompt=args.prompt,
        max_tokens=args.max_tokens,
        runs=args.runs,
        verbose=args.verbose,
    )

    print_summary(results)
    save_csv(results, args.output)


if __name__ == "__main__":
    main()
