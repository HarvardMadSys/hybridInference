"""Tests for the Kata Containers host provisioning script.

The script installs the runtime that puts each agent sandbox behind its own
kernel, so the cases that matter are the ones where it must *refuse*: an
unverifiable download, a host that cannot start a VM, a version that does not
match the pin. Each is exercised by running the real script with the host facts
it reads pointed at fixtures — `uname` faked on PATH, `/proc/cpuinfo` and
`/dev/kvm` redirected — rather than by stubbing out its logic.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SETUP_SCRIPT = REPO_ROOT / "ops/setup/setup_kata_runtime.sh"

PINNED_VERSION = "3.32.0"
_HAS_ZSTD = shutil.which("zstd") is not None


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


def _fake_bin(tmp_path: Path) -> Path:
    """A PATH front-end that makes this machine look like an x86_64 Linux host.

    Only `uname` and `curl` are faked. Everything else the script calls (tar,
    zstd, shasum, grep) is the real thing, so the archive really is unpacked and
    the digest really is computed.
    """
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    _write_executable(
        fake_bin / "uname",
        '#!/bin/sh\ncase "$1" in\n  -m) echo x86_64 ;;\n  *) echo Linux ;;\nesac\n',
    )
    _write_executable(
        fake_bin / "curl",
        "#!/bin/sh\n"
        'printf "download\\n" >> "$CURL_LOG"\n'
        'dest=""\n'
        "while [ $# -gt 0 ]; do\n"
        '  case "$1" in\n'
        '    -o) dest="$2"; shift 2 ;;\n'
        "    *) shift ;;\n"
        "  esac\n"
        "done\n"
        '[ -n "${FAKE_TARBALL:-}" ] && cp "$FAKE_TARBALL" "$dest"\n'
        "exit 0\n",
    )
    return fake_bin


def _cpuinfo(tmp_path: Path, *, virtualized: bool = True) -> Path:
    """A /proc/cpuinfo stand-in, with or without the virtualization flag."""
    flags = "fpu vme de pse tsc msr"
    if virtualized:
        flags += " vmx"
    path = tmp_path / "cpuinfo"
    path.write_text(f"processor\t: 0\nmodel name\t: Test CPU\nflags\t\t: {flags} pae mce\n")
    return path


def _kvm_device(tmp_path: Path) -> Path:
    """A /dev/kvm stand-in. Its presence is all the script checks."""
    path = tmp_path / "kvm"
    path.write_text("")
    return path


def _fake_release_tarball(tmp_path: Path, *, version: str = PINNED_VERSION) -> Path:
    """Build an archive shaped like a real kata-static release.

    Members are `./opt/kata/...` — verified against the published 3.32.0
    tarball — which is what makes the script's `tar -C /` correct.
    """
    staging = tmp_path / "staging"
    bin_dir = staging / "opt" / "kata" / "bin"
    bin_dir.mkdir(parents=True)
    (staging / "opt" / "kata" / "VERSION").write_text(f"{version}\n")
    _write_executable(bin_dir / "containerd-shim-kata-v2", "#!/bin/sh\nexit 0\n")
    _write_executable(
        bin_dir / "kata-runtime",
        f'#!/bin/sh\ncase "$1" in\n  --version) echo "kata-runtime  : {version}" ;;\n'
        "  check) exit 0 ;;\nesac\n",
    )
    _write_executable(bin_dir / "kata-collect-data.sh", "#!/bin/sh\nexit 0\n")

    archive = tmp_path / f"kata-static-{version}-amd64.tar.zst"
    subprocess.run(
        f"tar -C {staging} -cf - ./opt | zstd -q -o {archive}",
        shell=True,
        check=True,
    )
    return archive


def _run(
    tmp_path: Path,
    *args: str,
    env_overrides: dict[str, str] | None = None,
    tarball: Path | None = None,
    virtualized: bool = True,
    kvm: bool = True,
) -> subprocess.CompletedProcess[str]:
    fake_bin = _fake_bin(tmp_path)
    install_root = tmp_path / "root"
    install_root.mkdir(exist_ok=True)
    link_dir = tmp_path / "usr-local-bin"
    link_dir.mkdir(exist_ok=True)

    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "KATA_EXTRACT_ROOT": str(install_root),
        "KATA_LINK_DIR": str(link_dir),
        "KATA_CPUINFO": str(_cpuinfo(tmp_path, virtualized=virtualized)),
        "KATA_KVM_DEVICE": str(_kvm_device(tmp_path) if kvm else tmp_path / "no-kvm"),
        "CURL_LOG": str(tmp_path / "curl.log"),
        **({"FAKE_TARBALL": str(tarball)} if tarball else {}),
        **(env_overrides or {}),
    }
    return subprocess.run(
        ["bash", str(SETUP_SCRIPT), *args],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _download_count(tmp_path: Path) -> int:
    log = tmp_path / "curl.log"
    return len(log.read_text().splitlines()) if log.exists() else 0


# ── Refusals: the cases where installing anyway would be the bug ──────────


def test_a_checksum_mismatch_installs_nothing(tmp_path: Path) -> None:
    """Upstream publishes no checksum file, so the pin is the only guard.

    An archive that does not match it is a supply-chain question, not a retry:
    this becomes the kernel every agent job runs under.
    """
    tarball = tmp_path / "wrong.tar.zst"
    tarball.write_bytes(b"not the release you pinned")

    result = _run(tmp_path, tarball=tarball, env_overrides={"KATA_SHA256": "00" * 32})

    assert result.returncode != 0
    assert "checksum mismatch" in result.stderr
    assert not (tmp_path / "root" / "opt" / "kata").exists()
    # And the bad archive is gone, not left for someone to unpack by hand.
    assert not list(tmp_path.glob("**/kata-static.tar.zst"))


def test_an_unknown_version_without_a_digest_refuses(tmp_path: Path) -> None:
    """Refusing beats falling back to an unverified download.

    Bumping KATA_VERSION and forgetting the digest is the easy mistake; silently
    skipping verification would make it an invisible one.
    """
    result = _run(tmp_path, env_overrides={"KATA_VERSION": "9.9.9"})

    assert result.returncode != 0
    assert "no known digest" in result.stderr
    assert _download_count(tmp_path) == 0, "it downloaded before deciding it could not verify"


def test_a_host_without_kvm_is_refused_before_downloading(tmp_path: Path) -> None:
    """Kata needs a VM. Without /dev/kvm every job would fail at spawn."""
    result = _run(tmp_path, kvm=False)

    assert result.returncode != 0
    assert "is missing" in result.stderr
    assert "nested virtualization" in result.stderr
    assert _download_count(tmp_path) == 0


def test_a_cpu_without_virtualization_extensions_is_refused(tmp_path: Path) -> None:
    """A guest with no nested virt gives a clearer error than a KVM failure."""
    result = _run(tmp_path, virtualized=False)

    assert result.returncode != 0
    assert "vmx/svm" in result.stderr
    assert _download_count(tmp_path) == 0


def test_check_refuses_a_host_that_has_no_kata(tmp_path: Path) -> None:
    """What deployment calls. It must never install as a side effect."""
    result = _run(tmp_path, "--check")

    assert result.returncode != 0
    assert "Kata is not installed" in result.stderr
    assert _download_count(tmp_path) == 0
    assert not (tmp_path / "root" / "opt").exists()


# ── The install path ─────────────────────────────────────────────────────


@pytest.mark.skipif(not _HAS_ZSTD, reason="zstd is needed to build a release-shaped fixture")
def test_installs_links_the_shim_and_passes_its_own_check(tmp_path: Path) -> None:
    """A successful install leaves the daemon able to find the shim.

    The symlink is the load-bearing part: the daemon resolves
    `io.containerd.kata.v2` to a `containerd-shim-kata-v2` on its own PATH, so
    an unpacked-but-unlinked tree is a host where every job still fails.
    """
    tarball = _fake_release_tarball(tmp_path)

    result = _run(tmp_path, tarball=tarball, env_overrides={"KATA_SHA256": _digest(tarball)})

    assert result.returncode == 0, result.stderr
    prefix = tmp_path / "root" / "opt" / "kata"
    assert (prefix / "VERSION").read_text().strip() == PINNED_VERSION
    shim = tmp_path / "usr-local-bin" / "containerd-shim-kata-v2"
    assert shim.is_symlink()
    assert os.readlink(shim) == str(prefix / "bin" / "containerd-shim-kata-v2")

    # And the deploy-time check now passes on this host.
    assert _run(tmp_path, "--check").returncode == 0


@pytest.mark.skipif(not _HAS_ZSTD, reason="zstd is needed to build a release-shaped fixture")
def test_a_second_run_downloads_nothing(tmp_path: Path) -> None:
    """Provisioning is re-run on every host rebuild; 1.5 GB each time is not it."""
    tarball = _fake_release_tarball(tmp_path)
    env = {"KATA_SHA256": _digest(tarball)}

    assert _run(tmp_path, tarball=tarball, env_overrides=env).returncode == 0
    assert _download_count(tmp_path) == 1

    second = _run(tmp_path, tarball=tarball, env_overrides=env)

    assert second.returncode == 0
    assert "already installed" in second.stdout
    assert _download_count(tmp_path) == 1, "a repeat run re-downloaded the release"


@pytest.mark.skipif(not _HAS_ZSTD, reason="zstd is needed to build a release-shaped fixture")
def test_check_refuses_a_host_whose_shim_is_not_linked(tmp_path: Path) -> None:
    """Unpacked but unlinked is the failure that looks like success.

    /opt/kata is fully populated, so anyone eyeballing the host concludes Kata
    is installed — while the daemon still cannot resolve the runtime.
    """
    tarball = _fake_release_tarball(tmp_path)
    assert (
        _run(tmp_path, tarball=tarball, env_overrides={"KATA_SHA256": _digest(tarball)}).returncode
        == 0
    )
    (tmp_path / "usr-local-bin" / "containerd-shim-kata-v2").unlink()

    result = _run(tmp_path, "--check")

    assert result.returncode != 0
    assert "not linked" in result.stderr


@pytest.mark.skipif(not _HAS_ZSTD, reason="zstd is needed to build a release-shaped fixture")
def test_check_refuses_a_host_pinned_to_a_different_version(tmp_path: Path) -> None:
    """A host left on an old release must not read as provisioned.

    Upgrading the pin is how a Kata CVE gets rolled out, so deployment has to
    notice the host that never got it.
    """
    tarball = _fake_release_tarball(tmp_path)
    assert (
        _run(tmp_path, tarball=tarball, env_overrides={"KATA_SHA256": _digest(tarball)}).returncode
        == 0
    )

    result = _run(tmp_path, "--check", env_overrides={"KATA_VERSION": "4.0.0"})

    assert result.returncode != 0
    assert PINNED_VERSION in result.stderr
    assert "4.0.0" in result.stderr
