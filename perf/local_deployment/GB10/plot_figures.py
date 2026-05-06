#!/usr/bin/env python3
"""Generate all benchmark figures for the GB10 local deployment report."""

from __future__ import annotations

import json
from pathlib import Path

import matplotlib
import matplotlib.pyplot as plt
import numpy as np

matplotlib.rcParams.update(
    {
        "font.family": "sans-serif",
        "axes.spines.top": False,
        "axes.spines.right": False,
        "axes.grid": True,
        "grid.alpha": 0.3,
        "grid.linestyle": "--",
    }
)

OUT = Path(__file__).parent
DATA = Path(__file__).parent.parent.parent.parent / "ops" / "perf"

COLORS = {
    "qwen_sglang": "#2563eb",
    "qwen_vllm":   "#7c3aed",
    "glm_vllm":    "#dc2626",
}
LABELS = {
    "qwen_sglang": "Qwen3.6-35B / SGLang",
    "qwen_vllm":   "Qwen3.6-35B / vLLM v0.20.1",
    "glm_vllm":    "GLM-4.7-Flash / vLLM v0.20.1",
}
MARKERS = {"qwen_sglang": "o", "qwen_vllm": "s", "glm_vllm": "^"}

INPUT_LENGTHS = [128, 512, 1024, 2048, 4096]
OUTPUT_LENGTHS = [64, 128, 256, 512]


def load(name: str) -> list[dict]:
    return json.loads((DATA / f"results_{name}.json").read_text())


def by_in_out(data: list[dict]) -> dict[tuple, dict]:
    return {(r["input_tokens_target"], r["output_tokens_target"]): r for r in data}


def avg_decode(data: list[dict], in_tok: int) -> float:
    rows = [r for r in data if r["input_tokens_target"] == in_tok]
    vals = [r["decode_throughput_tok_s"] for r in rows]
    return float(np.mean(vals)) if vals else 0.0


def avg_prefill(data: list[dict], out_tok: int = 256) -> dict[int, float]:
    return {
        in_tok: next(
            (r["prefill_throughput_tok_s"] for r in data
             if r["input_tokens_target"] == in_tok and r["output_tokens_target"] == out_tok),
            0.0,
        )
        for in_tok in INPUT_LENGTHS
    }


def prompt_tokens(data: list[dict], out_tok: int = 256) -> dict[int, float]:
    return {
        in_tok: next(
            (r["prompt_tokens_avg"] for r in data
             if r["input_tokens_target"] == in_tok and r["output_tokens_target"] == out_tok),
            in_tok,
        )
        for in_tok in INPUT_LENGTHS
    }


# ─── Figure 1: Decode throughput vs input length ───────────────────────────
def fig_decode_vs_input(datasets: dict[str, list[dict]]) -> None:
    fig, ax = plt.subplots(figsize=(9, 5))

    for key, data in datasets.items():
        xs = []
        ys = []
        for in_tok in INPUT_LENGTHS:
            pt = next((r["prompt_tokens_avg"] for r in data
                       if r["input_tokens_target"] == in_tok), in_tok)
            yd = avg_decode(data, in_tok)
            xs.append(pt)
            ys.append(yd)
        ax.plot(xs, ys, marker=MARKERS[key], color=COLORS[key],
                linewidth=2.2, markersize=8, label=LABELS[key])

    ax.set_xlabel("Prompt Tokens", fontsize=12)
    ax.set_ylabel("Decode Throughput (tok/s)", fontsize=12)
    ax.set_title("Decode Throughput vs Prompt Length\n(averaged over output lengths 64–512)", fontsize=13)
    ax.legend(fontsize=11)
    ax.set_ylim(bottom=0)
    fig.tight_layout()
    fig.savefig(OUT / "fig1_decode_throughput.png", dpi=150)
    plt.close(fig)
    print("Saved fig1_decode_throughput.png")


# ─── Figure 2: Prefill throughput vs input length ──────────────────────────
def fig_prefill_vs_input(datasets: dict[str, list[dict]]) -> None:
    fig, ax = plt.subplots(figsize=(9, 5))

    for key, data in datasets.items():
        pt_map = prompt_tokens(data)
        pf_map = avg_prefill(data)
        xs = [pt_map[i] for i in INPUT_LENGTHS]
        ys = [pf_map[i] for i in INPUT_LENGTHS]
        ax.plot(xs, ys, marker=MARKERS[key], color=COLORS[key],
                linewidth=2.2, markersize=8, label=LABELS[key])
        for x, y in zip(xs, ys):
            ax.annotate(f"{y/1000:.1f}k", (x, y),
                        textcoords="offset points", xytext=(0, 8),
                        ha="center", fontsize=8, color=COLORS[key])

    ax.set_xlabel("Prompt Tokens", fontsize=12)
    ax.set_ylabel("Prefill Throughput (tok/s)", fontsize=12)
    ax.set_title("Prefill Throughput vs Prompt Length\n(output=256 tokens)", fontsize=13)
    ax.legend(fontsize=11)
    ax.set_ylim(bottom=0)
    fig.tight_layout()
    fig.savefig(OUT / "fig2_prefill_throughput.png", dpi=150)
    plt.close(fig)
    print("Saved fig2_prefill_throughput.png")


# ─── Figure 3: Prefill latency vs input length ─────────────────────────────
def fig_prefill_latency(datasets: dict[str, list[dict]]) -> None:
    fig, ax = plt.subplots(figsize=(9, 5))

    for key, data in datasets.items():
        pt_map = prompt_tokens(data)
        xs = [pt_map[i] for i in INPUT_LENGTHS]
        ys = [
            next((r["prefill_ms"] for r in data
                  if r["input_tokens_target"] == i and r["output_tokens_target"] == 256), 0.0)
            for i in INPUT_LENGTHS
        ]
        ax.plot(xs, ys, marker=MARKERS[key], color=COLORS[key],
                linewidth=2.2, markersize=8, label=LABELS[key])

    ax.set_xlabel("Prompt Tokens", fontsize=12)
    ax.set_ylabel("Prefill Latency (ms)", fontsize=12)
    ax.set_title("Prefill Latency vs Prompt Length\n(output=256 tokens)", fontsize=13)
    ax.legend(fontsize=11)
    ax.set_ylim(bottom=0)
    fig.tight_layout()
    fig.savefig(OUT / "fig3_prefill_latency.png", dpi=150)
    plt.close(fig)
    print("Saved fig3_prefill_latency.png")


# ─── Figure 4: Decode throughput vs output length (in=1024) ─────────────────
def fig_decode_vs_output(datasets: dict[str, list[dict]]) -> None:
    fig, ax = plt.subplots(figsize=(9, 5))
    x = np.arange(len(OUTPUT_LENGTHS))
    width = 0.25
    offsets = [-width, 0, width]

    for (key, data), offset in zip(datasets.items(), offsets):
        ys = [
            next((r["decode_throughput_tok_s"] for r in data
                  if r["input_tokens_target"] == 1024 and r["output_tokens_target"] == o), 0.0)
            for o in OUTPUT_LENGTHS
        ]
        bars = ax.bar(x + offset, ys, width, label=LABELS[key],
                      color=COLORS[key], alpha=0.85)
        for bar in bars:
            h = bar.get_height()
            ax.annotate(f"{h:.0f}", (bar.get_x() + bar.get_width() / 2, h),
                        textcoords="offset points", xytext=(0, 3),
                        ha="center", fontsize=8)

    ax.set_xlabel("Output Tokens", fontsize=12)
    ax.set_ylabel("Decode Throughput (tok/s)", fontsize=12)
    ax.set_title("Decode Throughput vs Output Length\n(input ≈ 886 tokens)", fontsize=13)
    ax.set_xticks(x)
    ax.set_xticklabels([str(o) for o in OUTPUT_LENGTHS])
    ax.legend(fontsize=11)
    ax.set_ylim(bottom=0)
    fig.tight_layout()
    fig.savefig(OUT / "fig4_decode_vs_output.png", dpi=150)
    plt.close(fig)
    print("Saved fig4_decode_vs_output.png")


# ─── Figure 5: End-to-end latency heatmap (in=1024, all outputs) ────────────
def fig_e2e_latency_bars(datasets: dict[str, list[dict]]) -> None:
    fig, axes = plt.subplots(1, len(OUTPUT_LENGTHS), figsize=(14, 5), sharey=False)

    keys = list(datasets.keys())
    bar_colors = [COLORS[k] for k in keys]
    bar_labels = [LABELS[k] for k in keys]

    for ax, out_tok in zip(axes, OUTPUT_LENGTHS):
        ys = [
            next((r["total_latency_ms"] / 1000 for r in datasets[k]
                  if r["input_tokens_target"] == 1024 and r["output_tokens_target"] == out_tok), 0.0)
            for k in keys
        ]
        bars = ax.bar(range(len(keys)), ys, color=bar_colors, alpha=0.85, width=0.6)
        for bar, y in zip(bars, ys):
            ax.annotate(f"{y:.1f}s", (bar.get_x() + bar.get_width() / 2, y),
                        textcoords="offset points", xytext=(0, 3),
                        ha="center", fontsize=9)
        ax.set_title(f"out={out_tok}", fontsize=11)
        ax.set_xticks([])
        ax.set_ylabel("Latency (s)" if out_tok == OUTPUT_LENGTHS[0] else "")
        ax.set_ylim(bottom=0)

    fig.suptitle("End-to-End Latency (in≈886 tokens) by Output Length", fontsize=13, y=1.01)
    handles = [plt.Rectangle((0, 0), 1, 1, color=c, alpha=0.85) for c in bar_colors]
    fig.legend(handles, bar_labels, loc="lower center", ncol=3,
               fontsize=10, bbox_to_anchor=(0.5, -0.08))
    fig.tight_layout()
    fig.savefig(OUT / "fig5_e2e_latency.png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    print("Saved fig5_e2e_latency.png")


# ─── Figure 6: SGLang vs vLLM for Qwen3.6 ──────────────────────────────────
def fig_sglang_vs_vllm(qwen_sg: list[dict], qwen_vl: list[dict]) -> None:
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 5))

    pt_sg = [next((r["prompt_tokens_avg"] for r in qwen_sg
                   if r["input_tokens_target"] == i and r["output_tokens_target"] == 256), i)
             for i in INPUT_LENGTHS]
    pt_vl = [next((r["prompt_tokens_avg"] for r in qwen_vl
                   if r["input_tokens_target"] == i and r["output_tokens_target"] == 256), i)
             for i in INPUT_LENGTHS]

    # Decode
    dec_sg = [avg_decode(qwen_sg, i) for i in INPUT_LENGTHS]
    dec_vl = [avg_decode(qwen_vl, i) for i in INPUT_LENGTHS]
    ax1.plot(pt_sg, dec_sg, "o-", color=COLORS["qwen_sglang"], linewidth=2.2,
             markersize=8, label=LABELS["qwen_sglang"])
    ax1.plot(pt_vl, dec_vl, "s-", color=COLORS["qwen_vllm"], linewidth=2.2,
             markersize=8, label=LABELS["qwen_vllm"])
    ax1.set_xlabel("Prompt Tokens", fontsize=12)
    ax1.set_ylabel("Decode Throughput (tok/s)", fontsize=12)
    ax1.set_title("Decode: SGLang vs vLLM\n(Qwen3.6-35B-FP8)", fontsize=13)
    ax1.legend(fontsize=10)
    ax1.set_ylim(48, 56)

    # Prefill
    pf_sg = [avg_prefill(qwen_sg)[i] for i in INPUT_LENGTHS]
    pf_vl = [avg_prefill(qwen_vl)[i] for i in INPUT_LENGTHS]
    ax2.plot(pt_sg, pf_sg, "o-", color=COLORS["qwen_sglang"], linewidth=2.2,
             markersize=8, label=LABELS["qwen_sglang"])
    ax2.plot(pt_vl, pf_vl, "s-", color=COLORS["qwen_vllm"], linewidth=2.2,
             markersize=8, label=LABELS["qwen_vllm"])
    ax2.set_xlabel("Prompt Tokens", fontsize=12)
    ax2.set_ylabel("Prefill Throughput (tok/s)", fontsize=12)
    ax2.set_title("Prefill: SGLang vs vLLM\n(Qwen3.6-35B-FP8)", fontsize=13)
    ax2.legend(fontsize=10)
    ax2.set_ylim(bottom=0)

    fig.tight_layout()
    fig.savefig(OUT / "fig6_sglang_vs_vllm.png", dpi=150)
    plt.close(fig)
    print("Saved fig6_sglang_vs_vllm.png")


if __name__ == "__main__":
    qwen_sg = load("qwen_sglang")
    qwen_vl = load("qwen_vllm")
    glm_vl  = load("glm_vllm")

    datasets = {
        "qwen_sglang": qwen_sg,
        "qwen_vllm":   qwen_vl,
        "glm_vllm":    glm_vl,
    }

    fig_decode_vs_input(datasets)
    fig_prefill_vs_input(datasets)
    fig_prefill_latency(datasets)
    fig_decode_vs_output(datasets)
    fig_e2e_latency_bars(datasets)
    fig_sglang_vs_vllm(qwen_sg, qwen_vl)

    print("\nAll figures saved to", OUT)
