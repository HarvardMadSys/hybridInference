#!/usr/bin/env python3
"""Plot prefill/decode benchmark comparison figures."""

from __future__ import annotations

import json
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np


def load_results(path: str) -> list[dict]:
    with open(path) as f:
        return json.load(f)


def plot_figures(
    qwen_results: list[dict],
    glm_results: list[dict],
    output_dir: str = "ops/perf",
):
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    input_lengths = [128, 512, 1024, 2048, 4096]
    output_length = 256

    qwen_by_input = {}
    for r in qwen_results:
        if r["output_tokens_target"] == output_length:
            qwen_by_input[r["input_tokens_target"]] = r

    glm_by_input = {}
    for r in glm_results:
        if r["output_tokens_target"] == output_length:
            glm_by_input[r["input_tokens_target"]] = r

    # ── Figure 1: Prefill throughput vs input length ──
    fig, ax = plt.subplots(figsize=(10, 6))

    q_prefill = [qwen_by_input.get(n, {}).get("prefill_throughput_tok_s", 0) for n in input_lengths]
    g_prefill = [glm_by_input.get(n, {}).get("prefill_throughput_tok_s", 0) for n in input_lengths]
    q_prompt_tok = [qwen_by_input.get(n, {}).get("prompt_tokens_avg", 0) for n in input_lengths]
    g_prompt_tok = [glm_by_input.get(n, {}).get("prompt_tokens_avg", 0) for n in input_lengths]

    ax.plot(
        q_prompt_tok,
        q_prefill,
        "o-",
        color="#2563eb",
        linewidth=2.5,
        markersize=8,
        label="Qwen3.6-35B (sglang)",
    )
    ax.plot(
        g_prompt_tok,
        g_prefill,
        "s-",
        color="#dc2626",
        linewidth=2.5,
        markersize=8,
        label="GLM-4.7-Flash (vllm)",
    )

    ax.set_xlabel("Prompt Tokens", fontsize=13)
    ax.set_ylabel("Prefill Throughput (tok/s)", fontsize=13)
    ax.set_title(
        f"Prefill Throughput vs Prompt Length (output={output_length} tokens)", fontsize=14
    )
    ax.legend(fontsize=12)
    ax.grid(True, alpha=0.3)
    ax.tick_params(labelsize=11)

    for x, yq, yg in zip(q_prompt_tok, q_prefill, g_prefill, strict=True):
        ax.annotate(
            f"{yq:.0f}",
            (x, yq),
            textcoords="offset points",
            xytext=(0, 12),
            ha="center",
            fontsize=9,
            color="#2563eb",
        )
        ax.annotate(
            f"{yg:.0f}",
            (x, yg),
            textcoords="offset points",
            xytext=(0, -18),
            ha="center",
            fontsize=9,
            color="#dc2626",
        )

    plt.tight_layout()
    fig.savefig(out / "prefill_throughput.png", dpi=150)
    print(f"Saved {out / 'prefill_throughput.png'}")

    # ── Figure 2: Decode throughput bar chart ──
    fig, ax = plt.subplots(figsize=(10, 6))

    decode_configs = [64, 128, 256, 512]
    input_for_decode = 1024

    q_decode = []
    g_decode = []
    for out_tok in decode_configs:
        qr = next(
            (
                r
                for r in qwen_results
                if r["input_tokens_target"] == input_for_decode
                and r["output_tokens_target"] == out_tok
            ),
            None,
        )
        gr = next(
            (
                r
                for r in glm_results
                if r["input_tokens_target"] == input_for_decode
                and r["output_tokens_target"] == out_tok
            ),
            None,
        )
        q_decode.append(qr["decode_throughput_tok_s"] if qr else 0)
        g_decode.append(gr["decode_throughput_tok_s"] if gr else 0)

    x = np.arange(len(decode_configs))
    width = 0.35

    bars1 = ax.bar(x - width / 2, q_decode, width, label="Qwen3.6-35B", color="#2563eb", alpha=0.85)
    bars2 = ax.bar(
        x + width / 2, g_decode, width, label="GLM-4.7-Flash", color="#dc2626", alpha=0.85
    )

    for bar in bars1:
        h = bar.get_height()
        ax.annotate(
            f"{h:.1f}",
            (bar.get_x() + bar.get_width() / 2, h),
            textcoords="offset points",
            xytext=(0, 5),
            ha="center",
            fontsize=10,
        )
    for bar in bars2:
        h = bar.get_height()
        ax.annotate(
            f"{h:.1f}",
            (bar.get_x() + bar.get_width() / 2, h),
            textcoords="offset points",
            xytext=(0, 5),
            ha="center",
            fontsize=10,
        )

    ax.set_xlabel("Output Tokens (max_tokens)", fontsize=13)
    ax.set_ylabel("Decode Throughput (tok/s)", fontsize=13)
    ax.set_title(
        f"Decode Throughput vs Output Length (input={input_for_decode} tokens)", fontsize=14
    )
    ax.set_xticks(x)
    ax.set_xticklabels([str(c) for c in decode_configs], fontsize=11)
    ax.legend(fontsize=12)
    ax.grid(True, alpha=0.3, axis="y")
    ax.tick_params(labelsize=11)

    y_max = max(max(q_decode, default=0), max(g_decode, default=0))
    ax.set_ylim(0, y_max * 1.2)

    plt.tight_layout()
    fig.savefig(out / "decode_throughput.png", dpi=150)
    print(f"Saved {out / 'decode_throughput.png'}")

    # ── Figure 3: Combined overview (2x2) ──
    fig, axes = plt.subplots(2, 2, figsize=(14, 10))

    # (0,0) Prefill latency vs input tokens
    ax = axes[0, 0]
    q_prefill_ms = [qwen_by_input.get(n, {}).get("prefill_ms", 0) for n in input_lengths]
    g_prefill_ms = [glm_by_input.get(n, {}).get("prefill_ms", 0) for n in input_lengths]
    ax.plot(
        q_prompt_tok,
        q_prefill_ms,
        "o-",
        color="#2563eb",
        linewidth=2,
        markersize=7,
        label="Qwen3.6-35B",
    )
    ax.plot(
        g_prompt_tok,
        g_prefill_ms,
        "s-",
        color="#dc2626",
        linewidth=2,
        markersize=7,
        label="GLM-4.7-Flash",
    )
    ax.set_xlabel("Prompt Tokens")
    ax.set_ylabel("Prefill Latency (ms)")
    ax.set_title("Prefill Latency vs Input Length")
    ax.legend()
    ax.grid(True, alpha=0.3)

    # (0,1) Prefill throughput vs input tokens
    ax = axes[0, 1]
    ax.plot(
        q_prompt_tok,
        q_prefill,
        "o-",
        color="#2563eb",
        linewidth=2,
        markersize=7,
        label="Qwen3.6-35B",
    )
    ax.plot(
        g_prompt_tok,
        g_prefill,
        "s-",
        color="#dc2626",
        linewidth=2,
        markersize=7,
        label="GLM-4.7-Flash",
    )
    ax.set_xlabel("Prompt Tokens")
    ax.set_ylabel("Prefill Throughput (tok/s)")
    ax.set_title("Prefill Throughput vs Input Length")
    ax.legend()
    ax.grid(True, alpha=0.3)

    # (1,0) Decode throughput vs output tokens (fixed input=1024)
    ax = axes[1, 0]
    ax.bar(x - width / 2, q_decode, width, label="Qwen3.6-35B", color="#2563eb", alpha=0.85)
    ax.bar(x + width / 2, g_decode, width, label="GLM-4.7-Flash", color="#dc2626", alpha=0.85)
    ax.set_xlabel("Output Tokens")
    ax.set_ylabel("Decode Throughput (tok/s)")
    ax.set_title("Decode Throughput vs Output Length (in=1024)")
    ax.set_xticks(x)
    ax.set_xticklabels([str(c) for c in decode_configs])
    ax.legend()
    ax.grid(True, alpha=0.3, axis="y")

    # (1,1) Total latency heatmap-style: input x output for Qwen
    ax = axes[1, 1]
    all_output_toks = [64, 128, 256, 512]
    q_total_matrix = []
    g_total_matrix = []
    for in_tok in input_lengths:
        q_row = []
        g_row = []
        for out_tok in all_output_toks:
            qr = next(
                (
                    r
                    for r in qwen_results
                    if r["input_tokens_target"] == in_tok and r["output_tokens_target"] == out_tok
                ),
                None,
            )
            gr = next(
                (
                    r
                    for r in glm_results
                    if r["input_tokens_target"] == in_tok and r["output_tokens_target"] == out_tok
                ),
                None,
            )
            q_row.append(qr["total_latency_ms"] if qr else 0)
            g_row.append(gr["total_latency_ms"] if gr else 0)
        q_total_matrix.append(q_row)
        g_total_matrix.append(g_row)

    q_arr = np.array(q_total_matrix)
    g_arr = np.array(g_total_matrix)
    ratios = np.where(q_arr > 0, g_arr / q_arr, 1.0)

    im = ax.imshow(ratios, cmap="RdYlGn_r", aspect="auto", vmin=0.8, vmax=2.0)
    ax.set_xticks(range(len(all_output_toks)))
    ax.set_xticklabels([str(c) for c in all_output_toks])
    ax.set_yticks(range(len(input_lengths)))
    ax.set_yticklabels([str(n) for n in input_lengths])
    ax.set_xlabel("Output Tokens")
    ax.set_ylabel("Input Tokens (target)")
    ax.set_title("GLM/Qwen Total Latency Ratio (>1 = GLM slower)")

    for i in range(len(input_lengths)):
        for j in range(len(all_output_toks)):
            val = ratios[i, j]
            color = "white" if val > 1.5 else "black"
            ax.text(j, i, f"{val:.2f}", ha="center", va="center", fontsize=9, color=color)

    fig.colorbar(im, ax=ax, label="GLM Latency / Qwen Latency")

    fig.suptitle(
        "Local GB10 Benchmark: Qwen3.6-35B (sglang) vs GLM-4.7-Flash (vllm)",
        fontsize=15,
        fontweight="bold",
        y=1.01,
    )
    plt.tight_layout()
    fig.savefig(out / "benchmark_overview.png", dpi=150, bbox_inches="tight")
    print(f"Saved {out / 'benchmark_overview.png'}")

    plt.close("all")


if __name__ == "__main__":
    qwen = load_results(Path(__file__).parent.parent / "results" / "results_qwen_sglang.json")
    glm = load_results(Path(__file__).parent.parent / "results" / "results_glm_vllm.json")
    plot_figures(qwen, glm, output_dir=Path(__file__).parent.parent / "figures")
