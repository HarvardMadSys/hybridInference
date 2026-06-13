"""Command-line entry point for the status monitor.

Usage::

    python -m status_monitor --config config.yml
    python -m status_monitor --config config.yml --run-once
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

import uvicorn

from status_monitor.app import create_app
from status_monitor.config import load_config
from status_monitor.scheduler import probe_once
from status_monitor.state import StatusStore


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parses command-line arguments."""
    parser = argparse.ArgumentParser(prog="status-monitor", description=__doc__)
    parser.add_argument("--config", required=True, help="Path to the YAML config file.")
    parser.add_argument(
        "--run-once",
        action="store_true",
        help="Probe every model once, print the results as JSON, and exit.",
    )
    parser.add_argument("--host", default="0.0.0.0", help="Bind host for the web server.")
    return parser.parse_args(argv)


def _run_once(config) -> int:  # noqa: ANN001 - AppConfig, kept import-light
    """Runs a single probe cycle and prints the snapshot as JSON."""
    store = StatusStore(history_size=config.settings.history_size)
    asyncio.run(probe_once(config, store))
    json.dump(store.snapshot(), sys.stdout, indent=2)
    sys.stdout.write("\n")
    snap = store.snapshot()
    return 0 if snap["total"] and not snap["unhealthy"] else 1


def main(argv: list[str] | None = None) -> int:
    """Loads config and either runs a single probe or serves the website.

    Returns:
        Process exit code.
    """
    args = _parse_args(argv)
    config = load_config(args.config)
    logging.basicConfig(
        level=getattr(logging, config.settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if args.run_once:
        return _run_once(config)

    app = create_app(config)
    uvicorn.run(app, host=args.host, port=config.settings.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
