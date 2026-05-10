"""
benchmark.run_genai_perf
========================

Thin wrapper that builds and runs `genai-perf` invocations for prefill and
decode batteries against an OpenAI-compatible server. Writes the JSON output
file to a deterministic name under `results/<engine>/`.

Usage (from orchestrate.py):
    run_prefill(engine="vllm", url="http://localhost:8000",
                input_len=4096, run_id=1)
    run_decode(engine="vllm", url="http://localhost:8000",
               concurrency=16, run_id=1)
"""

from __future__ import annotations

import shutil
import subprocess
from typing import TYPE_CHECKING

from benchmark import config

if TYPE_CHECKING:
    from pathlib import Path


def _genai_perf_exe() -> str:
    """Return the absolute path to the genai-perf executable, or raise."""
    exe = shutil.which("genai-perf")
    if exe is None:
        raise RuntimeError(
            "genai-perf not found on PATH. Install via `pip install genai-perf` "
            "or fall back to the Triton SDK container."
        )
    return exe


def _output_path(engine: str, phase: str, param: int, run_id: int) -> Path:
    """Return (and create) a deterministic output path for a benchmark run.

    Args:
        engine: One of "vllm", "sglang", "trtllm".
        phase:  "prefill" or "decode".
        param:  input_len for prefill; concurrency for decode.
        run_id: 1-based repeat index.

    Returns:
        Path object pointing to the JSON output file.
    """
    name = (
        f"prefill_input_{param}_run{run_id}.json"
        if phase == "prefill"
        else f"decode_concurrency_{param}_run{run_id}.json"
    )
    out = config.RESULTS_DIR / engine / name
    out.parent.mkdir(parents=True, exist_ok=True)
    return out


def build_prefill_args(url: str, input_len: int, output_path: Path) -> list[str]:
    """Build the argv list for a prefill benchmark run.

    genai-perf 0.0.16 requires --artifact-dir for directory placement and
    --profile-export-file to be a basename only (no path components). The tool
    writes the result to <artifact-dir>/<auto_subdir>/<basename_stem>_genai_perf.json.
    --streaming is required to surface TPOT/ITL/per-user-throughput metrics.

    Args:
        url:         Base URL of the OpenAI-compatible server (e.g. "http://localhost:8000").
        input_len:   Mean number of synthetic input tokens.
        output_path: Canonical destination path; its parent becomes --artifact-dir
                     and its name becomes --profile-export-file.

    Returns:
        A list of strings suitable for passing to subprocess.run().
    """
    return [
        _genai_perf_exe(),
        "profile",
        "--model",
        config.MODEL_REPO,
        "--endpoint-type",
        "completions",
        "--streaming",
        "--url",
        url,
        "--tokenizer",
        config.MODEL_REPO,
        "--synthetic-input-tokens-mean",
        str(input_len),
        "--synthetic-input-tokens-stddev",
        "0",
        "--output-tokens-mean",
        str(config.PREFILL_OUTPUT_LEN),
        "--output-tokens-stddev",
        "0",
        "--concurrency",
        str(config.PREFILL_CONCURRENCY),
        "--request-count",
        str(config.MEASUREMENT_REQUESTS),
        "--warmup-request-count",
        str(config.WARMUP_REQUESTS),
        "--artifact-dir",
        str(output_path.parent),
        "--profile-export-file",
        output_path.name,
    ]


def build_decode_args(url: str, concurrency: int, output_path: Path) -> list[str]:
    """Build the argv list for a decode benchmark run.

    genai-perf 0.0.16 requires --artifact-dir for directory placement and
    --profile-export-file to be a basename only (no path components). The tool
    writes the result to <artifact-dir>/<auto_subdir>/<basename_stem>_genai_perf.json.
    --streaming is required to surface TPOT/ITL/per-user-throughput metrics.

    Args:
        url:         Base URL of the OpenAI-compatible server (e.g. "http://localhost:8000").
        concurrency: Number of simultaneous requests.
        output_path: Canonical destination path; its parent becomes --artifact-dir
                     and its name becomes --profile-export-file.

    Returns:
        A list of strings suitable for passing to subprocess.run().
    """
    return [
        _genai_perf_exe(),
        "profile",
        "--model",
        config.MODEL_REPO,
        "--endpoint-type",
        "completions",
        "--streaming",
        "--url",
        url,
        "--tokenizer",
        config.MODEL_REPO,
        "--synthetic-input-tokens-mean",
        str(config.DECODE_INPUT_LEN),
        "--synthetic-input-tokens-stddev",
        "0",
        "--output-tokens-mean",
        str(config.DECODE_OUTPUT_LEN),
        "--output-tokens-stddev",
        "0",
        "--concurrency",
        str(concurrency),
        "--request-count",
        str(config.MEASUREMENT_REQUESTS),
        "--warmup-request-count",
        str(config.WARMUP_REQUESTS),
        "--artifact-dir",
        str(output_path.parent),
        "--profile-export-file",
        output_path.name,
    ]


def run_prefill(engine: str, url: str, input_len: int, run_id: int) -> Path:
    """Run genai-perf for a single prefill benchmark point.

    After genai-perf exits it produces a file at
    <artifact-dir>/<auto_subdir>/<out.stem>_genai_perf.json. This function
    finds that file via glob and renames it to the canonical `out` path so the
    rest of the pipeline (aggregate.py) can find it by the expected name.

    Args:
        engine:    Engine label ("vllm", "sglang", "trtllm").
        url:       Base URL of the OpenAI-compatible server.
        input_len: Mean number of synthetic input tokens.
        run_id:    1-based repeat index (used in the output filename).

    Returns:
        Path to the JSON profile export at the canonical location.

    Raises:
        subprocess.CalledProcessError: if genai-perf exits with a non-zero status.
        RuntimeError: if genai-perf produced no matching output file.
    """
    out = _output_path(engine, "prefill", input_len, run_id)
    args = build_prefill_args(url, input_len, out)
    subprocess.run(args, check=True)
    # genai-perf produced <out.parent>/<auto_subdir>/<out.stem>_genai_perf.json
    produced = next(out.parent.rglob(f"{out.stem}_genai_perf.json"), None)
    if produced is None:
        raise RuntimeError(
            f"genai-perf produced no output matching {out.stem}_genai_perf.json under {out.parent}"
        )
    produced.replace(out)
    return out


def run_decode(engine: str, url: str, concurrency: int, run_id: int) -> Path:
    """Run genai-perf for a single decode benchmark point.

    After genai-perf exits it produces a file at
    <artifact-dir>/<auto_subdir>/<out.stem>_genai_perf.json. This function
    finds that file via glob and renames it to the canonical `out` path so the
    rest of the pipeline (aggregate.py) can find it by the expected name.

    Args:
        engine:      Engine label ("vllm", "sglang", "trtllm").
        url:         Base URL of the OpenAI-compatible server.
        concurrency: Number of simultaneous requests.
        run_id:      1-based repeat index (used in the output filename).

    Returns:
        Path to the JSON profile export at the canonical location.

    Raises:
        subprocess.CalledProcessError: if genai-perf exits with a non-zero status.
        RuntimeError: if genai-perf produced no matching output file.
    """
    out = _output_path(engine, "decode", concurrency, run_id)
    args = build_decode_args(url, concurrency, out)
    subprocess.run(args, check=True)
    # genai-perf produced <out.parent>/<auto_subdir>/<out.stem>_genai_perf.json
    produced = next(out.parent.rglob(f"{out.stem}_genai_perf.json"), None)
    if produced is None:
        raise RuntimeError(
            f"genai-perf produced no output matching {out.stem}_genai_perf.json under {out.parent}"
        )
    produced.replace(out)
    return out
