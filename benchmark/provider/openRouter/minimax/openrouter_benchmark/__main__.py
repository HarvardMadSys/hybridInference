"""Drive the MiniMax-M2.5 OpenRouter benchmark pipeline.

Usage:
    python -c "
    import sys; sys.path.insert(0, 'benchmark/provider/openRouter/minimax')
    from openrouter_benchmark import __main__
    __main__.main()
    "
"""

from __future__ import annotations

import argparse
import asyncio
import os

from . import aggregate, benchmark as bm, config, plot


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MiniMax-M2.5 OpenRouter benchmark")
    parser.add_argument("--api-key", required=False)
    parser.add_argument("--providers", nargs="+", default=list(config.PROVIDERS))
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
    raw_csv = config.RAW_CSV
    agg_csv = config.SUMMARY_CSV

    providers = args.providers
    input_lens = tuple(args.input_lens)
    concurrencies = tuple(args.concurrencies)

    print("\n[1/3] Running benchmark...")
    rows = asyncio.run(
        bm._run_all_providers(
            api_key,
            providers=providers,
            input_lens=input_lens,
            concurrencies=concurrencies,
        )
    )
    bm._write_csv(rows, raw_csv)
    print(f"Raw CSV: {raw_csv}")

    print("\n[2/3] Aggregating...")
    aggregate.aggregate(raw_csv, agg_csv)

    if not args.skip_plot:
        print("\n[3/3] Plotting...")
        config.PLOTS_DIR.mkdir(parents=True, exist_ok=True)
        plot._apply_style()
        plot._ttft_bar(agg_csv, config.PLOTS_DIR / "ttft_bar.png")
        plot._ttft_by_input_len(agg_csv, config.PLOTS_DIR / "ttft_by_input_len.png", concurrency=16)
        plot._throughput_bar(agg_csv, config.PLOTS_DIR / "throughput_bar.png")
        print(f"Plots: {config.PLOTS_DIR}/")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
