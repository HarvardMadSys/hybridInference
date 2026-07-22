"""Tests for aggregating pytest durations across successful CI runs."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from ops.ci.aggregate_pytest_durations import (
    AggregateError,
    aggregate_duration_files,
)
from ops.ci.merge_pytest_durations import MergeError

REPO_ROOT = Path(__file__).resolve().parents[3]
AGGREGATOR = REPO_ROOT / "ops/ci/aggregate_pytest_durations.py"


def _write_manifest(path: Path, durations: dict[str, object], version: object = 1) -> None:
    path.write_text(
        json.dumps({"version": version, "durations": durations}),
        encoding="utf-8",
    )


def _write_fixture_repo(repo: Path) -> tuple[str, ...]:
    (repo / "tests/unit").mkdir(parents=True)
    (repo / "distributions/example/tests").mkdir(parents=True)
    (repo / "tests/unit/test_alpha.py").touch()
    (repo / "tests/unit/test_beta.py").touch()
    (repo / "tests/unit/test_new.py").touch()
    (repo / "tests/unit/helper.py").touch()
    (repo / "distributions/example/tests/test_overlay.py").touch()
    (repo / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["tests", "distributions"]\n',
        encoding="utf-8",
    )
    return (
        "distributions/example/tests/test_overlay.py",
        "tests/unit/test_alpha.py",
        "tests/unit/test_beta.py",
        "tests/unit/test_new.py",
    )


def test_uses_per_file_median_in_deterministic_order(tmp_path: Path) -> None:
    first = tmp_path / "run-1.json"
    second = tmp_path / "run-2.json"
    third = tmp_path / "run-3.json"
    _write_manifest(first, {"tests/test_beta.py": 9.0, "tests/test_alpha.py": 1.0})
    _write_manifest(second, {"tests/test_alpha.py": 100.0, "tests/test_beta.py": 5.0})
    _write_manifest(third, {"tests/test_beta.py": 7.0, "tests/test_alpha.py": 3.0})

    candidate = aggregate_duration_files(
        (third, first, second),
        ("tests/test_beta.py", "tests/test_alpha.py"),
    )

    assert candidate == {
        "version": 1,
        "durations": {
            "tests/test_alpha.py": 3.0,
            "tests/test_beta.py": 7.0,
        },
    }


def test_allows_missing_samples_and_omits_new_unmeasured_files(tmp_path: Path) -> None:
    first = tmp_path / "run-1.json"
    second = tmp_path / "run-2.json"
    _write_manifest(first, {"tests/test_alpha.py": 2.0, "tests/test_beta.py": 4.0})
    _write_manifest(second, {"tests/test_alpha.py": 6.0})

    candidate = aggregate_duration_files(
        (first, second),
        ("tests/test_alpha.py", "tests/test_beta.py", "tests/test_new.py"),
    )

    assert candidate["durations"] == {
        "tests/test_alpha.py": 4.0,
        "tests/test_beta.py": 4.0,
    }


def test_removes_stale_test_paths(tmp_path: Path) -> None:
    run = tmp_path / "run.json"
    _write_manifest(
        run,
        {
            "tests/test_current.py": 2.0,
            "tests/test_removed.py": 90.0,
            "scripts/test_not_under_pytest_roots.py": 12.0,
        },
    )

    candidate = aggregate_duration_files((run,), ("tests/test_current.py",))

    assert candidate["durations"] == {"tests/test_current.py": 2.0}


@pytest.mark.parametrize(
    ("durations", "version", "message"),
    [
        ({"../outside.py": 1.0}, 1, "stay within"),
        ({r"C:\outside.py": 1.0}, 1, "stay within"),
        ({"tests/test_alpha.py": 0.0}, 1, "non-positive"),
        ({"tests/test_alpha.py": "1.0"}, 1, "invalid entry"),
        ({"tests/test_alpha.py": 1.0}, True, "version 1"),
        ({"tests/test_alpha.py": 1.0}, 2, "version 1"),
    ],
)
def test_rejects_invalid_input_fail_closed(
    tmp_path: Path,
    durations: dict[str, object],
    version: object,
    message: str,
) -> None:
    run = tmp_path / "run.json"
    _write_manifest(run, durations, version)

    with pytest.raises(MergeError, match=message):
        aggregate_duration_files((run,), ("tests/test_alpha.py",))


def test_rejects_inputs_without_current_test_timings(tmp_path: Path) -> None:
    run = tmp_path / "run.json"
    _write_manifest(run, {"tests/test_removed.py": 2.0})

    with pytest.raises(AggregateError, match="no timings for current"):
        aggregate_duration_files((run,), ("tests/test_current.py",))


def test_cli_discovers_current_tests_and_writes_candidate_atomically(tmp_path: Path) -> None:
    current = _write_fixture_repo(tmp_path)
    first = tmp_path / "run-1.json"
    second = tmp_path / "run-2.json"
    output = tmp_path / "artifacts/candidate.json"
    _write_manifest(
        first,
        {
            current[0]: 3.0,
            current[1]: 1.0,
            current[2]: 7.0,
            "tests/unit/test_removed.py": 99.0,
        },
    )
    _write_manifest(second, {current[0]: 5.0, current[1]: 9.0})

    result = subprocess.run(
        [
            sys.executable,
            str(AGGREGATOR),
            str(first),
            str(second),
            "--repo-root",
            str(tmp_path),
            "--output",
            str(output),
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(output.read_text(encoding="utf-8")) == {
        "version": 1,
        "durations": {
            current[0]: 4.0,
            current[1]: 5.0,
            current[2]: 7.0,
        },
    }
    assert "3 current pytest files from 2 successful CI runs" in result.stderr
    assert not list(output.parent.glob(f".{output.name}.*.tmp"))


def test_cli_failure_does_not_replace_existing_output(tmp_path: Path) -> None:
    _write_fixture_repo(tmp_path)
    invalid = tmp_path / "invalid.json"
    output = tmp_path / "candidate.json"
    invalid.write_text("{not json", encoding="utf-8")
    output.write_text("keep me\n", encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(AGGREGATOR),
            str(invalid),
            "--repo-root",
            str(tmp_path),
            "--output",
            str(output),
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "error:" in result.stderr
    assert output.read_text(encoding="utf-8") == "keep me\n"
