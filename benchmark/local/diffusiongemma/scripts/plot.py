"""Render figures from the DiffusionGemma sweep summary.

Reads results/summary.json (written by run_sweeps.py) and emits three figures
into figures/. Requires matplotlib.
"""

from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
FIG = ROOT / "figures"
FIG.mkdir(parents=True, exist_ok=True)

with (ROOT / "results" / "summary.json").open() as _f:
    rows = [r for r in json.load(_f) if r.get("ok")]


def by(sweep: str) -> list[dict]:
    """Return the ok rows for one sweep, ordered by the varied axis."""
    return sorted(
        [r for r in rows if r["sweep"] == sweep],
        key=lambda r: (r["block_max_tokens"], r["concurrency"], r["input_len"]),
    )


# 1. Latency vs block size (max_tokens), concurrency=1
bs = sorted(by("block_size"), key=lambda r: r["block_max_tokens"])
if bs:
    x = [r["block_max_tokens"] for r in bs]
    fig, ax = plt.subplots(figsize=(6, 4))
    ax.plot(x, [r["req_latency_ms_avg"] for r in bs], "o-", label="avg")
    ax.plot(x, [r["req_latency_ms_p90"] for r in bs], "s--", label="p90", alpha=0.7)
    ax.set_xlabel("Block size — max_tokens")
    ax.set_ylabel("Request latency (ms)")
    ax.set_title("DiffusionGemma: latency vs block size (input=256, concurrency=1)")
    ax.grid(True, alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(FIG / "latency_vs_block_size.png", dpi=120)
    plt.close(fig)

# 2. Latency + throughput vs concurrency
cc = sorted(by("concurrency"), key=lambda r: r["concurrency"])
if cc:
    x = [r["concurrency"] for r in cc]
    fig, ax = plt.subplots(figsize=(6, 4))
    ax.plot(x, [r["req_latency_ms_avg"] for r in cc], "o-", color="tab:blue", label="latency avg")
    ax.set_xlabel("Concurrency")
    ax.set_ylabel("Request latency (ms)", color="tab:blue")
    ax.tick_params(axis="y", labelcolor="tab:blue")
    ax.set_xscale("log", base=2)
    ax.grid(True, alpha=0.3)
    ax2 = ax.twinx()
    ax2.plot(
        x,
        [r["req_throughput_per_s"] for r in cc],
        "s--",
        color="tab:red",
        label="throughput",
    )
    ax2.set_ylabel("Request throughput (req/s)", color="tab:red")
    ax2.tick_params(axis="y", labelcolor="tab:red")
    ax.set_title("DiffusionGemma: latency & throughput vs concurrency (in=256, block=256)")
    fig.tight_layout()
    fig.savefig(FIG / "latency_throughput_vs_concurrency.png", dpi=120)
    plt.close(fig)

# 3. Latency vs input (prefill) length
pf = sorted(by("prefill"), key=lambda r: r["input_len"])
if pf:
    x = [r["input_len"] for r in pf]
    fig, ax = plt.subplots(figsize=(6, 4))
    ax.plot(x, [r["req_latency_ms_avg"] for r in pf], "o-", color="tab:green")
    ax.set_xscale("log", base=2)
    ax.set_xlabel("Input (prompt) length — tokens")
    ax.set_ylabel("Request latency (ms)")
    ax.set_title("DiffusionGemma: prefill latency vs context length (block=8, concurrency=1)")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(FIG / "prefill_latency_vs_input_len.png", dpi=120)
    plt.close(fig)

print("wrote figures to", FIG)
