"""Drive the MiniMax-M2.7 benchmark pipeline.

Pipeline stages (each gated by a sentinel under config.STATE_DIR):
    1. venvs_ready            — vLLM + sglang + genai-perf venvs present
    2. model_downloaded       — model weights present at config.MODEL_DIR
    3. <engine>_started       — server came up healthy at least once
    4. <engine>_prefill_done  — full prefill battery completed
    5. <engine>_decode_done   — full decode battery completed
    6. aggregated             — results/summary.csv written
    7. plotted                — results/plots/*.png written

Re-running this driver picks up where it left off by checking sentinels.

Engines run as native processes (no Docker on this shared host); see
engines/{vllm,sglang}.sh. Run this module with the genai-perf venv's Python so
genai-perf, pandas, matplotlib, and requests are all importable:

    cd benchmark/local/minimax-m2.7/scripts
    PATH=/netscratch/juncheng/venvs/genai/bin:$PATH \
        /netscratch/juncheng/venvs/genai/bin/python orchestrate.py

Usage:
    orchestrate.py                       # full run, both engines
    orchestrate.py --dry-run
    orchestrate.py --engines vllm
    orchestrate.py --rerun sglang_decode_done   # force redo
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

import config
import requests
from aggregate import aggregate_directory
from plot import generate_all_plots
from run_genai_perf import run_decode, run_prefill
from state import StateStore

if TYPE_CHECKING:
    from collections.abc import Iterable


def plan_steps(engines: Iterable[str]) -> list[str]:
    """Return the ordered list of sentinel names this run would touch."""
    steps: list[str] = ["venvs_ready", "model_downloaded"]
    for engine in engines:
        steps.append(f"{engine}_started")
        steps.append(f"{engine}_prefill_done")
        steps.append(f"{engine}_decode_done")
    steps += ["aggregated", "plotted"]
    return steps


def dry_run(engines: Iterable[str]) -> None:
    """Print what the pipeline would do without executing any steps."""
    store = StateStore(config.STATE_DIR)
    for step in plan_steps(engines):
        prefix = "SKIP" if store.is_done(step) else "WOULD RUN"
        print(f"{prefix} {step}")


def _wait_for_health(url: str, timeout_s: int) -> None:
    """Poll {url}/v1/models until 200 or timeout."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            r = requests.get(f"{url}/v1/models", timeout=5)
            if r.status_code == 200:
                return
        except requests.RequestException:
            pass
        time.sleep(5)
    raise TimeoutError(f"Server at {url} did not become healthy within {timeout_s}s")


def _engine_script(engine: str) -> Path:
    """Return the absolute path to the engine launch script."""
    return Path(__file__).resolve().parent / "engines" / f"{engine}.sh"


def _engine_url(engine: str) -> str:
    """Return the base URL for the given engine's server."""
    return f"http://localhost:{config.ENGINE_PORT[engine]}"


def _start_engine(engine: str) -> None:
    """Start the engine server and wait for it to be healthy."""
    subprocess.run(["bash", str(_engine_script(engine)), "start"], check=True)
    _wait_for_health(_engine_url(engine), timeout_s=config.SERVER_HEALTH_TIMEOUT_S)


def _stop_engine(engine: str) -> None:
    """Stop the engine server. Errors are silently ignored."""
    subprocess.run(["bash", str(_engine_script(engine)), "stop"], check=False)
    # Give the GPU a moment to release memory before the next engine starts.
    time.sleep(10)


def _run_prefill_battery(engine: str, num_repeats: int) -> None:
    """Run the full prefill benchmark matrix for one engine."""
    url = _engine_url(engine)
    for input_len in config.PREFILL_INPUT_LENS:
        for run_id in range(1, num_repeats + 1):
            print(f"  [{engine}] prefill input={input_len} run={run_id}", flush=True)
            run_prefill(engine, url, input_len, run_id)


def _run_decode_battery(engine: str, num_repeats: int) -> None:
    """Run the full decode benchmark matrix for one engine."""
    url = _engine_url(engine)
    for conc in config.DECODE_CONCURRENCIES:
        for run_id in range(1, num_repeats + 1):
            print(f"  [{engine}] decode concurrency={conc} run={run_id}", flush=True)
            run_decode(engine, url, conc, run_id)


def _ensure_venvs(store: StateStore) -> None:
    """Check the engine + harness venvs exist; exit with a hint if not."""
    if store.is_done("venvs_ready"):
        return
    missing = []
    for rel in ("vllm/bin/vllm", "sglang/bin/python", "genai/bin/genai-perf"):
        if not (config.VENV_DIR / rel).exists():
            missing.append(str(config.VENV_DIR / rel))
    if missing:
        print("Missing engine/harness venvs:\n  " + "\n  ".join(missing))
        sys.exit(1)
    store.mark_done("venvs_ready")


def _ensure_model_downloaded(store: StateStore) -> None:
    """Check model weights exist at config.MODEL_DIR; exit with a hint if not."""
    if store.is_done("model_downloaded"):
        return
    if not config.MODEL_DIR.exists() or not any(config.MODEL_DIR.glob("*.safetensors")):
        print(f"Model not found at {config.MODEL_DIR}. Download it first. Aborting.")
        sys.exit(1)
    store.mark_done("model_downloaded")


def _run_engine_phases(engine: str, store: StateStore, num_repeats: int) -> None:
    """Run prefill + decode batteries for one engine, with sentinel guards."""
    prefill_done = store.is_done(f"{engine}_prefill_done")
    decode_done = store.is_done(f"{engine}_decode_done")
    if prefill_done and decode_done:
        return
    print(f"=== {engine.upper()} ===", flush=True)
    _start_engine(engine)
    store.mark_done(f"{engine}_started")
    try:
        if not prefill_done:
            _run_prefill_battery(engine, num_repeats)
            store.mark_done(f"{engine}_prefill_done")
            # Restart between batteries for clean KV-cache state.
            _stop_engine(engine)
            _start_engine(engine)
        if not decode_done:
            _run_decode_battery(engine, num_repeats)
            store.mark_done(f"{engine}_decode_done")
    finally:
        _stop_engine(engine)


def _aggregate_and_plot(store: StateStore) -> None:
    """Aggregate JSON results into summary CSV and generate all plots."""
    if not store.is_done("aggregated"):
        aggregate_directory(config.RESULTS_DIR, config.SUMMARY_CSV)
        store.mark_done("aggregated")
    if not store.is_done("plotted"):
        generate_all_plots(config.SUMMARY_CSV, config.PLOTS_DIR)
        store.mark_done("plotted")


def main(argv: list[str] | None = None) -> int:
    """Entry point for the pipeline orchestrator."""
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--engines", nargs="+", default=list(config.ENGINES), choices=list(config.ENGINES)
    )
    parser.add_argument("--num-repeats", type=int, default=config.NUM_REPEATS)
    parser.add_argument(
        "--rerun", action="append", default=[], help="Sentinel name(s) to clear before running"
    )
    parser.add_argument(
        "--skip-aggregate", action="store_true", help="Skip the aggregate + plot stage"
    )
    args = parser.parse_args(argv)

    store = StateStore(config.STATE_DIR)
    for s in args.rerun:
        store.clear(s)

    if args.dry_run:
        dry_run(args.engines)
        return 0

    _ensure_venvs(store)
    _ensure_model_downloaded(store)
    for engine in args.engines:
        _run_engine_phases(engine, store, args.num_repeats)
    if not args.skip_aggregate:
        _aggregate_and_plot(store)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
