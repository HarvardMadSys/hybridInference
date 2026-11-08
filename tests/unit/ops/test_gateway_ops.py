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


def test_import_options_alone_import_the_files_the_environment_names(tmp_path, monkeypatch):
    args = arguments(tmp_path, legacy_import_option=["--alerts=/app/alerts.yaml"])
    monkeypatch.setattr(ops, "_running_images", lambda _: OLD)
    calls = []
    monkeypatch.setattr(ops, "command", lambda argv, **kw: calls.append(argv))
    monkeypatch.setattr(ops, "_wait", Mock())
    ops.deploy(args)
    (imported,) = [argv for argv in calls if "import-legacy" in argv]
    assert imported[-2:] == ["import-legacy", "--alerts=/app/alerts.yaml"]


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


class FakeDocker:
    """Answer the restore drill's commands; record each with its environment."""

    def __init__(self, *, free_gib=100, database_bytes=10 << 20, fail_on=None, left=()):
        self.calls = []
        self.free_kib = free_gib << 20
        self.database_bytes = database_bytes
        self.fail_on = fail_on
        self.left = set(left)

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if self.fail_on and any(self.fail_on in str(token) for token in argv):
            raise subprocess.CalledProcessError(1, argv)
        if argv[-3:] == ["config", "--format", "json"]:
            return argparse.Namespace(
                stdout=json.dumps({"services": {"postgres": {"image": "pg:x"}}})
            )
        if "pg_database_size" in argv[-1]:
            return argparse.Namespace(stdout=f"{self.database_bytes}\n")
        if "df" in argv:
            return argparse.Namespace(
                stdout="Filesystem 1024-blocks Used Available Capacity Mounted on\n"
                f"/dev/sda1 999999999 1 {self.free_kib} 1% /var/lib/postgresql/data\n"
            )
        if argv[0] == "docker" and argv[2:3] == ["inspect"] and argv[1] not in self.left:
            raise subprocess.CalledProcessError(1, argv)  # removed: inspect finds nothing
        return argparse.Namespace(stdout="")

    def lines(self):
        return [" ".join(argv) for argv, _ in self.calls]


def test_restore_check_never_restores_into_the_deployment_database_server(tmp_path, monkeypatch):
    """A restore there costs the serving database its disk, WAL and buffers."""
    args = arguments(tmp_path)
    args.backup.write_bytes(b"private dump")
    docker = FakeDocker()
    monkeypatch.setattr(ops, "command", docker)
    ops.restore_check(args)

    lines = docker.lines()
    against_deployment = [line for line in lines if " exec -T postgres " in line]
    assert len(against_deployment) == 1 and "pg_database_size" in against_deployment[0]
    assert not any(word in line for line in lines for word in ("createdb", "dropdb"))

    (started,) = [(argv, kw) for argv, kw in docker.calls if "up" in argv]
    name = started[1]["env"]["RESTORE_CHECK_NAME"]
    assert name.startswith("gateway-restore-")
    assert started[1]["env"]["RESTORE_CHECK_IMAGE"] == "pg:x"
    assert started[0][-5:] == ["up", "-d", "--no-deps", "--wait", "restore-check"]
    assert ops._RESTORE_OVERLAY in started[0]

    (restored,) = [(argv, kw) for argv, kw in docker.calls if "pg_restore" in argv[-1]]
    assert restored[0][restored[0].index("exec") : restored[0].index("exec") + 3] == [
        "exec",
        "-T",
        "restore-check",
    ]
    assert restored[1]["stdin"].name == str(args.backup)


def test_restore_check_migrates_a_backup_from_before_the_upgrade(tmp_path, monkeypatch):
    """A backup one release behind is a valid backup: restoring it means migrating it."""
    args = arguments(tmp_path)
    args.backup.write_bytes(b"private dump")
    docker = FakeDocker()
    monkeypatch.setattr(ops, "command", docker)
    ops.restore_check(args)
    migrations = [line for line in docker.lines() if "python -m migrations" in line]
    assert [line.rsplit(" ", 1)[1] for line in migrations] == ["upgrade", "status"]
    for line in migrations:
        assert "run --rm --no-deps -e DB_HOST=restore-check backend" in line
        assert ops._RESTORE_OVERLAY in line


@pytest.mark.parametrize("failing", ["pg_restore", "upgrade", "df"])
def test_restore_check_removes_its_server_volume_and_network_even_on_failure(
    tmp_path, monkeypatch, failing
):
    args = arguments(tmp_path)
    args.backup.write_bytes(b"private dump")
    docker = FakeDocker(fail_on=failing)
    monkeypatch.setattr(ops, "command", docker)
    with pytest.raises(subprocess.CalledProcessError):
        ops.restore_check(args)
    name = next(kw["env"]["RESTORE_CHECK_NAME"] for argv, kw in docker.calls if "up" in argv)
    removed = [argv[1] for argv, _ in docker.calls if argv[:1] == ["docker"] and "rm" in argv]
    assert removed == ["container", "volume", "network"]
    assert all(
        argv[-1] == name for argv, _ in docker.calls if argv[:1] == ["docker"] and "rm" in argv
    )


def test_restore_check_names_what_it_could_not_remove(tmp_path, monkeypatch, capsys):
    args = arguments(tmp_path)
    args.backup.write_bytes(b"private dump")
    monkeypatch.setattr(ops, "command", FakeDocker(left={"volume"}))
    ops.restore_check(args)
    err = capsys.readouterr().err
    assert "could not remove its volume gateway-restore-" in err
    assert "copy of the backup" in err


def test_restore_check_refuses_before_restoring_when_the_disk_is_short(tmp_path, monkeypatch):
    """Twice the database plus 1 GiB must be free where the scratch volume lives."""
    args = arguments(tmp_path)
    args.backup.write_bytes(b"private dump")
    docker = FakeDocker(free_gib=5, database_bytes=3 << 30)
    monkeypatch.setattr(ops, "command", docker)
    with pytest.raises(ValueError, match=r"needs about 7\.0 GiB free .* 5\.0 GiB is free"):
        ops.restore_check(args)
    assert not any("pg_restore" in line for line in docker.lines())
    assert any(line.startswith("docker volume rm") for line in docker.lines())


def test_restore_check_measures_the_dump_when_the_database_is_not_running(tmp_path, monkeypatch):
    args = arguments(tmp_path)
    args.backup.write_bytes(b"x" * (3 << 20))
    docker = FakeDocker(free_gib=1, fail_on="pg_database_size")
    monkeypatch.setattr(ops, "command", docker)
    with pytest.raises(ValueError, match=r"needs about 1\.0 GiB free"):
        ops.restore_check(args)


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
