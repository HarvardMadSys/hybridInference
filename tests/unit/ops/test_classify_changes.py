"""Tests for the conservative CI change classifier."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from ops.ci.classify_changes import (
    Classification,
    classify,
    compute_changed_files,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
CLASSIFIER = REPO_ROOT / "ops/ci/classify_changes.py"


def _true_categories(result: Classification) -> set[str]:
    return {name for name, value in result.as_outputs().items() if value == "true"}


def test_frontend_only_change() -> None:
    result = classify(["apps/frontend/src/app/page.tsx"])
    assert _true_categories(result) == {"frontend"}
    assert result.docker_matrix() == ["frontend"]


def test_backend_sources_and_tests_map_to_backend() -> None:
    source = classify(["apps/backend/routing/routers.py"])
    tests = classify(["tests/unit/test_router.py"])

    assert _true_categories(source) == {"backend", "python_tests"}
    assert source.docker_matrix() == ["backend"]
    assert _true_categories(tests) == {"backend", "python_tests"}
    assert tests.docker_matrix() == []


def test_oncall_source_also_triggers_backend_tests() -> None:
    result = classify(["apps/backend/serving/oncall/app.py"])
    assert _true_categories(result) == {"oncall", "backend", "python_tests"}
    assert result.docker_matrix() == ["backend", "oncall"]


def test_oncall_dockerfile_is_oncall_only() -> None:
    result = classify(["deploy/docker/Dockerfile.oncall"])
    assert _true_categories(result) == {"oncall", "python_tests"}
    assert result.docker_matrix() == ["oncall"]


def test_shared_serving_change_triggers_oncall_and_backend() -> None:
    # Dockerfile.oncall COPYs the whole apps/backend/serving tree, so shared
    # serving code (not just serving/oncall) is baked into the on-call image.
    result = classify(["apps/backend/serving/config/settings.py"])
    assert _true_categories(result) == {"oncall", "backend", "python_tests"}
    assert result.docker_matrix() == ["backend", "oncall"]


def test_status_monitor_change() -> None:
    result = classify(["services/status-monitor-worker/main.py"])
    assert _true_categories(result) == {"status_monitor", "python_tests"}
    assert result.docker_matrix() == []


def test_alert_control_plane_change() -> None:
    result = classify(["services/alert-control-plane-worker/src/index.ts"])
    assert _true_categories(result) == {"alert_control_plane", "python_tests"}
    assert result.docker_matrix() == []


def test_docker_shared_change() -> None:
    for path in (".dockerignore", "deploy/docker/docker-compose.yml"):
        result = classify([path])
        assert _true_categories(result) == {"docker_shared", "python_tests"}
        assert result.docker_matrix() == ["frontend", "backend", "oncall"]


@pytest.mark.parametrize(
    ("path", "category", "matrix"),
    [
        ("deploy/docker/Dockerfile.frontend", "frontend", ["frontend"]),
        ("deploy/docker/Dockerfile.backend", "backend", ["backend"]),
        ("deploy/docker/Dockerfile.oncall", "oncall", ["oncall"]),
    ],
)
def test_image_specific_dockerfile_change(path: str, category: str, matrix: list[str]) -> None:
    result = classify([path])
    assert _true_categories(result) == {category, "python_tests"}
    assert result.docker_matrix() == matrix


@pytest.mark.parametrize(
    "path",
    [
        "pyproject.toml",
        "uv.lock",
        "Makefile",
        "README.md",  # repo-root README is a Dockerfile COPY input, not docs
        ".github/workflows/ci.yml",
        "config/models.yaml",
        "distributions/freeinference/overlay.yaml",
    ],
)
def test_full_triggers(path: str) -> None:
    result = classify([path])
    assert result.full is True
    assert result.python_tests is True
    assert "full" in _true_categories(result)
    assert result.docker_matrix() == ["frontend", "backend", "oncall"]


@pytest.mark.parametrize(
    "path",
    [
        "docs/developer/architecture.md",
        "apps/frontend/README.md",  # non-root markdown is documentation
        "LICENSE",
        "some/deep/NOTES.md",
    ],
)
def test_docs_only_changes_are_security_only(path: str) -> None:
    result = classify([path])
    assert _true_categories(result) == {"security_only"}
    assert result.full is False
    assert result.docker_matrix() == []


def test_unknown_path_forces_full() -> None:
    result = classify(["ops/ci/partition_pytest_files.py"])
    assert result.full is True
    assert "unknown paths force full" in result.reason


def test_unknown_path_still_reports_recognized_narrow_categories() -> None:
    result = classify(["apps/frontend/src/app/page.tsx", ".gitignore"])
    assert result.full is True  # .gitignore is unrecognized
    assert result.frontend is True  # but the frontend signal is still surfaced
    assert result.security_only is False


def test_none_diff_forces_full() -> None:
    result = classify(None)
    assert result.full is True
    assert result.python_tests is True
    assert "diff unavailable" in result.reason


def test_empty_diff_forces_full() -> None:
    result = classify([])
    assert result.full is True
    assert result.python_tests is True
    assert "empty diff" in result.reason


def test_rename_considers_both_old_and_new_paths() -> None:
    # A rename surfaces (via --no-renames) as old + new; classification must
    # reflect both endpoints.
    result = classify(["apps/frontend/old.tsx", "apps/backend/new.py"])
    assert result.frontend is True
    assert result.backend is True


def test_mixed_images_use_stable_matrix_order() -> None:
    result = classify(["apps/backend/serving/config/settings.py", "apps/frontend/src/app/page.tsx"])
    assert result.docker_matrix() == ["frontend", "backend", "oncall"]


def test_path_normalization_strips_leading_dot_slash() -> None:
    assert _true_categories(classify(["./apps/frontend/src/x.ts"])) == {"frontend"}


def test_cli_writes_outputs_and_json(tmp_path: Path) -> None:
    changed = tmp_path / "changed.txt"
    changed.write_text("apps/frontend/src/x.tsx\n", encoding="utf-8")
    output = tmp_path / "github_output.txt"

    result = subprocess.run(
        [
            sys.executable,
            str(CLASSIFIER),
            "--changed-files-file",
            str(changed),
            "--github-output",
            str(output),
            "--print-json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["frontend"] == "true"
    assert payload["full"] == "false"
    assert json.loads(payload["docker_matrix"]) == ["frontend"]

    written = dict(
        line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines() if line
    )
    assert written["frontend"] == "true"
    assert written["security_only"] == "false"
    assert written["docker_matrix"] == '["frontend"]'


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _init_repo(repo: Path) -> None:
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "ci@example.com")
    _git(repo, "config", "user.name", "CI")


def _rev(repo: Path, ref: str = "HEAD") -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", ref],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_compute_changed_files_push_range(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "apps/backend").mkdir(parents=True)
    (tmp_path / "apps/backend/base.py").write_text("x = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    before = _rev(tmp_path)

    (tmp_path / "apps/frontend").mkdir(parents=True)
    (tmp_path / "apps/frontend/page.tsx").write_text("export {}\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "change")
    head = _rev(tmp_path)

    files = compute_changed_files(tmp_path, "push", None, None, before, head)
    assert files == ["apps/frontend/page.tsx"]
    assert _true_categories(classify(files)) == {"frontend"}


def test_compute_changed_files_zero_base_returns_none(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "file.py").write_text("x = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")

    files = compute_changed_files(tmp_path, "push", None, None, "0" * 40, _rev(tmp_path))
    assert files is None  # first push / unknown base -> caller forces full


def test_push_endpoint_diff_sees_files_dropped_by_force_push(tmp_path: Path) -> None:
    # Regression: a force / non-fast-forward push must be diffed endpoint-to-
    # endpoint (before..sha), not three-dot (merge-base..sha). A diverged new
    # tip that drops the old tip's backend file would otherwise look
    # frontend-only.
    _init_repo(tmp_path)
    (tmp_path / "base.txt").write_text("base\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "common ancestor")
    ancestor = _rev(tmp_path)

    # Old tip: adds a backend file.
    (tmp_path / "apps/backend").mkdir(parents=True)
    (tmp_path / "apps/backend/service.py").write_text("x = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "old tip backend")
    before = _rev(tmp_path)

    # New tip diverges from the common ancestor and adds only a frontend file.
    _git(tmp_path, "checkout", "-q", "-b", "newtip", ancestor)
    (tmp_path / "apps/frontend").mkdir(parents=True)
    (tmp_path / "apps/frontend/page.tsx").write_text("export {}\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "new tip frontend")
    sha = _rev(tmp_path)

    files = compute_changed_files(tmp_path, "push", None, None, before, sha)
    assert files is not None
    result = classify(files)
    # Endpoint diff reveals both the removed backend file and the added frontend
    # file; three-dot would have missed the backend deletion.
    assert result.backend is True
    assert result.frontend is True
