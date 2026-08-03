"""Tests for the agent-runner host preflight.

The runner deploys with a `kata` sandbox backend by default, which is a claim
about the isolation boundary every job gets. These cover the ways that claim can
be false while everything still looks fine — no shim on the host, or a runtime
the daemon accepts that nonetheless hands the sandbox the host's own kernel.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
RUNNER_SCRIPT = REPO_ROOT / "ops/deploy/agent_runner.sh"

HOST_KERNEL = "6.8.0-51-generic"
GUEST_KERNEL = "6.12.0-kata"


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


def _host(
    tmp_path: Path,
    *,
    dotenv: str = "",
    kata_check_passes: bool = True,
    guest_kernel: str = GUEST_KERNEL,
    docker_run_fails: bool = False,
) -> dict[str, str]:
    """Build a fake runner host: an APP_DIR, a .env, and a faked docker/uname."""
    app_dir = tmp_path / "app"
    app_dir.mkdir(exist_ok=True)
    (app_dir / ".env").write_text(dotenv)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    _write_executable(
        fake_bin / "uname",
        f'#!/bin/sh\n[ "$1" = "-r" ] && echo "{HOST_KERNEL}" || echo Linux\n',
    )
    run_body = (
        f'echo "{guest_kernel}"; exit 0 ;;\n'
        if not docker_run_fails
        else 'echo "failed to start shim: no such file" >&2; exit 125 ;;\n'
    )
    _write_executable(
        fake_bin / "docker",
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "$DOCKER_LOG"\n'
        'case "$1" in\n'
        "  info) exit 0 ;;\n"
        f"  run) {run_body}"
        "  *) exit 0 ;;\n"
        "esac\n",
    )

    kata_script = tmp_path / "fake_kata_setup.sh"
    _write_executable(
        kata_script,
        "#!/bin/sh\n" + ("exit 0\n" if kata_check_passes else "echo 'not installed' >&2\nexit 1\n"),
    )

    return {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "APP_DIR": str(app_dir),
        "KATA_SETUP_SCRIPT": str(kata_script),
        "DOCKER_LOG": str(tmp_path / "docker.log"),
        # Otherwise the caller's own environment decides the backend and the
        # .env precedence under test never runs.
        "AGENT_SANDBOX_BACKEND": "",
    }


def _run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(RUNNER_SCRIPT), *args],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


def _docker_calls(tmp_path: Path) -> list[str]:
    log = tmp_path / "docker.log"
    return log.read_text().splitlines() if log.exists() else []


def test_preflight_confirms_isolation_when_the_guest_has_its_own_kernel(tmp_path: Path) -> None:
    """The passing case, and what "verified Kata" is allowed to mean."""
    env = _host(tmp_path)

    result = _run(env, "preflight")

    assert result.returncode == 0, result.stderr
    assert GUEST_KERNEL in result.stdout
    assert HOST_KERNEL in result.stdout
    assert any("--runtime io.containerd.kata.v2" in call for call in _docker_calls(tmp_path))


def test_preflight_refuses_a_sandbox_running_the_host_kernel(tmp_path: Path) -> None:
    """The failure every cheaper signal misses.

    The daemon accepted the runtime and the container started, so the flag
    reached the daemon and the shim exists — and the sandbox is still on the
    host's kernel. Only asking the container which kernel it is on catches this.
    """
    env = _host(tmp_path, guest_kernel=HOST_KERNEL)

    result = _run(env, "preflight")

    assert result.returncode != 0
    assert "NOT" in result.stderr and "VM-isolated" in result.stderr


def test_preflight_refuses_a_host_with_no_kata_shim_before_touching_docker_run(
    tmp_path: Path,
) -> None:
    """Fail on the cheap check, and say exactly which command fixes it."""
    env = _host(tmp_path, kata_check_passes=False)

    result = _run(env, "preflight")

    assert result.returncode != 0
    assert "not provisioned for Kata" in result.stderr
    assert "sudo" in result.stderr
    assert not any("run" in call for call in _docker_calls(tmp_path))


def test_preflight_reports_a_runtime_that_cannot_start(tmp_path: Path) -> None:
    """Installed but not working is its own diagnosis, not a generic failure."""
    env = _host(tmp_path, docker_run_fails=True)

    result = _run(env, "preflight")

    assert result.returncode != 0
    assert "could not start a container" in result.stderr
    assert "failed to start shim" in result.stderr


def test_an_explicit_container_backend_is_allowed_but_says_what_it_costs(tmp_path: Path) -> None:
    """Accepting a shared kernel stays possible — and stays loud.

    A silent `AGENT_SANDBOX_BACKEND=container` in an .env is how a deployment
    forgets it has no kernel boundary, so every run restates it.
    """
    env = _host(tmp_path, dotenv="AGENT_SANDBOX_BACKEND=container\n")

    result = _run(env, "preflight")

    assert result.returncode == 0, result.stderr
    assert "share the host kernel" in result.stderr
    # And it does not pretend to verify isolation it does not have.
    assert not any("run" in call for call in _docker_calls(tmp_path))


def test_the_backend_is_read_from_dotenv_the_way_compose_reads_it(tmp_path: Path) -> None:
    """Preflight must gate on the value the runner will actually be given.

    Quoted, because .env files are written both ways and gating on `"kata"`
    while compose sees `kata` would skip the check on exactly the hosts that
    need it.
    """
    env = _host(tmp_path, dotenv='AGENT_SANDBOX_BACKEND="kata"\n', kata_check_passes=False)

    result = _run(env, "preflight")

    assert result.returncode != 0
    assert "not provisioned for Kata" in result.stderr
