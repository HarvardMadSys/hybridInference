"""Tests for the controller-side pytest file duration collector."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pytest

from ops.ci.pytest_file_durations import (
    OUTPUT_DEST,
    OUTPUT_ENV,
    PytestFileDurationCollector,
    pytest_configure,
)

REPO_ROOT = Path(__file__).resolve().parents[3]


class _TerminalReporter:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def write_line(self, message: str, **kwargs: Any) -> None:
        del kwargs
        self.lines.append(message)


class _PluginManager:
    def __init__(self) -> None:
        self.terminal = _TerminalReporter()
        self.registered: list[tuple[object, str]] = []

    def get_plugin(self, name: str) -> object | None:
        return self.terminal if name == "terminalreporter" else None

    def register(self, plugin: object, name: str) -> None:
        self.registered.append((plugin, name))


class _Config:
    def __init__(self, rootpath: Path, output: str | None = None) -> None:
        self.rootpath = rootpath
        self.output = output
        self.pluginmanager = _PluginManager()

    def getoption(self, name: str) -> str | None:
        assert name == OUTPUT_DEST
        return self.output


def _report(path: str, when: str, duration: float, nodeid: str = "test") -> SimpleNamespace:
    return SimpleNamespace(
        location=(path, 1, nodeid),
        when=when,
        duration=duration,
        nodeid=f"{path}::{nodeid}",
    )


def test_aggregates_all_runtest_phases_by_repository_relative_file(tmp_path: Path) -> None:
    output = tmp_path / "artifacts/durations.json"
    config = _Config(tmp_path)
    collector = PytestFileDurationCollector(config, output)

    collector.pytest_runtest_logreport(_report("tests/test_alpha.py", "setup", 0.1))
    collector.pytest_runtest_logreport(_report("tests/test_alpha.py", "call", 1.2))
    collector.pytest_runtest_logreport(_report("tests/test_alpha.py", "teardown", 0.2))
    collector.pytest_runtest_logreport(_report("tests/test_beta.py", "call", 2.0))
    collector.pytest_sessionfinish(SimpleNamespace(), 0)

    assert json.loads(output.read_text(encoding="utf-8")) == {
        "version": 1,
        "durations": {
            "tests/test_alpha.py": 1.5,
            "tests/test_beta.py": 2.0,
        },
    }
    assert not list(output.parent.glob(f".{output.name}.*.tmp"))


def test_no_output_configuration_is_a_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(OUTPUT_ENV, raising=False)
    config = _Config(tmp_path)

    pytest_configure(config)

    assert config.pluginmanager.registered == []


def test_environment_output_registers_controller_collector(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(OUTPUT_ENV, "artifacts/durations.json")
    config = _Config(tmp_path)

    pytest_configure(config)

    [(collector, _)] = config.pluginmanager.registered
    assert isinstance(collector, PytestFileDurationCollector)
    collector.pytest_sessionfinish(SimpleNamespace(), 0)
    assert (tmp_path / "artifacts/durations.json").exists()


def test_xdist_worker_does_not_register_or_write(tmp_path: Path) -> None:
    output = tmp_path / "durations.json"
    config = _Config(tmp_path, str(output))
    config.workerinput = {"workerid": "gw0"}

    pytest_configure(config)

    assert config.pluginmanager.registered == []
    assert not output.exists()


def test_invalid_report_path_is_reported_and_ignored(tmp_path: Path) -> None:
    output = tmp_path / "durations.json"
    config = _Config(tmp_path)
    collector = PytestFileDurationCollector(config, output)

    collector.pytest_runtest_logreport(_report(str(tmp_path.parent / "outside.py"), "call", 1.0))
    collector.pytest_sessionfinish(SimpleNamespace(), 0)

    assert json.loads(output.read_text(encoding="utf-8"))["durations"] == {}
    assert any("invalid repository path" in line for line in config.pluginmanager.terminal.lines)


def test_output_failure_warns_without_raising(tmp_path: Path) -> None:
    output_directory = tmp_path / "durations.json"
    output_directory.mkdir()
    config = _Config(tmp_path)
    collector = PytestFileDurationCollector(config, output_directory)
    collector.pytest_runtest_logreport(_report("tests/test_alpha.py", "call", 1.0))

    collector.pytest_sessionfinish(SimpleNamespace(), 0)

    assert output_directory.is_dir()
    assert any("cannot write" in line for line in config.pluginmanager.terminal.lines)


def test_real_xdist_controller_writes_combined_manifest(tmp_path: Path) -> None:
    test_file = tmp_path / "test_sample.py"
    test_file.write_text(
        """
import time


def test_one():
    time.sleep(0.01)


def test_two():
    time.sleep(0.01)


def test_three():
    time.sleep(0.01)


def test_four():
    time.sleep(0.01)
""".lstrip(),
        encoding="utf-8",
    )
    output = tmp_path / "artifacts/durations.json"
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            filter(None, (str(REPO_ROOT), os.environ.get("PYTHONPATH", "")))
        ),
    }

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "ops.ci.pytest_file_durations",
            "-c",
            os.devnull,
            "--rootdir",
            str(tmp_path),
            "-n",
            "2",
            "--pytest-file-durations-output",
            str(output),
            str(test_file),
        ],
        cwd=tmp_path,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert list(payload["durations"]) == ["test_sample.py"]
    assert payload["durations"]["test_sample.py"] > 0.04
