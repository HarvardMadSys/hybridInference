"""Operational actions keep migrations, acceptance and recovery boundaries explicit."""

from __future__ import annotations

import argparse
import http.client
import json
import subprocess
import sys
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
        "legacy_manifest": "",
        "legacy_import_option": [],
    }
    values.update(overrides)
    return argparse.Namespace(**values)


OLD = {"backend": "sha256:" + "c" * 64, "frontend": "sha256:" + "d" * 64}


def _switches(calls):
    return [(i, kw) for i, (argv, kw) in enumerate(calls) if "--no-build" in argv]


def test_acceptance_failure_restores_previous_images_and_records_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    args = arguments(tmp_path)
    monkeypatch.setattr(ops, "_running_images", lambda _: OLD)
    calls = []
    monkeypatch.setattr(ops, "command", lambda argv, **kw: calls.append((argv, kw)))
    waits = Mock(side_effect=[ValueError("not ready"), None])
    monkeypatch.setattr(ops, "_wait", waits)
    with pytest.raises(ValueError, match=r"not ready\. The previous images are running again"):
        ops.deploy(args)
    migration = next(i for i, (argv, _) in enumerate(calls) if "upgrade" in argv)
    switches = _switches(calls)
    assert len(switches) == 2
    assert migration < switches[0][0] < switches[1][0]
    assert switches[-1][1]["env"]["BACKEND_IMAGE"] == OLD["backend"]
    # The restored stack is accepted again before the deploy reports failure.
    assert waits.call_count == 2
    # A failed attempt is neither what later commands start nor what a
    # rollback returns to: the release that keeps running stays recorded.
    assert not args.state.exists()
    assert not Path(".ops").exists()


def test_an_accepted_deploy_is_pinned_and_recorded_only_after_acceptance(tmp_path, monkeypatch):
    args = arguments(tmp_path)
    monkeypatch.setattr(ops, "_running_images", lambda _: OLD)
    monkeypatch.setattr(ops, "command", Mock())
    seen = []
    monkeypatch.setattr(
        ops, "_wait", lambda _: seen.append(sorted(p.name for p in tmp_path.rglob("*")))
    )
    ops.deploy(args)
    assert seen == [[]], "nothing may be recorded before acceptance"
    new = {"backend": args.backend_image, "frontend": args.frontend_image}
    assert json.loads((args.state / "previous.json").read_text()) == OLD
    assert json.loads((args.state / "current.json").read_text()) == new
    pin = args.state / "images.env"
    assert pin.read_text() == (
        f"BACKEND_IMAGE={args.backend_image}\nFRONTEND_IMAGE={args.frontend_image}\n"
    )
    assert pin.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "failure",
    [
        http.client.IncompleteRead(b"partial", 10),
        KeyboardInterrupt(),
        subprocess.CalledProcessError(1, ["docker", "compose", "up"]),
    ],
    ids=["short-read", "ctrl-c", "compose"],
)
def test_any_failure_after_the_switch_restores_the_previous_images(tmp_path, monkeypatch, failure):
    """Not only the errors a probe usually raises: a short read or Ctrl-C too."""
    args = arguments(tmp_path)
    monkeypatch.setattr(ops, "_running_images", lambda _: OLD)
    calls = []
    first = True

    def run(argv, **kwargs):
        nonlocal first
        calls.append((argv, kwargs))
        if "--no-build" in argv and first:
            first = False
            raise failure

    monkeypatch.setattr(ops, "command", run)
    monkeypatch.setattr(ops, "_wait", Mock())
    with pytest.raises(ValueError) as raised:
        ops.deploy(args)
    assert [kw["env"]["BACKEND_IMAGE"] for _, kw in _switches(calls)] == [
        args.backend_image,
        OLD["backend"],
    ]
    assert ops.describe(failure) in str(raised.value)
    assert not args.state.exists()


def test_a_failed_restore_reports_both_failures(tmp_path, monkeypatch):
    args = arguments(tmp_path)
    monkeypatch.setattr(ops, "_running_images", lambda _: OLD)
    monkeypatch.setattr(ops, "command", Mock())
    monkeypatch.setattr(
        ops, "_wait", Mock(side_effect=[ValueError("new is down"), ValueError("old is down")])
    )
    with pytest.raises(ValueError) as raised:
        ops.deploy(args)
    assert "new is down" in str(raised.value)
    assert "Restoring the previous images failed too: old is down" in str(raised.value)


def test_acceptance_timeout_names_the_last_underlying_failure(tmp_path, monkeypatch):
    """ "Timed out" alone sends the operator to the logs for what one line can say."""
    args = arguments(tmp_path, timeout=0)
    monkeypatch.setattr(ops, "smoke", Mock(side_effect=http.client.IncompleteRead(b"partial", 10)))
    with pytest.raises(ValueError, match=r"within 0s; last check: IncompleteRead"):
        ops._wait(args)


def test_rollback_pins_what_it_started_and_records_it_once_accepted(tmp_path, monkeypatch):
    args = arguments(tmp_path)
    ops._write_json(args.state / "previous.json", OLD)
    monkeypatch.setattr(ops, "command", Mock())
    monkeypatch.setattr(ops, "_wait", Mock(side_effect=ValueError("still starting")))
    with pytest.raises(ValueError, match="still starting"):
        ops.rollback(args)
    # Running and pinned, so a later `make up` keeps the release rolled back to,
    # but not recorded as accepted.
    assert f"BACKEND_IMAGE={OLD['backend']}" in (args.state / "images.env").read_text()
    assert not (args.state / "current.json").exists()

    monkeypatch.setattr(ops, "_wait", Mock())
    ops.rollback(args)
    assert json.loads((args.state / "current.json").read_text()) == OLD


def test_rollback_without_a_recorded_release_says_so(tmp_path, monkeypatch):
    run = Mock()
    monkeypatch.setattr(ops, "command", run)
    with pytest.raises(ValueError, match="No release to return to is recorded"):
        ops.rollback(arguments(tmp_path))
    run.assert_not_called()


def test_a_failed_command_is_reported_with_its_command_line(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["gateway_ops", "backup", "--backup", str(tmp_path / "x")])
    monkeypatch.setattr(
        ops,
        "command",
        Mock(side_effect=subprocess.CalledProcessError(2, ["docker", "compose", "exec"])),
    )
    assert ops.main() == 1
    assert "`docker compose exec` exited with status 2" in capsys.readouterr().err


def test_an_interrupted_operation_exits_without_a_traceback(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["gateway_ops", "smoke", "--base-url", "http://x"])
    monkeypatch.setattr(ops, "smoke", Mock(side_effect=KeyboardInterrupt))
    assert ops.main() == 130
    assert capsys.readouterr().err == "smoke interrupted\n"


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


def test_a_legacy_manifest_is_imported_by_the_new_image_before_the_switch(tmp_path, monkeypatch):
    """Otherwise the new release serves open signup and default alerts until someone imports."""
    args = arguments(
        tmp_path,
        legacy_manifest="distributions/site/distribution.yaml",
        legacy_import_option=["--example-option"],
    )
    monkeypatch.setattr(ops, "_running_images", lambda _: OLD)
    calls = []
    monkeypatch.setattr(ops, "command", lambda argv, **kw: calls.append((argv, kw)))
    monkeypatch.setattr(ops, "_wait", Mock())
    ops.deploy(args)
    migration = next(i for i, (argv, _) in enumerate(calls) if "upgrade" in argv)
    (imported,) = [i for i, (argv, _) in enumerate(calls) if "import-legacy" in argv]
    first_switch = _switches(calls)[0][0]
    assert migration < imported < first_switch
    argv, kwargs = calls[imported]
    assert argv[-4:] == [
        "serving.config.manage",
        "import-legacy",
        "--example-option",
        "distributions/site/distribution.yaml",
    ]
    assert argv[argv.index("run") : argv.index("run") + 4] == [
        "run",
        "--rm",
        "--no-deps",
        "backend",
    ]
    assert ops._ARTIFACTS in argv
    assert kwargs["env"]["BACKEND_IMAGE"] == args.backend_image


def test_a_failed_legacy_import_never_switches_running_services(tmp_path, monkeypatch):
    args = arguments(tmp_path, legacy_manifest="distributions/site/distribution.yaml")
    monkeypatch.setattr(ops, "_running_images", lambda _: OLD)

    def run(argv, **kwargs):
        if "import-legacy" in argv:
            raise subprocess.CalledProcessError(1, argv)

    monkeypatch.setattr(ops, "command", run)
    switch = Mock()
    monkeypatch.setattr(ops, "_switch", switch)
    with pytest.raises(subprocess.CalledProcessError):
        ops.deploy(args)
    switch.assert_not_called()


def test_no_legacy_manifest_imports_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(ops, "_running_images", lambda _: OLD)
    calls = []
    monkeypatch.setattr(ops, "command", lambda argv, **kw: calls.append(argv))
    monkeypatch.setattr(ops, "_wait", Mock())
    ops.deploy(arguments(tmp_path))
    assert not any("import-legacy" in argv for argv in calls)


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


@pytest.mark.parametrize("status", ["healthy", "degraded"])
def test_smoke_checks_the_public_health_path_and_refuses_degraded_stores(monkeypatch, status):
    responses = {"/health": {"status": status}, "/site-config": {}, "/v1/models": {"data": []}}
    request = Mock(side_effect=lambda base_url, path: responses[path])
    monkeypatch.setattr(ops, "request", request)
    if status == "healthy":
        assert ops.smoke("http://localhost:3001")["ready"] is True
    else:
        with pytest.raises(ValueError, match="Gateway health is not healthy"):
            ops.smoke("http://localhost:3001")
    assert request.call_args_list[0].args == ("http://localhost:3001", "/health")


@pytest.mark.parametrize("status", ["healthy", "degraded"])
def test_doctor_checks_the_public_health_path_and_reports_degraded_stores(
    tmp_path, monkeypatch, status
):
    responses = {"/health": {"status": status}, "/site-config": {}}
    request = Mock(side_effect=lambda base_url, path: responses[path])
    monkeypatch.setattr(ops, "request", request)
    monkeypatch.setattr(ops, "command", Mock(return_value=argparse.Namespace(stdout="[]")))
    monkeypatch.setattr(ops, "configuration_status", Mock(return_value={}))
    assert ops.doctor(arguments(tmp_path)) == int(status != "healthy")
    assert request.call_args_list[0].args == ("http://localhost:3001", "/health")


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


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1", "127.0.0.1"),
        ("0.0.0.0", "localhost"),
        ("::", "localhost"),
        ("::1", "[::1]"),
        ("192.0.2.10", "192.0.2.10"),
    ],
)
def test_probe_url_uses_published_port_and_safe_bound_host(monkeypatch, host, expected):
    config = {
        "services": {
            "frontend": {"ports": [{"target": 3001, "published": "23001", "host_ip": host}]}
        }
    }
    run = Mock(return_value=argparse.Namespace(stdout=json.dumps(config)))
    monkeypatch.setattr(ops, "command", run)
    compose = ["docker", "compose", "--env-file", "custom.env"]
    assert ops.published_base_url(compose, "frontend") == f"http://{expected}:23001"
    run.assert_called_once_with([*compose, "config", "--format", "json"], capture=True, text=True)


@pytest.mark.parametrize(
    "ports",
    [[], [{"target": 3001}], [{"target": 3001, "published": "23001-23002"}]],
)
def test_probe_url_requires_a_known_published_endpoint(monkeypatch, ports):
    config = {"services": {"frontend": {"ports": ports}}}
    monkeypatch.setattr(
        ops, "command", Mock(return_value=argparse.Namespace(stdout=json.dumps(config)))
    )
    with pytest.raises(ValueError, match="No published frontend endpoint; specify --base-url"):
        ops.published_base_url(["docker", "compose"], "frontend")


@pytest.mark.parametrize("action", ["doctor", "smoke", "deploy", "rollback"])
def test_main_discovers_the_effective_url_when_none_is_supplied(monkeypatch, action):
    monkeypatch.setattr(sys, "argv", ["gateway_ops", action])
    resolve = Mock(return_value="http://127.0.0.1:23001")
    monkeypatch.setattr(ops, "published_base_url", resolve)
    operation = Mock(return_value=0)
    monkeypatch.setattr(ops, action, operation)
    assert ops.main() == 0
    resolve.assert_called_once_with(
        ["docker", "compose"], "backend" if action == "smoke" else "frontend"
    )
    if action == "smoke":
        operation.assert_called_once_with("http://127.0.0.1:23001", "")
    else:
        assert operation.call_args.args[0].base_url == "http://127.0.0.1:23001"


def test_explicit_probe_url_bypasses_compose_discovery(monkeypatch):
    monkeypatch.setattr(
        sys, "argv", ["gateway_ops", "smoke", "--base-url", "https://gateway.example"]
    )
    resolve = Mock()
    monkeypatch.setattr(ops, "published_base_url", resolve)
    smoke = Mock(return_value={"ready": True})
    monkeypatch.setattr(ops, "smoke", smoke)
    assert ops.main() == 0
    resolve.assert_not_called()
    smoke.assert_called_once_with("https://gateway.example", "")
