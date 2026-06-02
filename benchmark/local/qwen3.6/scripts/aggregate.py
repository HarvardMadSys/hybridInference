"""Aggregate Qwen3.6 benchmark results.

Convert genai-perf JSON outputs into a tidy long-format CSV ready for
plotting. One row per (engine, phase, params, metric, run_id).

The genai-perf JSON schema can shift across versions; this module reads
defensively and treats missing fields as absent metrics rather than errors,
so a single malformed file does not block aggregation of the rest.

Filename convention assumed (produced by run_genai_perf.py):
    <phase>_<param>_run<N>.json
where param is `input_<len>` for prefill or `concurrency_<N>` for decode.

Usage:
    aggregate_directory(Path("results"), Path("results/summary.csv"))
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from benchmark.config import ENGINES

# Map genai-perf JSON keys → our canonical metric names.
# Each entry: (json_key, sub_key, our_metric_name).
# Note: time_per_output_token does not exist in genai-perf 0.0.16.
# tpot_ms_* and itl_ms_* both map to inter_token_latency (semantically equivalent).
_METRICS: tuple[tuple[str, str, str], ...] = (
    ("output_token_throughput", "avg", "throughput_tps"),
    ("output_token_throughput_per_user", "avg", "throughput_per_user_tps"),
    ("request_throughput", "avg", "request_throughput_rps"),
    ("time_to_first_token", "p50", "ttft_ms_p50"),
    ("time_to_first_token", "p95", "ttft_ms_p95"),
    ("inter_token_latency", "p50", "tpot_ms_p50"),
    ("inter_token_latency", "p95", "tpot_ms_p95"),
    ("inter_token_latency", "p50", "itl_ms_p50"),
    ("request_latency", "p50", "request_latency_ms_p50"),
    ("request_latency", "p95", "request_latency_ms_p95"),
)

_FNAME_RE = re.compile(r"^(?P<phase>prefill|decode)_(?P<param>.+)_run(?P<run>\d+)\.json$")


def _safe_get(data: dict[str, Any], top: str, sub: str) -> float | None:
    """Get data[top][sub] if present and numeric, else None."""
    section = data.get(top)
    if not isinstance(section, dict):
        return None
    val = section.get(sub)
    return float(val) if isinstance(val, (int, float)) else None


def parse_genai_perf(
    path: Path,
    engine: str,
    phase: str,
    run_id: int,
) -> list[dict[str, Any]]:
    """Parse one genai-perf JSON file into a list of long-format rows.

    Args:
        path: Path to the genai-perf JSON output file.
        engine: Name of the inference engine (e.g., "vllm", "sglang").
        phase: Benchmark phase ("prefill" or "decode").
        run_id: Integer identifier for the run (for repeated trials).

    Returns:
        A list of dicts, one per extracted metric. Fields present in every
        row: engine, phase, input_len, output_len, concurrency, metric,
        value, run_id, timestamp.  If a metric key is missing from the JSON,
        no row is emitted for that metric (rather than producing NaN).
    """
    data = json.loads(Path(path).read_text())

    # Context columns: top-level sequence-length sections + nested concurrency path.
    input_len = int(_safe_get(data, "input_sequence_length", "avg") or -1)
    output_len = int(_safe_get(data, "output_sequence_length", "avg") or -1)
    concurrency = int(
        data.get("input_config", {})
        .get("perf_analyzer", {})
        .get("stimulus", {})
        .get("concurrency", -1)
    )
    timestamp = datetime.now(timezone.utc).isoformat()

    rows: list[dict[str, Any]] = []

    def _emit(metric_name: str, value: float) -> None:
        rows.append(
            {
                "engine": engine,
                "phase": phase,
                "input_len": input_len,
                "output_len": output_len,
                "concurrency": concurrency,
                "metric": metric_name,
                "value": value,
                "run_id": run_id,
                "timestamp": timestamp,
            }
        )

    for top, sub, metric_name in _METRICS:
        value = _safe_get(data, top, sub)
        if value is None:
            continue
        _emit(metric_name, value)

    # Derived metric: prefill input throughput. With output=1, the engine's
    # `throughput_tps` (output tokens/sec) collapses to request rate, which
    # tells us nothing about prefill compute. Compute input_len / TTFT instead.
    if phase == "prefill" and input_len > 0:
        ttft_ms_p50 = _safe_get(data, "time_to_first_token", "p50")
        if ttft_ms_p50 is not None and ttft_ms_p50 > 0:
            _emit("prefill_tps_input_p50", input_len / (ttft_ms_p50 / 1000.0))

    return rows


def aggregate_directory(results_root: Path, out_csv: Path) -> None:
    """Walk `results_root/<engine>/*.json` and write a tidy CSV to `out_csv`.

    Scans each immediate subdirectory of `results_root` as an engine name,
    then matches JSON files against the filename convention
    `<phase>_<param>_run<N>.json`. Files that do not match the convention
    are silently skipped.

    Args:
        results_root: Root directory containing per-engine subdirectories.
        out_csv: Destination CSV path (parent dirs are created if needed).
    """
    all_rows: list[dict[str, Any]] = []
    for engine_dir in sorted(p for p in results_root.iterdir() if p.is_dir() and p.name in ENGINES):
        engine = engine_dir.name
        for json_path in sorted(engine_dir.glob("*.json")):
            m = _FNAME_RE.match(json_path.name)
            if m is None:
                continue
            phase = m.group("phase")
            run_id = int(m.group("run"))
            all_rows.extend(parse_genai_perf(json_path, engine, phase, run_id))

    df = pd.DataFrame(
        all_rows,
        columns=[
            "engine",
            "phase",
            "input_len",
            "output_len",
            "concurrency",
            "metric",
            "value",
            "run_id",
            "timestamp",
        ],
    )
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_csv, index=False)
