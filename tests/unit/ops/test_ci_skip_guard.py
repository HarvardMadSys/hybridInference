from __future__ import annotations

import subprocess
from pathlib import Path

SCRIPT = Path(__file__).parents[3] / "ops" / "ci" / "check_pytest_skips.sh"
PATTERN = r"SKIPPED.*(PostgreSQL|database .*does not exist|TEST_PG_DSN|PG_TEST_DSN)"


def _run_checker(tmp_path: Path, contents: str) -> subprocess.CompletedProcess[str]:
    log_file = tmp_path / "pytest.log"
    log_file.write_text(contents)
    return subprocess.run(
        ["bash", str(SCRIPT), PATTERN, str(log_file)],
        check=False,
        capture_output=True,
        text=True,
    )


def test_skip_checker_rejects_unexpected_database_skip(tmp_path: Path) -> None:
    result = _run_checker(tmp_path, "SKIPPED test_x: PostgreSQL database does not exist\n")

    assert result.returncode == 1
    assert "Unexpected PostgreSQL/database test skip" in result.stderr


def test_skip_checker_accepts_log_without_database_skip(tmp_path: Path) -> None:
    result = _run_checker(tmp_path, "2 passed in 0.10s\n")

    assert result.returncode == 0
    assert result.stderr == ""
