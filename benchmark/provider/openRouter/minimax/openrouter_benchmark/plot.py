"""Plot MiniMax-M2.5 OpenRouter benchmark results.

Reads the aggregated CSV and produces TTFT bar charts and line plots.

Usage:
    python -c "
    import sys; sys.path.insert(0, 'benchmark/provider/openRouter/minimax')
    from openrouter_benchmark import plot
    plot.main(['results/minimax_m2_5_openrouter/summary.csv'])
    "
"""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd

from . import config

_INPUT_LEN_COLORS = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd", "#8c564b"]


def _apply_style() -> None:
    plt.rcParams.update(
        {
            "axes.grid": True,
            "axes.titlesize": 14,
            "axes.labelsize": 12,
            "xtick.labelsize": 10,
            "ytick.labelsize": 10,
            "legend.fontsize": 10,
            "figure.figsize": (10, 6),
            "savefig.bbox": "tight",
            "savefig.dpi": 120,
        }
    )


def _ttft_bar(csv_path: Path, out_path: Path) -> None:
    df = pd.read_csv(csv_path)
    pivot = df.pivot_table(index="provider", columns="input_len", values="ttft_ms_p50")
    fig, ax = plt.subplots()
    pivot.plot(kind="bar", ax=ax, color=_INPUT_LEN_COLORS[: len(pivot.columns)])
    ax.set_xlabel("Provider")
    ax.set_ylabel("TTFT p50 (ms)")
    ax.set_title("TTFT p50 by Provider and Input Length")
    ax.set_xticklabels(ax.get_xticklabels(), rotation=45, ha="right")
    ax.legend(title="Input Len")
    fig.savefig(out_path)
    plt.close(fig)


def _ttft_by_input_len(csv_path: Path, out_path: Path, concurrency: int = 16) -> None:
    df = pd.read_csv(csv_path)
    sub = df[df["concurrency"] == concurrency]
    if sub.empty:
        return
    fig, ax = plt.subplots()
    for provider, g in sub.groupby("provider"):
        g_sorted = g.sort_values("input_len")
        ax.plot(
            g_sorted["input_len"],
            g_sorted["ttft_ms_p50"],
            marker="o",
            label=provider,
            color=config.PROVIDER_COLORS.get(provider, "#888"),
        )
    ax.set_xscale("log")
    ax.set_xlabel("Input length (tokens)")
    ax.set_ylabel("TTFT p50 (ms)")
    ax.set_title(f"TTFT p50 at concurrency={concurrency} by Provider")
    ax.legend()
    fig.savefig(out_path)
    plt.close(fig)


def _throughput_bar(csv_path: Path, out_path: Path) -> None:
    df = pd.read_csv(csv_path)
    sub = df[df["concurrency"] == 1]
    if sub.empty:
        return
    pivot = sub.pivot_table(index="provider", columns="input_len", values="chars_per_sec_p50")
    fig, ax = plt.subplots()
    pivot.plot(kind="bar", ax=ax)
    ax.set_xlabel("Provider")
    ax.set_ylabel("Throughput (chars/sec)")
    ax.set_title("Decode throughput p50 at concurrency=1")
    ax.set_xticklabels(ax.get_xticklabels(), rotation=45, ha="right")
    ax.legend(title="Input Len")
    fig.savefig(out_path)
    plt.close(fig)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_csv", type=Path)
    parser.add_argument("--out-dir", type=Path, required=False)
    args = parser.parse_args(argv)

    csv_path = args.input_csv
    out_dir = args.out_dir or csv_path.parent / "plots"

    _apply_style()
    out_dir.mkdir(parents=True, exist_ok=True)

    _ttft_bar(csv_path, out_dir / "ttft_bar.png")
    _ttft_by_input_len(csv_path, out_dir / "ttft_by_input_len.png")
    _throughput_bar(csv_path, out_dir / "throughput_bar.png")

    print(f"Plots written to {out_dir}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
