"""Tests for deterministic pytest file partitioning."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from ops.ci.partition_pytest_files import (
    PartitionError,
    Shard,
    build_file_weights,
    discover_test_files,
    load_duration_history,
    partition_test_files,
    validate_partitions,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
PARTITIONER = REPO_ROOT / "ops/ci/partition_pytest_files.py"


def _write_fixture_repo(repo: Path) -> tuple[str, ...]:
    (repo / "tests/unit").mkdir(parents=True)
    (repo / "distributions/example/tests").mkdir(parents=True)
    (repo / "tests/unit/test_alpha.py").touch()
    (repo / "tests/unit/test_beta.py").touch()
    (repo / "tests/unit/helper.py").touch()
    (repo / "distributions/example/tests/test_overlay.py").touch()
    (repo / "pyproject.toml").write_text(
        """
[tool.pytest.ini_options]
testpaths = [
  "tests",
  "distributions",
]
""".lstrip(),
        encoding="utf-8",
    )
    return (
        "distributions/example/tests/test_overlay.py",
        "tests/unit/test_alpha.py",
        "tests/unit/test_beta.py",
    )


def test_discovers_all_configured_testpaths(tmp_path: Path) -> None:
    expected = _write_fixture_repo(tmp_path)

    discovered = discover_test_files(tmp_path, tmp_path / "pyproject.toml")

    assert discovered == expected


def test_rejects_configuration_that_omits_required_test_root(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
        encoding="utf-8",
    )

    with pytest.raises(PartitionError, match="distributions"):
        discover_test_files(tmp_path, tmp_path / "pyproject.toml")


def test_lpt_partition_is_deterministic_balanced_and_complete() -> None:
    files = tuple(f"tests/test_{letter}.py" for letter in "abcdef")
    durations = dict(zip(files, (9.0, 8.0, 7.0, 6.0, 5.0, 4.0), strict=True))

    first, _ = partition_test_files(files, 3, durations)
    second, _ = partition_test_files(tuple(reversed(files)), 3, durations)

    assert first == second
    assert [shard.estimated_seconds for shard in first] == [13.0, 13.0, 13.0]
    assert {path for shard in first for path in shard.files} == set(files)


def test_unknown_files_use_nearest_rank_p75() -> None:
    files = tuple(f"tests/test_{letter}.py" for letter in "abcde")
    durations = dict(zip(files[:4], (1.0, 2.0, 3.0, 100.0), strict=True))

    weights, unknown_weight = build_file_weights(files, durations)

    assert unknown_weight == 3.0
    assert weights[files[-1]] == 3.0


@pytest.mark.parametrize(
    "contents",
    ["{not-json", json.dumps({"version": 99, "durations": {}})],
)
def test_corrupt_duration_history_falls_back_safely(tmp_path: Path, contents: str) -> None:
    duration_path = tmp_path / "durations.json"
    duration_path.write_text(contents, encoding="utf-8")

    durations, warning = load_duration_history(duration_path)

    assert durations == {}
    assert warning is not None
    shards, unknown_weight = partition_test_files(
        ("tests/test_a.py", "tests/test_b.py"),
        2,
        durations,
    )
    assert unknown_weight == 1.0
    assert {path for shard in shards for path in shard.files} == {
        "tests/test_a.py",
        "tests/test_b.py",
    }


def test_partition_validation_detects_duplicates_and_missing_files() -> None:
    shards = (
        Shard(files=("tests/test_a.py",), estimated_seconds=1.0),
        Shard(files=("tests/test_a.py",), estimated_seconds=1.0),
    )

    with pytest.raises(PartitionError, match=r"duplicate.*missing"):
        validate_partitions(("tests/test_a.py", "tests/test_b.py"), shards)


def test_cli_writes_each_file_exactly_once_across_shards(tmp_path: Path) -> None:
    expected = set(_write_fixture_repo(tmp_path))
    duration_path = tmp_path / "durations.json"
    duration_path.write_text(
        json.dumps(
            {
                "version": 1,
                "durations": {
                    "tests/unit/test_alpha.py": 10,
                    "tests/unit/test_beta.py": 2,
                },
            }
        ),
        encoding="utf-8",
    )

    assigned: list[str] = []
    for shard_index in (1, 2):
        output = tmp_path / f"shard-{shard_index}.txt"
        result = subprocess.run(
            [
                sys.executable,
                str(PARTITIONER),
                "--repo-root",
                str(tmp_path),
                "--shard-index",
                str(shard_index),
                "--shard-count",
                "2",
                "--durations",
                str(duration_path),
                "--output",
                str(output),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert f"shard {shard_index}/2" in result.stderr
        assigned.extend(output.read_text(encoding="utf-8").splitlines())

    assert set(assigned) == expected
    assert len(assigned) == len(set(assigned))


@pytest.mark.parametrize(
    ("shard_index", "shard_count"),
    [(0, 2), (3, 2), (1, 0)],
)
def test_cli_rejects_invalid_shard_arguments(
    tmp_path: Path,
    shard_index: int,
    shard_count: int,
) -> None:
    _write_fixture_repo(tmp_path)

    result = subprocess.run(
        [
            sys.executable,
            str(PARTITIONER),
            "--repo-root",
            str(tmp_path),
            "--shard-index",
            str(shard_index),
            "--shard-count",
            str(shard_count),
            "--output",
            str(tmp_path / "shard.txt"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "error:" in result.stderr
