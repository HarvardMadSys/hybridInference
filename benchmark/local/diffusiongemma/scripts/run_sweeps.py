"""Drive genai-perf sweeps for DiffusionGemma on gpu1 (rtx6000, live :8001).

DiffusionGemma is a block-diffusion LLM: one denoising pass emits a whole block,
so request latency tracks the *block size* (max_tokens), input length, and
concurrency — not the number of tokens actually returned. Autoregressive metrics
(inter-token latency, TPOT, output-token throughput) are therefore not meaningful
here; we record genai-perf's reliable request_latency and request_throughput.

Three sweeps:
  1. block-size : input=256, concurrency=1, max_tokens in {32..1024}  -> latency vs block
  2. concurrency: input=256, max_tokens=256, concurrency in {1..32}   -> latency + throughput
  3. prefill    : max_tokens=8, concurrency=1, input in {512..131072} -> latency vs context

Writes results/summary.json and results/summary.csv.
"""

from __future__ import annotations

import csv
import glob
import json
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
RESULTS = ROOT / "results"
ARTIFACTS = RESULTS / "artifacts"
RESULTS.mkdir(parents=True, exist_ok=True)

MODEL = "nvidia/diffusiongemma-26B-A4B-it-NVFP4"
TOKENIZER = "/scratch/juncheng/models/diffusiongemma-26B-A4B-it-NVFP4"
URL = "http://127.0.0.1:8001"
KEY = "freeinference_api"

SWEEPS = [
    # name, input_len, output_len, concurrency, request_count, warmup
    *[("block_size", 256, o, 1, 24, 4) for o in (32, 64, 128, 256, 512, 1024)],
    *[("concurrency", 256, 256, c, 24, 4) for c in (1, 2, 4, 8, 16, 32)],
    *[("prefill", i, 8, 1, 12, 2) for i in (512, 2048, 8192, 32768, 131072)],
]


def run_point(name: str, in_len: int, out_len: int, conc: int, rc: int, warm: int) -> dict:
    """Run one genai-perf point and parse request latency/throughput from its export."""
    tag = f"{name}_in{in_len}_out{out_len}_c{conc}"
    artifact_dir = ARTIFACTS / tag
    cmd = [
        "genai-perf",
        "profile",
        "--model",
        MODEL,
        "--endpoint-type",
        "completions",
        "--url",
        URL,
        "--tokenizer",
        TOKENIZER,
        "--synthetic-input-tokens-mean",
        str(in_len),
        "--synthetic-input-tokens-stddev",
        "0",
        "--output-tokens-mean",
        str(out_len),
        "--output-tokens-stddev",
        "0",
        "--extra-inputs",
        "ignore_eos:true",
        "--concurrency",
        str(conc),
        "--request-count",
        str(rc),
        "--warmup-request-count",
        str(warm),
        "--artifact-dir",
        str(artifact_dir),
        "--profile-export-file",
        "p.json",
        "-H",
        f"Authorization: Bearer {KEY}",
    ]
    print(f"[run] {tag} …", flush=True)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        print(f"  FAILED rc={proc.returncode}: {proc.stderr[-300:]}", flush=True)
        return {"tag": tag, "sweep": name, "ok": False}
    matches = glob.glob(str(artifact_dir / "**" / "p_genai_perf.json"), recursive=True)
    if not matches:
        print("  no export json produced", flush=True)
        return {"tag": tag, "sweep": name, "ok": False}
    with open(matches[0]) as f:
        d = json.load(f)

    def m(key: str, stat: str = "avg"):
        return (d.get(key) or {}).get(stat)

    row = {
        "tag": tag,
        "sweep": name,
        "ok": True,
        "input_len": in_len,
        "block_max_tokens": out_len,
        "concurrency": conc,
        "req_latency_ms_avg": m("request_latency"),
        "req_latency_ms_p50": m("request_latency", "p50"),
        "req_latency_ms_p90": m("request_latency", "p90"),
        "req_latency_ms_p99": m("request_latency", "p99"),
        "req_throughput_per_s": m("request_throughput"),
        "out_seq_len_avg": m("output_sequence_length"),
        "in_seq_len_avg": m("input_sequence_length"),
    }
    lat = row["req_latency_ms_avg"]
    thr = row["req_throughput_per_s"]
    print(
        f"  latency avg={lat:.0f}ms p90={row['req_latency_ms_p90']:.0f}ms | "
        f"throughput={thr:.2f} req/s | out_tok_avg={row['out_seq_len_avg']:.0f}",
        flush=True,
    )
    return row


def main() -> None:
    """Run all sweep points and write results/summary.{json,csv}."""
    rows = [run_point(*s) for s in SWEEPS]
    ok = [r for r in rows if r.get("ok")]
    (RESULTS / "summary.json").write_text(json.dumps(rows, indent=2))
    if ok:
        cols = list(ok[0].keys())
        with open(RESULTS / "summary.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            w.writerows(ok)
    print(f"\nDONE: {len(ok)}/{len(rows)} points ok -> {RESULTS / 'summary.json'}", flush=True)


if __name__ == "__main__":
    main()
