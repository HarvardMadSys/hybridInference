"""Drive the MiniMax-M2.5 OpenRouter benchmark pipeline.

Usage:
    python -m openrouter_benchmark --api-key <key>
    python -m openrouter_benchmark --skip-plot
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from openrouter_benchmark import aggregate, benchmark as bm, config, plot


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MiniMax-M2.5 OpenRouter benchmark")
    parser.add_argument("--api-key", required=False)
    parser.add_argument("--providers", nargs="+", default=config.PROVIDERS)
    parser.add_argument("--input-lens", type=int, nargs="+", default=list(config.INPUT_LENS))
    parser.add_argument("--concurrencies", type=int, nargs="+", default=list(config.CONCURRENCIES))
    parser.add_argument("--skip-plot", action="store_true")
    args = parser.parse_args(argv)

    api_key = args.api_key or os.getenv(config.OPENROUTER_API_KEY_ENV)
    if not api_key:
        print("OPENROUTER_API_KEY env var or --api-key required")
        return 1

    print("=" * 60)
    print("MiniMax-M2.5 OpenRouter Benchmark")
    print("=" * 60)

    config.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    raw_csv = config.RESULTS_DIR / "raw.csv"
    agg_csv = config.SUMMARY_CSV

    print("\n[1/3] Running benchmark...")
    rows = asyncio.run(bm._run_all_providers(api_key))
    bm._write_csv(rows, raw_csv)
    print(f"Raw CSV: {raw_csv}")

    print("\n[2/3] Aggregating...")
    aggregate.aggregate(raw_csv, agg_csv)

    if not args.skip_plot:
        print("\n[3/3] Plotting...")
        config.PLOTS_DIR.mkdir(parents=True, exist_ok=True)
        plot._apply_style()
        plot._ttft_heatmap(agg_csv, config.PLOTS_DIR / "ttft_heatmap.png")
        plot._ttft_by_concurrency(agg_csv, config.PLOTS_DIR / "ttft_by_concurrency.png")
        plot._throughput_bar(agg_csv, config.PLOTS_DIR / "throughput_bar.png")
        print(f"Plots: {config.PLOTS_DIR}/")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
