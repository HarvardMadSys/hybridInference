"""
benchmark.orchestrate
=====================

Top-level pipeline driver.

Pipeline stages (each gated by a sentinel under config.STATE_DIR):
    1. docker_installed       — host has Docker + NVIDIA Container Toolkit
    2. model_downloaded       — model weights present at config.MODEL_DIR
    3. <engine>_image_pulled  — container image present locally
    4. <engine>_prefill_done  — full prefill battery completed (NUM_REPEATS runs)
    5. <engine>_decode_done   — full decode battery completed
    6. aggregated             — results/summary.csv written
    7. plotted                — results/plots/*.png written

Re-running this driver picks up where it left off by checking sentinels.

Usage:
    python -m benchmark.orchestrate          # full run, all engines
    python -m benchmark.orchestrate --dry-run
    python -m benchmark.orchestrate --engines vllm sglang
    python -m benchmark.orchestrate --rerun trtllm_decode_done   # force redo
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterable

import requests

from benchmark import config
from benchmark.aggregate import aggregate_directory
from benchmark.plot import generate_all_plots
from benchmark.run_genai_perf import run_prefill, run_decode
from benchmark.state import StateStore


def plan_steps(engines: Iterable[str], num_repeats: int) -> list[str]:
    """Return the ordered list of sentinel names this run would touch.

    Args:
        engines:     Iterable of engine names (e.g., ("vllm", "sglang")).
        num_repeats: Number of repeated benchmark runs (unused in naming,
                     but included for signature symmetry with dry_run).

    Returns:
        Ordered list of sentinel step names.
    """
    steps: list[str] = ["docker_installed", "model_downloaded"]
    for engine in engines:
        steps.append(f"{engine}_image_pulled")
        steps.append(f"{engine}_prefill_done")
        steps.append(f"{engine}_decode_done")
    steps += ["aggregated", "plotted"]
    return steps


def dry_run(engines: Iterable[str], num_repeats: int) -> None:
    """Print what the pipeline would do without executing any steps.

    Steps with existing sentinels are printed as "SKIP <step>"; steps
    without sentinels are printed as "WOULD RUN <step>".

    Args:
        engines:     Iterable of engine names to include.
        num_repeats: Number of repeated benchmark runs (forwarded to plan_steps).
    """
    store = StateStore(config.STATE_DIR)
    for step in plan_steps(engines, num_repeats):
        prefix = "SKIP" if store.is_done(step) else "WOULD RUN"
        print(f"{prefix} {step}")


def _wait_for_health(url: str, timeout_s: int = 300) -> None:
    """Poll {url}/v1/models until 200 or timeout.

    Args:
        url:       Base URL of the OpenAI-compatible server.
        timeout_s: Maximum seconds to wait before raising TimeoutError.

    Raises:
        TimeoutError: if the server does not respond with HTTP 200 within timeout_s.
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            r = requests.get(f"{url}/v1/models", timeout=5)
            if r.status_code == 200:
                return
        except requests.RequestException:
            pass
        time.sleep(2)
    raise TimeoutError(f"Server at {url} did not become healthy within {timeout_s}s")


def _engine_script(engine: str) -> Path:
    """Return the absolute path to the engine launch script.

    Args:
        engine: Engine name (e.g., "vllm").

    Returns:
        Path to benchmark/engines/<engine>.sh.
    """
    return Path(__file__).resolve().parent / "engines" / f"{engine}.sh"


def _engine_url(engine: str) -> str:
    """Return the base URL for the given engine's server.

    Args:
        engine: Engine name (e.g., "vllm").

    Returns:
        URL string of the form "http://localhost:<port>".
    """
    return f"http://localhost:{config.ENGINE_PORT[engine]}"


def _start_engine(engine: str) -> None:
    """Start the engine container and wait for it to be healthy.

    Args:
        engine: Engine name (e.g., "vllm").

    Raises:
        subprocess.CalledProcessError: if the launch script exits non-zero.
        TimeoutError: if the server does not become healthy within 600 seconds.
    """
    subprocess.run([str(_engine_script(engine)), "start"], check=True)
    _wait_for_health(_engine_url(engine), timeout_s=600)


def _stop_engine(engine: str) -> None:
    """Stop the engine container. Errors are silently ignored.

    Args:
        engine: Engine name (e.g., "vllm").
    """
    subprocess.run([str(_engine_script(engine)), "stop"], check=False)


def _run_prefill_battery(engine: str, num_repeats: int) -> None:
    """Run the full prefill benchmark matrix for one engine.

    Iterates over all PREFILL_INPUT_LENS and all repeat indices.

    Args:
        engine:      Engine label.
        num_repeats: Number of times to repeat each configuration.
    """
    url = _engine_url(engine)
    for input_len in config.PREFILL_INPUT_LENS:
        for run_id in range(1, num_repeats + 1):
            print(f"  [{engine}] prefill input={input_len} run={run_id}")
            run_prefill(engine, url, input_len, run_id)


def _run_decode_battery(engine: str, num_repeats: int) -> None:
    """Run the full decode benchmark matrix for one engine.

    Iterates over all DECODE_CONCURRENCIES and all repeat indices.

    Args:
        engine:      Engine label.
        num_repeats: Number of times to repeat each configuration.
    """
    url = _engine_url(engine)
    for conc in config.DECODE_CONCURRENCIES:
        for run_id in range(1, num_repeats + 1):
            print(f"  [{engine}] decode concurrency={conc} run={run_id}")
            run_decode(engine, url, conc, run_id)


def _ensure_docker_installed(store: StateStore) -> None:
    """Check Docker + NVIDIA Container Toolkit are available; exit with hint if not.

    Sets the "docker_installed" sentinel on success.

    Args:
        store: StateStore to check/set the sentinel.
    """
    if store.is_done("docker_installed"):
        return
    if shutil.which("docker") is None:
        print("Docker not installed. Run benchmark/setup_docker.sh first "
              "(requires sudo). Aborting.")
        sys.exit(1)
    # Verify NVIDIA runtime is wired up
    r = subprocess.run(["docker", "info", "--format", "{{.Runtimes}}"],
                        capture_output=True, text=True, check=True)
    if "nvidia" not in r.stdout:
        print("Docker is installed but NVIDIA Container Toolkit is not configured. "
              "Run benchmark/setup_docker.sh. Aborting.")
        sys.exit(1)
    store.mark_done("docker_installed")


def _ensure_model_downloaded(store: StateStore) -> None:
    """Check model weights exist at config.MODEL_DIR; exit with hint if not.

    Sets the "model_downloaded" sentinel on success.

    Args:
        store: StateStore to check/set the sentinel.
    """
    if store.is_done("model_downloaded"):
        return
    if not config.MODEL_DIR.exists() or not any(config.MODEL_DIR.iterdir()):
        print(f"Model not found at {config.MODEL_DIR}. Run "
              f"benchmark/download_model.sh. Aborting.")
        sys.exit(1)
    store.mark_done("model_downloaded")


def _ensure_image_pulled(engine: str, store: StateStore) -> None:
    """No-op stub — the image pull sentinel is set inside _run_engine_phases.

    The launch script pulls the image lazily on first start. Recording the
    sentinel before a successful start would poison it on failures; therefore
    the sentinel is set only after _start_engine() returns successfully.

    Args:
        engine: Engine name.
        store:  StateStore (unused here; present for signature symmetry).
    """
    # Intentional no-op: sentinel set inside _run_engine_phases after start.


def _run_engine_phases(engine: str, store: StateStore, num_repeats: int) -> None:
    """Run prefill + decode batteries for one engine, with sentinel guards.

    Starts the engine, runs batteries that are not yet marked done, stops
    the engine between batteries for a clean KV-cache state, and always
    stops the engine in a finally block.

    Args:
        engine:      Engine label.
        store:       StateStore for reading/writing sentinels.
        num_repeats: Number of times to repeat each benchmark configuration.
    """
    prefill_done = store.is_done(f"{engine}_prefill_done")
    decode_done = store.is_done(f"{engine}_decode_done")
    if prefill_done and decode_done:
        return
    print(f"=== {engine.upper()} ===")
    _start_engine(engine)
    store.mark_done(f"{engine}_image_pulled")
    try:
        if not prefill_done:
            _run_prefill_battery(engine, num_repeats)
            store.mark_done(f"{engine}_prefill_done")
            # Restart between batteries for clean KV-cache state
            _stop_engine(engine)
            _start_engine(engine)
        if not decode_done:
            _run_decode_battery(engine, num_repeats)
            store.mark_done(f"{engine}_decode_done")
    finally:
        _stop_engine(engine)


def _aggregate_and_plot(store: StateStore) -> None:
    """Aggregate JSON results into summary CSV and generate all plots.

    Each phase is individually sentinel-gated so the pipeline can resume
    if it was interrupted after aggregation but before plotting.

    Args:
        store: StateStore for reading/writing sentinels.
    """
    if not store.is_done("aggregated"):
        aggregate_directory(config.RESULTS_DIR, config.SUMMARY_CSV)
        store.mark_done("aggregated")
    if not store.is_done("plotted"):
        generate_all_plots(config.SUMMARY_CSV, config.PLOTS_DIR)
        store.mark_done("plotted")


def main(argv: list[str] | None = None) -> int:
    """Entry point for the pipeline orchestrator.

    Parses CLI arguments, clears any requested sentinels, then either prints
    a dry-run plan or executes the full pipeline.

    Args:
        argv: Argument list (defaults to sys.argv[1:] when None).

    Returns:
        Exit code (0 on success).
    """
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--engines", nargs="+", default=list(config.ENGINES),
                        choices=list(config.ENGINES))
    parser.add_argument("--num-repeats", type=int, default=config.NUM_REPEATS)
    parser.add_argument("--rerun", action="append", default=[],
                        help="Sentinel name(s) to clear before running")
    args = parser.parse_args(argv)

    store = StateStore(config.STATE_DIR)
    for s in args.rerun:
        store.clear(s)

    if args.dry_run:
        dry_run(args.engines, args.num_repeats)
        return 0

    _ensure_docker_installed(store)
    _ensure_model_downloaded(store)
    for engine in args.engines:
        _run_engine_phases(engine, store, args.num_repeats)
    _aggregate_and_plot(store)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
