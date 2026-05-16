"""Aggregate MiniMax-M2.5 OpenRouter benchmark results.

Reads the per-request CSV produced by benchmark.py and emits a tidy summary
CSV with p50/p95 TTFT per (provider, input_len, concurrency). The ``chars``
field from benchmark.py is character count (not token count); throughput
is reported as chars/sec and labelled accordingly.

Usage:
    python -m openrouter_benchmark.aggregate results/minimax_m2_5_openrouter/raw.csv
"""

from __future__ import annotations

import argparse
import csv
import statistics
from pathlib import Path
from typing import Any


def aggregate(input_csv: Path, output_csv: Path) -> None:
    by_key: dict[tuple, list[dict[str, Any]]] = {}

    with input_csv.open() as f:
        for row in csv.DictReader(f):
            if row["error"]:
                continue
            if not row["ttft_ms"]:
                continue
            key = (row["provider"], int(row["input_len"]), int(row["concurrency"]))
            by_key.setdefault(key, []).append(row)

    out_rows: list[dict[str, Any]] = []
    for (provider, input_len, concurrency), rows in sorted(by_key.items()):
        ttfts = [float(r["ttft_ms"]) for r in rows]
        chars_list = [int(r["chars"]) for r in rows]

        if len(ttfts) < 2:
            ttft_p50 = ttft_p95 = ttft_mean = ttfts[0] if ttfts else None
        else:
            sorted_ttfts = sorted(ttfts)
            ttft_p50 = statistics.median(sorted_ttfts)
            ttft_p95 = statistics.quantiles(sorted_ttfts, n=20)[18]
            ttft_mean = statistics.mean(sorted_ttfts)

        out_rows.append(
            {
                "provider": provider,
                "input_len": input_len,
                "concurrency": concurrency,
                "ttft_ms_p50": round(ttft_p50, 2) if ttft_p50 is not None else None,
                "ttft_ms_p95": round(ttft_p95, 2) if ttft_p95 is not None else None,
                "ttft_ms_mean": round(ttft_mean, 2) if ttft_mean is not None else None,
                "chars_per_sec_p50": round(statistics.median(chars_list) / (ttft_p50 / 1000), 2)
                if ttft_p50
                else None,
                "n": len(ttfts),
            }
        )

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "provider",
        "input_len",
        "concurrency",
        "ttft_ms_p50",
        "ttft_ms_p95",
        "ttft_ms_mean",
        "chars_per_sec_p50",
        "n",
    ]
    with output_csv.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(out_rows)

    print(f"Aggregated {len(out_rows)} rows -> {output_csv}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_csv", type=Path)
    parser.add_argument("--output-csv", type=Path, required=False)
    args = parser.parse_args(argv)

    if not args.input_csv.exists():
        print(f"Input CSV not found: {args.input_csv}")
        return 1

    output_csv = args.output_csv or args.input_csv.parent / "summary_agg.csv"
    aggregate(args.input_csv, output_csv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
