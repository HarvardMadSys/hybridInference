"""Tests for merging pytest shard duration manifests."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from ops.ci.merge_pytest_durations import MergeError, merge_duration_files

REPO_ROOT = Path(__file__).resolve().parents[3]
MERGER = REPO_ROOT / "ops/ci/merge_pytest_durations.py"


def _write_manifest(path: Path, durations: dict[str, object], version: int = 1) -> None:
    path.write_text(
        json.dumps({"version": version, "durations": durations}),
        encoding="utf-8",
    )


def test_merges_disjoint_shards_in_deterministic_path_order(tmp_path: Path) -> None:
    second = tmp_path / "shard-2.json"
    first = tmp_path / "shard-1.json"
    _write_manifest(first, {"tests/test_b.py": 2.0})
    _write_manifest(second, {"distributions/example/tests/test_a.py": 1.0})

    merged = merge_duration_files((second, first))

    assert list(merged["durations"]) == [
        "distributions/example/tests/test_a.py",
        "tests/test_b.py",
    ]


def test_rejects_overlapping_shards(tmp_path: Path) -> None:
    first = tmp_path / "shard-1.json"
    second = tmp_path / "shard-2.json"
    _write_manifest(first, {"tests/test_a.py": 1.0})
    _write_manifest(second, {"tests/test_a.py": 2.0})

    with pytest.raises(MergeError, match="overlap"):
        merge_duration_files((first, second))


@pytest.mark.parametrize(
    ("durations", "message"),
    [
        ({"../outside.py": 1.0}, "stay within"),
        ({r"C:\outside.py": 1.0}, "stay within"),
        ({"tests/test_a.py": 0}, "non-positive"),
        ({"tests/test_a.py": "slow"}, "invalid entry"),
        ({"tests/test_a.py": "1.0"}, "invalid entry"),
    ],
)
def test_rejects_invalid_duration_entries(
    tmp_path: Path,
    durations: dict[str, object],
    message: str,
) -> None:
    shard = tmp_path / "shard.json"
    _write_manifest(shard, durations)

    with pytest.raises(MergeError, match=message):
        merge_duration_files((shard,))


def test_rejects_unknown_schema_version(tmp_path: Path) -> None:
    shard = tmp_path / "shard.json"
    _write_manifest(shard, {"tests/test_a.py": 1.0}, version=2)

    with pytest.raises(MergeError, match="version 1"):
        merge_duration_files((shard,))


def test_cli_writes_merged_candidate(tmp_path: Path) -> None:
    first = tmp_path / "shard-1.json"
    second = tmp_path / "shard-2.json"
    output = tmp_path / "candidate.json"
    _write_manifest(first, {"tests/test_a.py": 1.25})
    _write_manifest(second, {"tests/test_b.py": 2.5})

    result = subprocess.run(
        [
            sys.executable,
            str(MERGER),
            str(first),
            str(second),
            "--output",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(output.read_text(encoding="utf-8")) == {
        "version": 1,
        "durations": {
            "tests/test_a.py": 1.25,
            "tests/test_b.py": 2.5,
        },
    }
