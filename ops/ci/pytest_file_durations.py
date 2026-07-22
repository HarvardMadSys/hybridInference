"""Collect repository-relative pytest file durations for CI shard balancing."""

from __future__ import annotations

import json
import math
import os
import sys
import tempfile
from collections import defaultdict
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

OUTPUT_OPTION = "--pytest-file-durations-output"
OUTPUT_DEST = "pytest_file_durations_output"
OUTPUT_ENV = "PYTEST_FILE_DURATIONS_OUTPUT"
PLUGIN_NAME = "pytest-file-duration-collector"
REPORT_PHASES = frozenset({"setup", "call", "teardown"})


def pytest_addoption(parser: Any) -> None:
    """Register the optional duration output path."""
    group = parser.getgroup("CI timing")
    group.addoption(
        OUTPUT_OPTION,
        action="store",
        dest=OUTPUT_DEST,
        default=None,
        metavar="PATH",
        help=f"write per-file test durations as JSON (or set {OUTPUT_ENV})",
    )


def _emit_warning(config: Any, message: str) -> None:
    rendered = f"pytest file duration collection warning: {message}"
    try:
        terminal = config.pluginmanager.get_plugin("terminalreporter")
        if terminal is not None:
            terminal.write_line(f"WARNING: {rendered}", red=True)
            return
    except Exception:
        pass

    with suppress(Exception):
        print(f"WARNING: {rendered}", file=sys.stderr)


def _configured_output(config: Any) -> Path | None:
    configured = config.getoption(OUTPUT_DEST)
    if configured is None:
        configured = os.environ.get(OUTPUT_ENV)
    if configured is None or not str(configured).strip():
        return None

    output = Path(configured).expanduser()
    if not output.is_absolute():
        output = Path(config.rootpath) / output
    return output.resolve(strict=False)


def pytest_configure(config: Any) -> None:
    """Install one controller-side collector when an output path is configured."""
    if getattr(config, "workerinput", None) is not None:
        return

    try:
        output_path = _configured_output(config)
    except Exception as exc:
        _emit_warning(config, f"invalid output path: {exc}")
        return
    if output_path is None:
        return

    try:
        collector = PytestFileDurationCollector(config, output_path)
        config.pluginmanager.register(collector, PLUGIN_NAME)
    except Exception as exc:
        _emit_warning(config, f"cannot initialize collector for {output_path}: {exc}")


class PytestFileDurationCollector:
    """Aggregate runtest reports on the pytest controller and publish one JSON file."""

    def __init__(self, config: Any, output_path: Path) -> None:
        self._config = config
        self._repo_root = Path(config.rootpath).resolve()
        self._output_path = output_path
        self._durations: defaultdict[str, float] = defaultdict(float)
        self._reported_errors: set[str] = set()

    def _warn_once(self, key: str, message: str) -> None:
        if key in self._reported_errors:
            return
        self._reported_errors.add(key)
        _emit_warning(self._config, message)

    def _relative_report_path(self, report: Any) -> str | None:
        try:
            location = report.location
            raw_path = location[0]
            candidate = Path(raw_path)
            if not candidate.is_absolute():
                candidate = self._repo_root / candidate
            return candidate.resolve(strict=False).relative_to(self._repo_root).as_posix()
        except (AttributeError, IndexError, OSError, TypeError, ValueError) as exc:
            raw_path = getattr(report, "nodeid", repr(report))
            self._warn_once(
                f"report-path:{raw_path}",
                f"ignoring report with invalid repository path {raw_path!r}: {exc}",
            )
            return None

    def pytest_runtest_logreport(self, report: Any) -> None:
        """Add setup, call, and teardown time from one serialized test report."""
        if getattr(report, "when", None) not in REPORT_PHASES:
            return

        try:
            duration = float(report.duration)
        except (AttributeError, TypeError, ValueError) as exc:
            nodeid = getattr(report, "nodeid", repr(report))
            self._warn_once(
                f"report-duration:{nodeid}",
                f"ignoring report with invalid duration for {nodeid!r}: {exc}",
            )
            return
        if not math.isfinite(duration) or duration < 0:
            nodeid = getattr(report, "nodeid", repr(report))
            self._warn_once(
                f"report-duration:{nodeid}",
                f"ignoring non-finite or negative duration for {nodeid!r}: {duration!r}",
            )
            return

        relative_path = self._relative_report_path(report)
        if relative_path is not None:
            self._durations[relative_path] += duration

    def pytest_sessionfinish(self, session: Any, exitstatus: int) -> None:
        """Publish collected durations without changing the pytest result on failure."""
        del session, exitstatus
        try:
            write_duration_file(self._output_path, self._durations)
        except Exception as exc:
            self._warn_once(
                "write-output",
                f"cannot write {self._output_path}: {exc}",
            )


def write_duration_file(output_path: Path, durations: Mapping[str, float]) -> None:
    """Atomically write a version-1 duration manifest in deterministic order."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "durations": {path: round(duration, 6) for path, duration in sorted(durations.items())},
    }
    serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"

    descriptor, temporary_name = tempfile.mkstemp(
        dir=output_path.parent,
        prefix=f".{output_path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary_file:
            temporary_file.write(serialized)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, output_path)
    except Exception:
        with suppress(OSError):
            os.close(descriptor)
        with suppress(OSError):
            temporary_path.unlink()
        raise
