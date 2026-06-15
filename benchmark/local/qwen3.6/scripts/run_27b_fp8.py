#!/usr/bin/env python3
"""Ad-hoc throughput benchmark for Qwen3.6-27B-FP8 on vLLM.

Matrix (requested):
  - prefill: 8192 input / 1 output, concurrency 1
  - decode:  8192 input / 1024 output, concurrency 1,2,4,8,16,32

Uses NVIDIA genai-perf against the OpenAI-compatible vLLM server and prints a
tidy summary table. Self-contained: does not import the benchmark.config module
(matrix differs from the 35B head-to-head).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

MODEL = os.environ.get("MODEL", "Qwen/Qwen3.6-27B-FP8")
URL = os.environ.get("URL", "http://localhost:8000")
TOKENIZER = os.environ.get(
    "TOKENIZER",
    "/scratch/hf/hub/models--Qwen--Qwen3.6-27B-FP8/"
    "snapshots/e89b16ebf1988b3d6befa7de50abc2d76f26eb09",
)
RESULTS_NAME = os.environ.get("RESULTS_NAME", "results_27b_fp8")
OUT_DIR = Path(__file__).resolve().parent.parent / RESULTS_NAME / "vllm"

# DECODE_ONLY skips the prefill battery; DECODE_INPUT overrides decode prompt len.
DECODE_ONLY = os.environ.get("DECODE_ONLY", "0") == "1"
PREFILL_INPUT = 8192
DECODE_INPUT = int(os.environ.get("DECODE_INPUT", "8192"))
DECODE_OUTPUT = 1024
DECODE_CONCURRENCIES = (1, 2, 4, 8, 16, 32)

GENAI_PERF = shutil.which("genai-perf") or "genai-perf"


def _common_args(
    input_len: int, output_len: int, concurrency: int, count: int, warmup: int, out: Path
) -> list[str]:
    return [
        GENAI_PERF,
        "profile",
        "--model",
        MODEL,
        "--endpoint-type",
        "completions",
        "--streaming",
        "--url",
        URL,
        "--tokenizer",
        TOKENIZER,
        "--synthetic-input-tokens-mean",
        str(input_len),
        "--synthetic-input-tokens-stddev",
        "0",
        "--output-tokens-mean",
        str(output_len),
        "--output-tokens-stddev",
        "0",
        "--extra-inputs",
        "ignore_eos:true",
        "--extra-inputs",
        f"max_tokens:{output_len}",
        "--extra-inputs",
        "min_tokens:" + str(output_len),
        "--concurrency",
        str(concurrency),
        "--request-count",
        str(count),
        "--warmup-request-count",
        str(warmup),
        "--artifact-dir",
        str(out.parent),
        "--profile-export-file",
        out.name,
    ]


def _run(args: list[str], out: Path) -> dict:
    out.parent.mkdir(parents=True, exist_ok=True)
    print("\n$ " + " ".join(args), flush=True)
    subprocess.run(args, check=True)
    produced = next(out.parent.rglob(f"{out.stem}_genai_perf.json"), None)
    if produced is None:
        raise RuntimeError(f"no genai-perf output for {out.stem}")
    produced.replace(out)
    return json.loads(out.read_text())


def _g(d: dict, top: str, sub: str):
    sec = d.get(top)
    if isinstance(sec, dict) and isinstance(sec.get(sub), (int, float)):
        return float(sec[sub])
    return None


def main() -> None:
    """Run the prefill/decode benchmark matrix and print the summary table."""
    results = []

    # ---- Prefill battery: 8k in / 1 out, concurrency 1 ----
    if not DECODE_ONLY:
        out = OUT_DIR / "prefill_input_8192_run1.json"
        d = _run(_common_args(PREFILL_INPUT, 1, 1, 100, 10, out), out)
        in_len = _g(d, "input_sequence_length", "avg") or PREFILL_INPUT
        ttft = _g(d, "time_to_first_token", "p50")
        results.append(
            {
                "phase": "prefill",
                "conc": 1,
                "in": int(in_len),
                "out": 1,
                "prefill_tps": (in_len / (ttft / 1000.0)) if ttft else None,
                "ttft_ms_p50": ttft,
                "ttft_ms_p95": _g(d, "time_to_first_token", "p95"),
            }
        )

    # ---- Decode battery: 8k in / 1k out, concurrency sweep ----
    for c in DECODE_CONCURRENCIES:
        count = max(30, 12 * c)
        warmup = max(3, c)
        out = OUT_DIR / f"decode_concurrency_{c}_run1.json"
        d = _run(_common_args(DECODE_INPUT, DECODE_OUTPUT, c, count, warmup, out), out)
        results.append(
            {
                "phase": "decode",
                "conc": c,
                "in": int(_g(d, "input_sequence_length", "avg") or DECODE_INPUT),
                "out": int(_g(d, "output_sequence_length", "avg") or DECODE_OUTPUT),
                "decode_tps": _g(d, "output_token_throughput", "avg"),
                "tps_per_user": _g(d, "output_token_throughput_per_user", "avg"),
                "req_throughput": _g(d, "request_throughput", "avg"),
                "ttft_ms_p50": _g(d, "time_to_first_token", "p50"),
                "tpot_ms_p50": _g(d, "inter_token_latency", "p50"),
                "tpot_ms_p95": _g(d, "inter_token_latency", "p95"),
            }
        )

    (OUT_DIR.parent / "summary.json").write_text(json.dumps(results, indent=2))

    # ---- Print tables ----
    print("\n\n===== PREFILL (8192 in / 1 out, concurrency 1) =====")
    print(f"{'in_tok':>7} {'prefill_tps':>12} {'ttft_p50_ms':>12} {'ttft_p95_ms':>12}")
    for r in results:
        if r["phase"] == "prefill":
            print(
                f"{r['in']:>7} {r['prefill_tps']:>12.0f} "
                f"{r['ttft_ms_p50']:>12.1f} {r['ttft_ms_p95']:>12.1f}"
            )

    print("\n===== DECODE (8192 in / 1024 out) =====")
    print(
        f"{'batch':>5} {'decode_tps':>11} {'tps/user':>9} "
        f"{'ttft_p50_ms':>12} {'tpot_p50_ms':>12} {'tpot_p95_ms':>12}"
    )
    for r in results:
        if r["phase"] == "decode":
            print(
                f"{r['conc']:>5} {r['decode_tps']:>11.1f} {r['tps_per_user']:>9.1f} "
                f"{r['ttft_ms_p50']:>12.1f} {r['tpot_ms_p50']:>12.2f} "
                f"{r['tpot_ms_p95']:>12.2f}"
            )


if __name__ == "__main__":
    main()
