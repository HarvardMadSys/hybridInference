"""Operational actions keep migrations, acceptance and recovery boundaries explicit."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from ops.admin import gateway_ops as ops


def arguments(tmp_path, **overrides):
    values = {
        "compose": ["docker", "compose", "-f", "base.yml"],
        "backend_image": "registry/backend@sha256:" + "a" * 64,
        "frontend_image": "registry/frontend@sha256:" + "b" * 64,
        "base_url": "http://localhost:3001",
        "model": "one",
        "timeout": 0,
        "state": tmp_path / "release",
        "backup": tmp_path / "backup.dump",
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_acceptance_failure_restores_previous_images_and_persists_selection(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    args = arguments(tmp_path)
    old = {"backend": "sha256:" + "c" * 64, "frontend": "sha256:" + "d" * 64}
    monkeypatch.setattr(ops, "_running_images", lambda _: old)
    calls = []
    monkeypatch.setattr(ops, "command", lambda argv, **kw: calls.append((argv, kw)))
    monkeypatch.setattr(ops, "_wait", Mock(side_effect=ValueError("not ready")))
    with pytest.raises(ValueError, match="not ready"):
        ops.deploy(args)
    migration = next(i for i, (argv, _) in enumerate(calls) if "upgrade" in argv)
    switches = [(i, kw) for i, (argv, kw) in enumerate(calls) if "--no-build" in argv]
    assert len(switches) == 2
    assert migration < switches[0][0] < switches[1][0]
    assert switches[-1][1]["env"]["BACKEND_IMAGE"] == old["backend"]
    assert old["frontend"] in Path(".ops/images.env").read_text()
    assert json.loads((args.state / "previous.json").read_text()) == old


def test_migration_failure_never_switches_running_services(tmp_path, monkeypatch):
    args = arguments(tmp_path)
    monkeypatch.setattr(ops, "_running_images", lambda _: {"backend": "old", "frontend": "old"})

    def run(argv, **kwargs):
        if "upgrade" in argv:
            raise ValueError("migration failed")

    monkeypatch.setattr(ops, "command", run)
    switch = Mock()
    monkeypatch.setattr(ops, "_switch", switch)
    with pytest.raises(ValueError, match="migration failed"):
        ops.deploy(args)
    switch.assert_not_called()


def test_deploy_refuses_mutable_image_tags_before_any_command(tmp_path, monkeypatch):
    run = Mock()
    monkeypatch.setattr(ops, "command", run)
    with pytest.raises(ValueError, match="sha256"):
        ops.deploy(arguments(tmp_path, backend_image="registry/backend:latest"))
    run.assert_not_called()


def test_restore_check_drops_only_its_generated_scratch_database_even_on_failure(
    tmp_path, monkeypatch
):
    args = arguments(tmp_path)
    args.backup.write_bytes(b"private dump")
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if any("pg_restore" in token for token in argv):
            raise ValueError("broken dump")

    monkeypatch.setattr(ops, "command", run)
    with pytest.raises(ValueError, match="broken dump"):
        ops.restore_check(args)
    name = calls[0][-1]
    assert name.startswith("gateway_restore_")
    assert calls[-1][-1] == name
    assert any("dropdb" in token for token in calls[-1])


def test_backup_is_private_and_uses_no_tty(tmp_path, monkeypatch):
    args = arguments(tmp_path)
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        kwargs["stdout"].write(b"dump")

    monkeypatch.setattr(ops, "command", run)
    ops.backup(args)
    assert args.backup.stat().st_mode & 0o777 == 0o600
    assert "-T" in calls[0] and "-t" not in calls[0]
    with pytest.raises(FileExistsError):
        ops.backup(args)


def test_smoke_refuses_incomplete_configuration_before_inference(monkeypatch):
    run = Mock(side_effect=[{"status": "healthy"}, {"configuration": {"incomplete": True}}])
    monkeypatch.setattr(ops, "request", run)
    with pytest.raises(ValueError, match="Ask admin to configure"):
        ops.smoke("http://localhost:3001", "one")
    assert run.call_count == 2


def test_compose_output_accepts_arrays_and_json_lines():
    assert ops._records('[{"Service":"backend"}]') == [{"Service": "backend"}]
    assert len(ops._records('{"Service":"backend"}\n{"Service":"frontend"}\n')) == 2


def test_failed_backup_removes_partial_dump(tmp_path, monkeypatch):
    args = arguments(tmp_path)

    def run(argv, **kwargs):
        kwargs["stdout"].write(b"incomplete")
        raise ValueError("database unavailable")

    monkeypatch.setattr(ops, "command", run)
    with pytest.raises(ValueError, match="database unavailable"):
        ops.backup(args)
    assert not args.backup.exists()


def test_saved_configuration_does_not_claim_running_values_are_applied(tmp_path, monkeypatch):
    monkeypatch.delenv("ADMIN_ACCESS_TOKEN", raising=False)
    monkeypatch.setattr(
        ops,
        "command",
        Mock(return_value=argparse.Namespace(stdout='{"pending_restart": []}')),
    )
    status = ops.configuration_status(arguments(tmp_path))
    assert status["view"] == "saved configuration"
    assert status["pending_restart"].startswith("not checked")


def test_configuration_status_reads_applied_state_without_disclosing_values(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_ACCESS_TOKEN", "test-access-token")
    request = Mock(
        return_value={
            "entries": [{"key": "SECRET_SETTING", "value": "private", "invalid": True}],
            "pending_restart": ["MODEL_CATALOG"],
            "missing": [],
        }
    )
    monkeypatch.setattr(ops, "request", request)
    status = ops.configuration_status(arguments(tmp_path))
    assert status["pending_restart"] == ["MODEL_CATALOG"]
    assert status["invalid"] == ["SECRET_SETTING"]
    assert "private" not in json.dumps(status)
    request.assert_called_once_with(
        "http://localhost:3001", "/admin/config", token="test-access-token"
    )
