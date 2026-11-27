"""Tests for the direct-tree private-surface migration guard."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

import pytest

from ops.admin import private_surface_sweep as sweep_module

REPO = Path(__file__).resolve().parents[3]

_SAMPLES = {
    "personal mailbox": "alice@" + "gmail.com",
    "personal home path": "/Us" + "ers/alice/work/repo",
    "cluster path": "/n/net" + "scratch/alice/models/x",
    "documented admin password": "`admin@" + "admin.com` / `admin`",
    "cloudflare identifier": 'account_id = "' + "a" * 32 + '"',
}

# The deployment keeps its real host names, subdomains and networks, as sample
# texts the guard must flag, outside this repository. Point this at that file
# to check the real digests; without it, the invented ones below cover the
# matching.
_PRIVATE_SAMPLES = os.environ.get("PRIVATE_SURFACE_SAMPLES", "")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# Invented names, given to the matcher by digest exactly as the real ones are.
_INVENTED = sweep_module.NameDigests(
    hosts=[_sha256("edgebox1"), _sha256("alpha-direct")],
    families=[
        (4, _sha256("node"), "[0-9]+"),
        (4, _sha256("rack"), "[a-z]?"),
        (5, _sha256("fleet"), "[a-z0-9]*"),
    ],
    subdomains=[_sha256("intra"), _sha256("intradocs")],
)
_INVENTED_NETWORKS = sweep_module.AddressDigests(
    [(24, _sha256("198.51.100.0/24")), (48, _sha256("2001:db8:42::/48"))]
)


@pytest.mark.parametrize("label", sorted(_SAMPLES))
def test_each_private_surface_shape_is_detected(label: str) -> None:
    """Every retained detector has a negative test that proves it can fire."""
    assert sweep_module.PATTERNS[label].search(_SAMPLES[label])


@pytest.mark.parametrize(
    "text",
    [
        "edgebox1",
        "ssh edgebox1.lan",
        "alpha-direct",
        "x-alpha-direct-2",
        "node7",
        "node12-docker-1",
        "ops/node3_idle_proxy",
        "rack",
        "racks",
        "fleet",
        "fleetq9",
        "intra.example.org",
        "https://intradocs.example.org/page",
        "staging-intra.example.org",
        "a.intra.example.org",
    ],
)
def test_listed_names_are_found_by_digest(text: str) -> None:
    """Hosts, families and subdomains are found from their digests alone."""
    assert list(_INVENTED.offsets(text))


@pytest.mark.parametrize(
    "text",
    [
        "edgebox12",
        "Edgebox1",
        "edgebox",
        "alpha direct",
        "node",
        "nodex",
        "rack1",
        "rackab",
        "Fleet",
        "intra.example",
        "example.intra",
        "host.docker.intra",
        "intra.10.0.0.1",
        "intranet.example.org",
    ],
)
def test_near_misses_are_not_listed_names(text: str) -> None:
    """A part, a case change or a missing domain is not the listed name."""
    assert not list(_INVENTED.offsets(text))


@pytest.mark.parametrize(
    "text",
    [
        "198.51.100.7",
        "peer 198.51.100.7.",
        "198.51.100.0/24",
        "198.51.100.128/25",
        "::ffff:198.51.100.7",
        "2001:db8:42::1",
        "2001:DB8:42:0:0::1",
        "[2001:db8:42::53]:8080",
        "2001:db8:42:7::/64",
    ],
)
def test_addresses_in_listed_networks_are_found(text: str) -> None:
    """Any spelling of an address or subnet inside a listed network is found."""
    assert list(_INVENTED_NETWORKS.offsets(text))


@pytest.mark.parametrize(
    "text",
    ["198.51.101.7", "198.51.100", "198.51.0.0/16", "2001:db8:43::1", "12:41:20", "a::b"],
)
def test_addresses_outside_listed_networks_are_not_found(text: str) -> None:
    """A neighbour, a wider network or a time of day is not a listed network."""
    assert not list(_INVENTED_NETWORKS.offsets(text))


def test_a_listed_name_is_reported_on_its_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Names and addresses found by digest join the patterns' findings."""
    (tmp_path / "notes.md").write_text("first line\nthen edgebox1 again\nvia 198.51.100.7\n")
    monkeypatch.setattr(sweep_module, "PRIVATE_NAMES", _INVENTED)
    monkeypatch.setattr(sweep_module, "PRIVATE_ADDRESSES", _INVENTED_NETWORKS)
    monkeypatch.setattr(sweep_module, "tracked_files", lambda _root: ["notes.md"])
    _buckets, violations = sweep_module.sweep(tmp_path)
    assert violations == [
        f"{sweep_module.PRIVATE_ADDRESS_LABEL}:notes.md:3",
        f"{sweep_module.PRIVATE_NAME_LABEL}:notes.md:2",
    ]


def test_the_real_tables_hold_digests_only() -> None:
    """A readable name or network here would publish what the guard keeps out."""
    digests = [
        *sweep_module.HOST_NAME_DIGESTS,
        *(digest for _size, digest, _rest in sweep_module.HOST_FAMILY_DIGESTS),
        *sweep_module.SUBDOMAIN_DIGESTS,
        *(digest for _size, digest in sweep_module.ADDRESS_DIGESTS),
    ]
    assert digests
    assert all(re.fullmatch(r"[0-9a-f]{64}", digest) for digest in digests)
    assert all(size > 0 for size, _digest, _rest in sweep_module.HOST_FAMILY_DIGESTS)
    assert all(0 < size <= 128 for size, _digest in sweep_module.ADDRESS_DIGESTS)


@pytest.mark.skipif(not _PRIVATE_SAMPLES, reason="PRIVATE_SURFACE_SAMPLES is not set")
def test_the_deployment_samples_are_still_detected() -> None:
    """The real digests still match the names and networks they stand for.

    Failures name line numbers in the samples file, not the samples, so the
    output can be shared without republishing them.
    """
    lines = Path(_PRIVATE_SAMPLES).read_text(encoding="utf-8").splitlines()
    samples = [
        (number, line.strip())
        for number, line in enumerate(lines, start=1)
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert samples, "the samples file lists no samples"
    missed = [
        number
        for number, sample in samples
        if not list(sweep_module.PRIVATE_NAMES.offsets(sample))
        and not list(sweep_module.PRIVATE_ADDRESSES.offsets(sample))
    ]
    assert not missed, f"samples on these lines are no longer detected: {missed}"


@pytest.mark.parametrize("value", ["/home/agent", "/home/somebody"])
def test_stable_service_and_placeholder_homes_are_not_personal(value: str) -> None:
    """The guard stays focused on copied developer-machine paths."""
    assert value in sweep_module.IGNORED_VALUES


@pytest.mark.parametrize("name", ["logo.svg", "uv.lock"])
def test_text_files_are_read_regardless_of_suffix(tmp_path: Path, name: str) -> None:
    """An SVG comment or lock-file URL is part of the publication surface."""
    path = tmp_path / name
    path.write_text(_SAMPLES["cluster path"])
    assert sweep_module.read_text(path) == path.read_text()


def test_binary_files_are_skipped_by_content(tmp_path: Path) -> None:
    """A NUL sniff distinguishes binaries without a bypassable suffix list."""
    path = tmp_path / "renamed-binary"
    path.write_bytes(b"prefix\0" + _SAMPLES["cluster path"].encode())
    assert sweep_module.read_text(path) is None


def test_an_unreadable_file_is_a_violation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A path the guard could not read was not cleared for publication."""
    monkeypatch.setattr(sweep_module, "tracked_files", lambda _root: ["missing.txt"])
    _buckets, violations = sweep_module.sweep(tmp_path)
    assert violations and violations[0].startswith("unreadable:missing.txt")


def test_current_tree_has_no_unclassified_private_surface() -> None:
    """Every current finding has an explicit migration owner."""
    buckets, violations = sweep_module.sweep(REPO)
    assert not violations
    assert set(buckets) == set(sweep_module.PENDING), (
        "remove empty PENDING entries when their migration finishes"
    )


@pytest.mark.parametrize("kind", ["file", "directory", "missing"])
def test_symlink_text_remains_in_the_scan(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kind: str
) -> None:
    """Directory and dangling aliases must not hide private link targets."""
    target = tmp_path / "target"
    if kind == "file":
        target.write_text("ordinary content")
    elif kind == "directory":
        target.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(target.name, target_is_directory=kind == "directory")
    assert sweep_module.read_text(alias) == target.name

    private_alias = tmp_path / "private-alias"
    private_alias.symlink_to(_SAMPLES["cluster path"])
    monkeypatch.setattr(sweep_module, "tracked_files", lambda _root: ["alias", "private-alias"])
    _buckets, violations = sweep_module.sweep(tmp_path)
    assert len(violations) == 1
    assert "private-alias" in violations[0]
    assert "cluster path" in violations[0]
