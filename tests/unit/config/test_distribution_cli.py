"""CLI contract tests for detached distribution validation."""

from __future__ import annotations

import json

from serving.config.distribution import render_distribution_bundle_lock
from serving.config.distribution_cli import ValidationExitCode, main


def _write_contract(root):
    (root / "environment.yaml").write_text("environment_schema_version: 1\nvariables: []\n")


def _write_runtime(root, *, gateway: str = "  gateway: {}\n"):
    runtime = root / "runtime.yaml"
    runtime.write_text(
        """\
schema_version: 2
distribution:
  id: detached
resources:
"""
        + gateway
        + """\
  rag: {}
environment_contract: environment.yaml
"""
    )
    return runtime


def test_runtime_cli_validates_a_detached_root(tmp_path, capsys):
    _write_contract(tmp_path)
    _write_runtime(tmp_path)

    result = main(
        [
            "runtime-validate",
            "runtime.yaml",
            "--root",
            str(tmp_path),
            "--strict",
        ]
    )

    assert result == ValidationExitCode.OK
    assert json.loads(capsys.readouterr().out) == {
        "artifact": "runtime",
        "distribution_id": "detached",
        "schema_version": 2,
        "status": "valid",
        "strict": True,
    }


def test_runtime_cli_schema_error_has_stable_exit_code(tmp_path, capsys):
    runtime = tmp_path / "runtime.yaml"
    runtime.write_text("schema_version: 1\ndistribution:\n  id: legacy\n")

    result = main(["runtime-validate", "runtime.yaml", "--root", str(tmp_path), "--strict"])

    assert result == ValidationExitCode.SCHEMA
    assert json.loads(capsys.readouterr().err)["category"] == "schema"


def test_runtime_cli_schema_error_does_not_echo_input_values(tmp_path, capsys):
    canary = "sk-audit-do-not-log"
    runtime = tmp_path / "runtime.yaml"
    runtime.write_text(
        "schema_version: 2\n"
        "distribution:\n  id: detached\n"
        f"accidental_secret: {canary}\n"
        "environment_contract: environment.yaml\n"
    )

    result = main(["runtime-validate", "runtime.yaml", "--root", str(tmp_path), "--strict"])

    assert result == ValidationExitCode.SCHEMA
    error_text = capsys.readouterr().err
    assert canary not in error_text
    assert "accidental_secret" in error_text


def test_runtime_cli_path_escape_has_stable_exit_code(tmp_path, capsys):
    outside = tmp_path.parent / "outside-runtime.yaml"
    outside.write_text("schema_version: 1\ndistribution:\n  id: outside\n")

    result = main(
        [
            "runtime-validate",
            "../outside-runtime.yaml",
            "--root",
            str(tmp_path),
            "--strict",
        ]
    )

    assert result == ValidationExitCode.PATH
    assert json.loads(capsys.readouterr().err)["category"] == "path"


def test_runtime_cli_semantic_error_has_stable_exit_code(tmp_path, capsys):
    _write_contract(tmp_path)
    (tmp_path / "models.yaml").write_text("models:\n  - invalid scalar\n")
    _write_runtime(tmp_path, gateway="  gateway:\n    models: models.yaml\n")

    result = main(["runtime-validate", "runtime.yaml", "--root", str(tmp_path), "--strict"])

    assert result == ValidationExitCode.SEMANTIC
    assert json.loads(capsys.readouterr().err)["category"] == "semantic"


def test_runtime_cli_environment_error_has_stable_exit_code(tmp_path, capsys):
    _write_contract(tmp_path)
    (tmp_path / "models.yaml").write_text("models: []\nvalidator_reference: ${UNDECLARED_TOKEN}\n")
    _write_runtime(tmp_path, gateway="  gateway:\n    models: models.yaml\n")

    result = main(["runtime-validate", "runtime.yaml", "--root", str(tmp_path), "--strict"])

    assert result == ValidationExitCode.ENVIRONMENT
    error = json.loads(capsys.readouterr().err)
    assert error["category"] == "environment"
    assert "UNDECLARED_TOKEN" in error["error"]


def test_bundle_cli_validates_lock_and_reports_staleness(tmp_path, capsys):
    _write_contract(tmp_path)
    runtime = _write_runtime(tmp_path)
    bundle = tmp_path / "bundle.yaml"
    bundle.write_text(
        """\
bundle_schema_version: 1
runtime_manifest: runtime.yaml
environment_contract: environment.yaml
"""
    )
    (tmp_path / "bundle.lock.json").write_text(render_distribution_bundle_lock(bundle))

    valid = main(["bundle-validate", "bundle.yaml", "--root", str(tmp_path), "--strict"])
    assert valid == ValidationExitCode.OK
    assert json.loads(capsys.readouterr().out)["status"] == "valid"

    runtime.write_text(runtime.read_text().replace("id: detached", "id: changed"))
    stale = main(["bundle-validate", "bundle.yaml", "--root", str(tmp_path), "--strict"])

    assert stale == ValidationExitCode.LOCK
    assert json.loads(capsys.readouterr().err)["category"] == "lock"


def test_bundle_cli_schema_error_does_not_echo_input_values(tmp_path, capsys):
    canary = "sk-bundle-audit-do-not-log"
    bundle = tmp_path / "bundle.yaml"
    bundle.write_text(
        "bundle_schema_version: 1\n"
        "runtime_manifest: runtime.yaml\n"
        "environment_contract: environment.yaml\n"
        f"accidental_secret: {canary}\n"
    )

    result = main(["bundle-validate", "bundle.yaml", "--root", str(tmp_path), "--strict"])

    assert result == ValidationExitCode.SCHEMA
    error_text = capsys.readouterr().err
    assert canary not in error_text
    assert "accidental_secret" in error_text
