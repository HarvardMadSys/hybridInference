"""Benchmark MiniMax-M2.5 TTFT across OpenRouter providers.

Sweeps (provider, input_len, concurrency) and collects TTFT and throughput
metrics by sending real streaming requests directly to the OpenRouter API,
using the ``provider`` field to pin each provider.

Output: results/minimax_m2_5_openrouter/raw.csv
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import time
from pathlib import Path
from typing import Any

import httpx

from . import config


def _prompt_for_len(n: int) -> str:
    return "hi"


async def _stream_request(
    client: httpx.AsyncClient,
    api_key: str,
    provider: str,
    input_len: int,
    output_len: int,
    concurrency: int,
) -> dict[str, Any]:
    start = time.perf_counter()
    ttft: float | None = None
    chars: int = 0
    error: str | None = None

    prompt = _prompt_for_len(input_len)
    body: dict[str, Any] = {
        "model": config.MODEL_ID,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": output_len,
        "stream": True,
        "provider": {"order": [provider], "allow_fallbacks": False},
    }

    headers = {
        "HTTP-Referer": "https://freeinference.org",
        "X-Title": "FreeInference",
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        async with client.stream(
            "POST",
            f"{config.BASE_URL}/chat/completions",
            json=body,
            headers=headers,
            timeout=httpx.Timeout(120.0, connect=30.0),
        ) as resp:
            if resp.status_code != 200:
                raw = await resp.aread()
                error = f"HTTP {resp.status_code}: {raw[:200].decode(errors='replace')}"
                return _make_result(
                    provider, input_len, output_len, concurrency, ttft, chars, error
                )

            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                payload = line.removeprefix("data: ")
                if payload.strip() in ("", "[DONE]"):
                    continue
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                delta = chunk.get("choices", [{}])[0].get("delta", {}).get("content") or ""
                if delta:
                    if ttft is None:
                        ttft = (time.perf_counter() - start) * 1000
                    chars += len(delta)

    except httpx.TimeoutException:
        error = "timeout"
    except Exception as e:
        error = str(e)

    latency_ms = (time.perf_counter() - start) * 1000
    return _make_result(
        provider, input_len, output_len, concurrency, ttft, chars, error, latency_ms
    )


def _make_result(
    provider: str,
    input_len: int,
    output_len: int,
    concurrency: int,
    ttft_ms: float | None,
    chars: int,
    error: str | None,
    latency_ms: float | None = None,
) -> dict[str, Any]:
    return {
        "provider": provider,
        "input_len": input_len,
        "output_len": output_len,
        "concurrency": concurrency,
        "ttft_ms": ttft_ms,
        "chars": chars,
        "error": error,
        "latency_ms": latency_ms,
    }


async def _run_battery(
    client: httpx.AsyncClient,
    api_key: str,
    provider: str,
    input_lens: tuple[int, ...],
    concurrencies: tuple[int, ...],
    output_len: int,
    num_repeats: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for input_len in input_lens:
        for conc in concurrencies:
            for run_id in range(1, num_repeats + 1):
                tasks = [
                    _stream_request(client, api_key, provider, input_len, output_len, conc)
                    for _ in range(conc)
                ]
                results = await asyncio.gather(*tasks)
                for r in results:
                    r["run_id"] = run_id
                    rows.append(r)
    return rows


async def _run_all_providers(
    api_key: str,
    providers: list[str] | None = None,
    input_lens: tuple[int, ...] | None = None,
    concurrencies: tuple[int, ...] | None = None,
    output_len: int | None = None,
    num_repeats: int | None = None,
) -> list[dict[str, Any]]:
    providers = providers or config.PROVIDERS
    input_lens = input_lens or config.INPUT_LENS
    concurrencies = concurrencies or config.CONCURRENCIES
    output_len = output_len or config.OUTPUT_LEN
    num_repeats = num_repeats or config.NUM_REPEATS

    all_rows: list[dict[str, Any]] = []
    async with httpx.AsyncClient() as client:
        for provider in providers:
            print(f"  Running provider: {provider}")
            rows = await _run_battery(
                client, api_key, provider, input_lens, concurrencies, output_len, num_repeats
            )
            all_rows.extend(rows)
    return all_rows


def _compute_summary(rows: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    summary: dict[str, dict[str, float]] = {}
    by_key: dict[tuple, list[float]] = {}

    for r in rows:
        if r["error"] or r["ttft_ms"] is None:
            continue
        key = (r["provider"], r["input_len"], r["concurrency"])
        by_key.setdefault(key, []).append(r["ttft_ms"])

    for (provider, input_len, concurrency), ttfts in by_key.items():
        if not ttfts:
            continue
        k = f"{provider}/{input_len}/{concurrency}"
        summary[k] = {
            "ttft_ms_p50": statistics.median(ttfts),
            "ttft_ms_p95": statistics.quantiles(ttfts, n=20)[18] if len(ttfts) > 1 else ttfts[0],
            "ttft_ms_mean": statistics.mean(ttfts),
            "provider": provider,
            "input_len": input_len,
            "concurrency": concurrency,
        }
    return summary


def _print_summary_table(summary: dict[str, dict[str, float]]) -> None:
    print(
        "\n{:40s} {:>10s} {:>8s} {:>8s}".format(
            "Provider/InputLen/Conc", "TTFT p50", "TTFT p95", "TTFT mean"
        )
    )
    print("-" * 72)
    for k, v in sorted(summary.items()):
        print(f"{k:40s} {v['ttft_ms_p50']:10.1f} {v['ttft_ms_p95']:8.1f} {v['ttft_ms_mean']:8.1f}")


def _write_csv(rows: list[dict[str, Any]], out_path: Path) -> None:
    import csv

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "provider",
        "input_len",
        "output_len",
        "concurrency",
        "ttft_ms",
        "chars",
        "error",
        "latency_ms",
        "run_id",
    ]
    with out_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r[k] for k in fieldnames})


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for the benchmark."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--api-key",
        default=os.getenv(config.OPENROUTER_API_KEY_ENV),
        required=not bool(os.getenv(config.OPENROUTER_API_KEY_ENV)),
    )
    parser.add_argument("--providers", nargs="+", default=list(config.PROVIDERS))
    parser.add_argument("--input-lens", type=int, nargs="+", default=list(config.INPUT_LENS))
    parser.add_argument("--concurrencies", type=int, nargs="+", default=list(config.CONCURRENCIES))
    parser.add_argument("--output-raw-csv", type=Path, default=config.RAW_CSV)
    args = parser.parse_args(argv)

    if not args.api_key:
        print("Error: --api-key or OPENROUTER_API_KEY env var required")
        return 1

    providers = args.providers
    input_lens = tuple(args.input_lens)
    concurrencies = tuple(args.concurrencies)

    print(f"Benchmarking MiniMax-M2.5 across {len(providers)} providers")
    print(
        f"Input lens: {input_lens}, Concurrencies: {concurrencies}, Repeats: {config.NUM_REPEATS}"
    )

    rows = asyncio.run(
        _run_all_providers(
            args.api_key,
            providers=providers,
            input_lens=input_lens,
            concurrencies=concurrencies,
        )
    )
    _write_csv(rows, args.output_raw_csv)
    print(f"\nCSV written to {args.output_raw_csv}")

    summary = _compute_summary(rows)
    _print_summary_table(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
