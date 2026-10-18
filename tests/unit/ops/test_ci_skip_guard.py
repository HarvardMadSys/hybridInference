from __future__ import annotations

import subprocess
import sys
import tempfile
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


def test_skip_checker_rejects_skip_reported_by_real_pytest(tmp_path: Path) -> None:
    repo_root = Path(__file__).parents[3]
    with tempfile.TemporaryDirectory(prefix=".pytest-skip-guard-", dir=repo_root) as directory:
        fixture = Path(directory) / "test_postgres_skip_probe.py"
        fixture.write_text(
            "import pytest\n"
            "pytestmark = pytest.mark.dbtest\n\n"
            "@pytest.fixture\n"
            "def postgres_database():\n"
            "    pytest.skip('PostgreSQL database does not exist')\n\n"
            "def test_database_probe(postgres_database):\n"
            "    raise AssertionError('the skipped test body must not run')\n"
        )
        pytest_result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-rs",
                "-m",
                "not external and dbtest",
                str(fixture),
            ],
            check=False,
            capture_output=True,
            text=True,
            cwd=repo_root,
        )

    pytest_output = pytest_result.stdout + pytest_result.stderr
    assert pytest_result.returncode == 0, pytest_output
    assert "SKIPPED" in pytest_output
    assert "PostgreSQL database does not exist" in pytest_output

    checker_result = _run_checker(tmp_path, pytest_output)
    assert checker_result.returncode == 1
    assert "Unexpected PostgreSQL/database test skip" in checker_result.stderr


def test_skip_checker_accepts_log_without_database_skip(tmp_path: Path) -> None:
    result = _run_checker(tmp_path, "2 passed in 0.10s\n")

    assert result.returncode == 0
    assert result.stderr == ""
