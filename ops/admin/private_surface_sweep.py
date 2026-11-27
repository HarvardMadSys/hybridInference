"""Inventory private deployment metadata in the repository that will be public.

This is a direct-tree migration guard, not a publication filter. The default
run fails on a finding outside an explicitly owned migration bucket. ``--strict``
also fails while any bucket still contains findings and is the visibility-flip
acceptance command.

    uv run python ops/admin/private_surface_sweep.py
    uv run python ops/admin/private_surface_sweep.py --strict
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator

PATTERNS: dict[str, re.Pattern[str]] = {
    "personal mailbox": re.compile(
        r"\b[A-Za-z0-9._%+-]+@(?:gmail|googlemail|outlook|hotmail|live|icloud|"
        r"me|yahoo|qq|163|126|foxmail)\.(?:com|me)\b",
        re.IGNORECASE,
    ),
    "personal home path": re.compile(r"(?<![A-Za-z0-9_.-])/(?:Users|home)/[A-Za-z0-9._-]+"),
    "cluster path": re.compile(r"(?:/n)?/(?:net)?scratch/[A-Za-z0-9_./-]+"),
    "documented admin password": re.compile(
        r"`?admin@admin\.com`?\s*(?:/|:)\s*`?admin\b", re.IGNORECASE
    ),
    "cloudflare identifier": re.compile(r'(?:account_id|database_id)\s*=\s*"[0-9a-f-]{32,}"'),
}

# A deployment's host names, internal subdomains and networks are private
# themselves, and a guard that spelled them out would publish what it guards.
# So it keeps only their SHA-256 digests (`printf %s NAME | sha256sum`) and
# hashes what it reads. Names are compared case-sensitively, as configuration
# writes them. The names, with sample texts that must stay detected, are kept
# by the deployment they belong to; tests/unit/ops/test_private_surface_sweep.py
# checks those samples when PRIVATE_SURFACE_SAMPLES names that file. A short
# name or a network can still be found by hashing guesses: this keeps the list
# out of the tree, not out of reach of a determined search.
PRIVATE_NAME_LABEL = "internal hostname"
PRIVATE_ADDRESS_LABEL = "deployment address"

# A name run is ASCII letters, digits and hyphens, in labels joined by dots;
# anything else, `_` and `/` included, ends it. A host name is one
# hyphen-separated part of a label, or two adjacent parts.
HOST_NAME_DIGESTS = (
    "94364d42fabb0be8e99f509a9a62f84c3a8c71b6b4d2da52b1c15a5473ba364b",
    "db88950fe98569668fa8dbbf604d6c8dc77785c57987de4ddf36cdf3ee31a9aa",
    "ea2a7ac2f86b064150cf3c21d355c13d9ece7ec2f6c972ff777b39e1e9c34122",
    "413e947d4179b879c39e98db3a3509a098dffe45c1ec3a20d5ca591b8e512e02",
)

# A family of host names within one part: (length of the shared prefix, the
# prefix's digest, a pattern for the rest of the part).
HOST_FAMILY_DIGESTS = (
    (3, "e3c5ba51dba85ab0c990db5e309f4d7bdb282470fb02aee75b9022c1399e91c8", "[0-9]+"),
    (4, "43c25ee16c4402ebc92dd72c143f2e5b1579a34ebeb41638cce87a72d5d80c2f", "[a-z]?"),
    (7, "9783f579c97e8b95155b9dc1e4eff4cf487d9d91585d79d8a4f2443d16f3fb31", "[a-z0-9]*"),
    (7, "c7aef23539bbde514cf75a488bea5f58fa87ec156fbf8c4b4bace50f37b89426", "[a-z0-9]*"),
)

# A subdomain: a label, or the trailing hyphen-separated parts of one, that has
# at least two more labels after it, one of them a top-level domain.
SUBDOMAIN_DIGESTS = (
    "3bed2cb3a3acf7b6a8ef408420cc682d5520e26976d354254f528c965612054f",
    "b35725a97cc7055c666382dda5530fd7dffce3ec0e0e6a56a761ee7eab9ac102",
    "a76ce747bd4f5e4ae87079fa1ae60e0f7ec9cd1cc61a13de99fd04a7a07e2582",
)

# A deployment network: (prefix length, digest of the network as `ipaddress`
# writes it, e.g. `192.0.2.0/24`). Any IPv4 or IPv6 address or network inside
# it is a finding.
ADDRESS_DIGESTS = (
    (16, "dc31b260730b0338609304e2dd16007029e3fe1dda9a1bbf36f9ad6f095c694e"),
    (16, "1a7d28d2c97f4f444d1099cbcd1767b282a1caff92e50131229494cf31982431"),
    (32, "2e518062fd91e17d9fea96cb54e6b5d1110321334d7c7af5ec648ec463c74c59"),
    (48, "429ea77b333daa2f8bb16371feedc3f8f5afdddc6b8512dcc2bf9e2ec2f19365"),
)

_NAME_RUN = re.compile(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*")
_TOP_LEVEL = re.compile(r"[A-Za-z]{2,}")
# Loose on purpose: a candidate counts only if `ipaddress` parses it inside a
# listed network, and leading with a character class keeps the scan fast.
_IPV4 = re.compile(r"[0-9]{1,3}(?:\.[0-9]{1,3}){3}(?:/[0-9]{1,2})?")
_IPV6 = re.compile(r"[0-9A-Fa-f]{0,4}:[0-9A-Fa-f]{0,4}:[0-9A-Fa-f:]*(?:/[0-9]{1,3})?")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class NameDigests:
    """Find names that are known only by the SHA-256 digests of their text."""

    def __init__(
        self,
        hosts: Iterable[str],
        families: Iterable[tuple[int, str, str]],
        subdomains: Iterable[str],
    ) -> None:
        self._hosts = frozenset(hosts)
        self._families = {(size, digest): re.compile(rest) for size, digest, rest in families}
        self._prefix_sizes = sorted({size for size, _ in self._families})
        self._subdomains = frozenset(subdomains)
        # Each distinct run and label is hashed once, however often it recurs.
        self._run_verdicts: dict[str, bool] = {}
        self._label_verdicts: dict[str, bool] = {}

    def _names_a_host(self, label: str) -> bool:
        verdict = self._label_verdicts.get(label)
        if verdict is None:
            verdict = self._label_verdicts[label] = self._match_host(label.split("-"))
        return verdict

    def _match_host(self, parts: list[str]) -> bool:
        for index, part in enumerate(parts):
            if _sha256(part) in self._hosts:
                return True
            if index + 1 < len(parts) and _sha256(f"{part}-{parts[index + 1]}") in self._hosts:
                return True
            for size in self._prefix_sizes:
                if len(part) < size:
                    break
                rest = self._families.get((size, _sha256(part[:size])))
                if rest is not None and rest.fullmatch(part, size):
                    return True
        return False

    def _names_a_subdomain(self, labels: list[str]) -> bool:
        for index, label in enumerate(labels[:-2]):
            if not any(_TOP_LEVEL.fullmatch(later) for later in labels[index + 2 :]):
                break
            parts = label.split("-")
            tails = ("-".join(parts[start:]) for start in range(len(parts)))
            if any(_sha256(tail) in self._subdomains for tail in tails):
                return True
        return False

    def _is_private(self, run: str) -> bool:
        labels = run.split(".")
        return any(self._names_a_host(label) for label in labels) or self._names_a_subdomain(labels)

    def offsets(self, text: str) -> Iterator[int]:
        """Yield the offset of each name run in ``text`` that contains a listed name."""
        yield from _offsets(_NAME_RUN, text, self._run_verdicts, self._is_private)


class AddressDigests:
    """Find addresses inside networks that are known only by digest."""

    def __init__(self, networks: Iterable[tuple[int, str]]) -> None:
        self._networks = frozenset(networks)
        self._prefix_sizes = sorted({size for size, _ in self._networks})
        self._verdicts: dict[str, bool] = {}

    def _is_inside(self, candidate: str) -> bool:
        try:
            network = ipaddress.ip_network(candidate, strict=False)
        except ValueError:
            return False
        for size in self._prefix_sizes:
            if size > network.prefixlen:
                break
            enclosing = str(network.supernet(new_prefix=size))
            if (size, _sha256(enclosing)) in self._networks:
                return True
        return False

    def offsets(self, text: str) -> Iterator[int]:
        """Yield the offset of each address or network in ``text`` inside a listed one."""
        yield from _offsets(_IPV4, text, self._verdicts, self._is_inside)
        yield from _offsets(_IPV6, text, self._verdicts, self._is_inside)


def _offsets(
    pattern: re.Pattern[str], text: str, verdicts: dict[str, bool], decide: Callable[[str], bool]
) -> Iterator[int]:
    """Yield where ``pattern`` matches text that ``decide`` flags, deciding each once."""
    found = set()
    for candidate in set(pattern.findall(text)):
        verdict = verdicts.get(candidate)
        if verdict is None:
            verdict = verdicts[candidate] = decide(candidate)
        if verdict:
            found.add(candidate)
    if not found:
        return
    for match in pattern.finditer(text):
        if match.group() in found:
            yield match.start()


PRIVATE_NAMES = NameDigests(HOST_NAME_DIGESTS, HOST_FAMILY_DIGESTS, SUBDOMAIN_DIGESTS)
PRIVATE_ADDRESSES = AddressDigests(ADDRESS_DIGESTS)

# These are migration work streams, not publication exemptions. Remove each
# entry when its findings have moved out or been neutralized. A strict run is
# clean only when this mapping is empty and no unclassified finding remains.
PENDING: dict[str, str] = {}

GUARDS = {
    "ops/admin/private_surface_sweep.py",
    "tests/unit/ops/test_private_surface_sweep.py",
}

IGNORED_VALUES = {
    "/home/agent",
    "/home/somebody",
    # Placeholder in docs/developer/staging.md ("/home/to/dir"), not a person.
    "/home/to",
    # Synthetic user in tests and sanitized historical worktree examples.
    "/home/dev",
}

_SNIFF_BYTES = 8192


def tracked_files(repo_root: Path) -> list[str]:
    """Return the paths tracked by the repository index."""
    output = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=repo_root,
        capture_output=True,
        check=True,
    ).stdout
    return [
        name.decode()
        for name in output.split(b"\0")
        if name
        and ((repo_root / name.decode()).exists() or (repo_root / name.decode()).is_symlink())
    ]


def read_text(path: Path) -> str | None:
    """Read text regardless of suffix; return None only for NUL-bearing binary."""
    # Git stores the link target text; tracked target files are scanned separately.
    if path.is_symlink():
        return str(path.readlink())
    with path.open("rb") as stream:
        head = stream.read(_SNIFF_BYTES)
        if b"\0" in head:
            return None
        rest = stream.read()
    return (head + rest).decode("utf-8", errors="replace")


def classify(path: str) -> str | None:
    """Return the pending migration bucket that owns a path."""
    return next((prefix for prefix in PENDING if path.startswith(prefix)), None)


def findings(text: str) -> Iterator[tuple[str, int]]:
    """Yield ``(label, offset)`` for each private-surface finding in ``text``."""
    for label, pattern in PATTERNS.items():
        for match in pattern.finditer(text):
            if match.group() not in IGNORED_VALUES:
                yield label, match.start()
    for offset in PRIVATE_NAMES.offsets(text):
        yield PRIVATE_NAME_LABEL, offset
    for offset in PRIVATE_ADDRESSES.offsets(text):
        yield PRIVATE_ADDRESS_LABEL, offset


def sweep(repo_root: Path) -> tuple[dict[str, list[str]], list[str]]:
    """Return pending buckets and findings that have no migration owner."""
    buckets: dict[str, set[str]] = defaultdict(set)
    violations: list[str] = []

    for relative in tracked_files(repo_root):
        if relative in GUARDS:
            continue
        try:
            text = read_text(repo_root / relative)
        except OSError as exc:
            violations.append(f"unreadable:{relative} ({exc.strerror or exc})")
            continue
        if text is None:
            continue

        for label, offset in findings(text):
            line = text.count("\n", 0, offset) + 1
            bucket = classify(relative)
            if bucket is None:
                violations.append(f"{label}:{relative}:{line}")
            else:
                buckets[bucket].add(relative)

    return (
        {bucket: sorted(paths) for bucket, paths in buckets.items()},
        sorted(violations),
    )


def main() -> int:
    """Print the direct-tree private-surface inventory."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit non-zero while any migration bucket or violation remains",
    )
    args = parser.parse_args()
    repo_root = Path(__file__).resolve().parents[2]
    buckets, violations = sweep(repo_root)

    print("pending private-surface migration buckets:")
    for prefix, paths in sorted(buckets.items()):
        print(f"  {len(paths):4d}  {prefix:36s} {PENDING[prefix]}")
    if not buckets:
        print("  (none)")

    if violations:
        print(f"\nUNCLASSIFIED ({len(violations)}):")
        for finding in violations:
            print(f"  {finding}")
    else:
        print("\nno unclassified private-surface findings")

    print(
        f"\nreadiness: {len(violations)} unclassified, "
        f"{len(buckets)} migration bucket(s) still active."
    )
    if violations or (args.strict and buckets):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
