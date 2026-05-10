"""
benchmark.plot
==============

Generate the four comparison plots from the tidy summary CSV. Applies the
user's global plotting defaults (gridlines on, large fonts, distribution
plots show both median and mean, x-tick rotation 90°).

Plots produced under `out_dir`:
    prefill_throughput_vs_input_len.png
    decode_throughput_vs_concurrency.png
    ttft_cdf.png
    tpot_violin.png

Usage:
    generate_all_plots(Path("results/summary.csv"), Path("results/plots"))
"""
from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


def _apply_global_style() -> None:
    """User's global plotting defaults."""
    plt.rcParams.update({
        "axes.grid": True,
        "axes.titlesize": 16,
        "axes.labelsize": 16,
        "xtick.labelsize": 12,
        "ytick.labelsize": 12,
        "legend.fontsize": 12,
        "figure.figsize": (10, 6),
        "savefig.bbox": "tight",
        "savefig.dpi": 120,
    })


_ENGINE_COLORS = {"vllm": "#1f77b4", "sglang": "#ff7f0e", "trtllm": "#2ca02c"}


def _plot_prefill(df: pd.DataFrame, out_path: Path) -> None:
    """Plot prefill throughput (input tokens/sec) vs input length per engine.

    Uses the derived `prefill_tps_input_p50` metric (input_len / TTFT_p50)
    rather than `throughput_tps`, which is output-tokens/sec and collapses
    to request rate when output=1.
    """
    sub = df[(df["phase"] == "prefill") & (df["metric"] == "prefill_tps_input_p50")]
    if sub.empty:
        return
    fig, ax = plt.subplots()
    for engine, g in sub.groupby("engine"):
        med = g.groupby("input_len")["value"].median()
        ax.plot(med.index, med.values, marker="o",
                label=engine, color=_ENGINE_COLORS.get(engine))
    ax.set_xscale("log")
    ax.set_xlabel("Input length (tokens)")
    ax.set_ylabel("Prefill throughput (input tokens/sec)")
    ax.set_title("Prefill throughput vs input length (input_len / TTFT_p50)")
    ax.legend()
    plt.setp(ax.get_xticklabels(), rotation=90)
    fig.savefig(out_path)
    plt.close(fig)


def _plot_decode(df: pd.DataFrame, out_path: Path) -> None:
    """Plot aggregate decode throughput (tokens/sec) vs concurrency per engine.

    Shows median across runs for each (engine, concurrency) pair.
    X-axis is log-scaled; x-tick labels rotated 90°.
    """
    sub = df[(df["phase"] == "decode") & (df["metric"] == "throughput_tps")]
    if sub.empty:
        return
    fig, ax = plt.subplots()
    for engine, g in sub.groupby("engine"):
        med = g.groupby("concurrency")["value"].median()
        ax.plot(med.index, med.values, marker="o",
                label=engine, color=_ENGINE_COLORS.get(engine))
    ax.set_xscale("log")
    ax.set_xlabel("Concurrency")
    ax.set_ylabel("Aggregate decode throughput (tokens/sec)")
    ax.set_title("Decode throughput vs concurrency")
    ax.legend()
    plt.setp(ax.get_xticklabels(), rotation=90)
    fig.savefig(out_path)
    plt.close(fig)


def _plot_ttft_cdf(df: pd.DataFrame, out_path: Path) -> None:
    """Plot CDF of TTFT (ms, p50) at concurrency=16 per engine.

    Shows both median (dashed) and mean (dotted) vertical lines per engine,
    per the global plotting defaults for distribution plots.
    """
    sub = df[(df["phase"] == "decode") & (df["metric"] == "ttft_ms_p50")
             & (df["concurrency"] == 16)]
    if sub.empty:
        return
    fig, ax = plt.subplots()
    for engine, g in sub.groupby("engine"):
        vals = np.sort(g["value"].values)
        if len(vals) == 0:
            continue
        cdf_y = np.arange(1, len(vals) + 1) / len(vals)
        ax.plot(vals, cdf_y, label=engine, color=_ENGINE_COLORS.get(engine))
        ax.axvline(np.median(vals), color=_ENGINE_COLORS.get(engine),
                   linestyle="--", alpha=0.5)
        ax.axvline(np.mean(vals), color=_ENGINE_COLORS.get(engine),
                   linestyle=":", alpha=0.7)
    ax.set_xlabel("TTFT (ms)")
    ax.set_ylabel("CDF")
    ax.set_title("TTFT CDF at concurrency=16 (dashed=median, dotted=mean)")
    ax.legend()
    plt.setp(ax.get_xticklabels(), rotation=90)
    fig.savefig(out_path)
    plt.close(fig)


def _plot_tpot_violin(df: pd.DataFrame, out_path: Path) -> None:
    """Plot violin distribution of TPOT (ms, p50) at concurrency=16 per engine.

    Shows both median and mean per the global plotting defaults for
    distribution plots. X-tick labels rotated 90°.
    """
    sub = df[(df["phase"] == "decode") & (df["metric"] == "tpot_ms_p50")
             & (df["concurrency"] == 16)]
    if sub.empty:
        return
    fig, ax = plt.subplots()
    engines = sorted(sub["engine"].unique())
    data = [sub[sub["engine"] == e]["value"].values for e in engines]
    if not any(len(d) > 0 for d in data):
        return
    parts = ax.violinplot(data, showmedians=True, showmeans=True)
    for pc, e in zip(parts["bodies"], engines):
        pc.set_facecolor(_ENGINE_COLORS.get(e, "#888"))
        pc.set_alpha(0.6)
    ax.set_xticks(range(1, len(engines) + 1))
    ax.set_xticklabels(engines)
    ax.set_ylabel("TPOT (ms, p50)")
    ax.set_title("TPOT distribution at concurrency=16 (— median, -- mean)")
    plt.setp(ax.get_xticklabels(), rotation=90)
    fig.savefig(out_path)
    plt.close(fig)


def generate_all_plots(csv_path: Path, out_dir: Path) -> None:
    """Generate all four comparison plots from the tidy summary CSV.

    Args:
        csv_path: Path to the tidy summary CSV (output of benchmark.aggregate).
        out_dir:  Directory where PNG files will be written (created if absent).

    Produces:
        out_dir/prefill_throughput_vs_input_len.png
        out_dir/decode_throughput_vs_concurrency.png
        out_dir/ttft_cdf.png
        out_dir/tpot_violin.png
    """
    _apply_global_style()
    out_dir.mkdir(parents=True, exist_ok=True)
    df = pd.read_csv(csv_path)
    _plot_prefill(df, out_dir / "prefill_throughput_vs_input_len.png")
    _plot_decode(df, out_dir / "decode_throughput_vs_concurrency.png")
    _plot_ttft_cdf(df, out_dir / "ttft_cdf.png")
    _plot_tpot_violin(df, out_dir / "tpot_violin.png")
