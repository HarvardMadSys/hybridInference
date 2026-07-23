#!/usr/bin/env python3
"""Enforce the in-repo boundary between upstream and distributions.

The checks in this module deliberately use only the Python standard library so
they can run early in CI.  They cover four invariants:

* upstream Python and JavaScript/TypeScript sources do not import a distribution;
* the backend Dockerfile only consumes files present in an upstream-only
  inventory and never copies distribution or production configuration;
* representative backend modules import while ``distributions`` is unavailable;
* known brand and production-infrastructure debt is explicit, owned, justified,
  expiring, and cannot grow unnoticed.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Sequence

SOURCE_SUFFIXES = frozenset({".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"})
TEXT_SUFFIXES = SOURCE_SUFFIXES | frozenset({".json", ".md", ".sh", ".toml", ".yaml", ".yml"})
SKIPPED_DIRECTORIES = frozenset(
    {
        ".codegraph",
        ".git",
        ".next",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "coverage",
        "dist",
        "node_modules",
    }
)
JAVASCRIPT_SPECIFIER_RE = re.compile(
    r"""
    (?:import|export)\s+
        (?:type\s+)?
        (?:[^"'`;]*?\s+from\s+)?
        ["'](?P<static>[^"']+)["']
    |
    (?:require|import)\s*\(\s*["'](?P<dynamic>[^"']+)["']\s*\)
    """,
    re.MULTILINE | re.VERBOSE,
)
JAVASCRIPT_DISTRIBUTION_READ_RE = re.compile(
    r"""
    (?:
        readFile|readFileSync|createReadStream|access|accessSync|
        stat|statSync|lstat|lstatSync|realpath|realpathSync|resolve|join
    )
    \s*\(\s*["'](?P<path>[^"']*distributions(?:[/\\][^"']*)?)["']
    """,
    re.IGNORECASE | re.MULTILINE | re.VERBOSE,
)
FORBIDDEN_DISTRIBUTION_COMPONENT_RE = re.compile(r"(?:^|/)distributions(?:/|$)")
OWNERSHIP_LABELS = frozenset({"upstream", "freeinference", "paper", "mixed", "internal"})


@dataclass(frozen=True, order=True)
class Violation:
    """One actionable repository-boundary violation."""

    check: str
    path: str
    detail: str

    def render(self) -> str:
        """Render a stable, grep-friendly diagnostic."""

        location = f" [{self.path}]" if self.path else ""
        return f"{self.check}{location}: {self.detail}"


@dataclass(frozen=True)
class DockerCopy:
    """One source in a Dockerfile COPY or ADD instruction."""

    line: int
    instruction: str
    source: str
    from_stage: bool


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _iter_files(
    root: Path, configured_paths: Iterable[str], suffixes: frozenset[str]
) -> Iterator[Path]:
    for configured in configured_paths:
        candidate = root / configured
        if candidate.is_file():
            # Explicit files are policy decisions and must never disappear
            # from a scan merely because they are suffixless (Dockerfile) or
            # introduce a new text extension.
            yield candidate
            continue
        if not candidate.is_dir():
            continue
        for path in candidate.rglob("*"):
            if any(part in SKIPPED_DIRECTORIES for part in path.parts):
                continue
            if path.is_file() and not path.is_symlink() and path.suffix.lower() in suffixes:
                yield path


def _is_distribution_specifier(specifier: str) -> bool:
    if specifier == "distributions" or specifier.startswith("distributions."):
        return True
    normalized = PurePosixPath(specifier.replace("\\", "/")).as_posix()
    while normalized.startswith("../"):
        normalized = normalized[3:]
    if normalized.startswith("./"):
        normalized = normalized[2:]
    return FORBIDDEN_DISTRIBUTION_COMPONENT_RE.search(normalized) is not None


def _python_distribution_imports(path: Path) -> tuple[list[tuple[int, str]], str | None]:
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (OSError, UnicodeDecodeError, SyntaxError) as exc:
        return [], str(exc)

    findings: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_distribution_specifier(alias.name):
                    findings.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.module and _is_distribution_specifier(node.module):
                findings.append((node.lineno, node.module))
        elif isinstance(node, ast.Call) and node.args:
            function_name = ""
            if isinstance(node.func, ast.Name):
                function_name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                function_name = node.func.attr
            first = node.args[0]
            if (
                function_name in {"__import__", "import_module"}
                and isinstance(first, ast.Constant)
                and isinstance(first.value, str)
                and _is_distribution_specifier(first.value)
            ):
                findings.append((node.lineno, first.value))
    return sorted(set(findings)), None


def _javascript_distribution_imports(path: Path) -> tuple[list[tuple[int, str]], str | None]:
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return [], str(exc)

    findings: list[tuple[int, str]] = []
    for match in JAVASCRIPT_SPECIFIER_RE.finditer(source):
        specifier = match.group("static") or match.group("dynamic")
        if _is_distribution_specifier(specifier):
            line = source.count("\n", 0, match.start()) + 1
            findings.append((line, specifier))
    return sorted(set(findings)), None


def _python_distribution_reads(path: Path) -> tuple[list[tuple[int, str]], str | None]:
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (OSError, UnicodeDecodeError, SyntaxError) as exc:
        return [], str(exc)

    path_consumers = {
        "Path",
        "PurePath",
        "PosixPath",
        "WindowsPath",
        "open",
        "join",
        "resolve",
        "abspath",
        "realpath",
    }
    findings: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            function_name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            function_name = node.func.attr
        else:
            function_name = ""
        if function_name not in path_consumers:
            continue
        for argument in node.args:
            if (
                isinstance(argument, ast.Constant)
                and isinstance(argument.value, str)
                and _is_distribution_specifier(argument.value)
            ):
                findings.append((node.lineno, argument.value))
    return sorted(set(findings)), None


def _javascript_distribution_reads(
    path: Path,
) -> tuple[list[tuple[int, str]], str | None]:
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return [], str(exc)

    findings: list[tuple[int, str]] = []
    for match in JAVASCRIPT_DISTRIBUTION_READ_RE.finditer(source):
        candidate = match.group("path")
        if _is_distribution_specifier(candidate):
            line = source.count("\n", 0, match.start()) + 1
            findings.append((line, candidate))
    return sorted(set(findings)), None


def check_distribution_imports(root: Path, scan_roots: Sequence[str]) -> list[Violation]:
    """Reject imports from ``distributions`` in upstream Python/JS/TS."""

    violations: list[Violation] = []
    for path in sorted(set(_iter_files(root, scan_roots, SOURCE_SUFFIXES))):
        if "distributions" in path.relative_to(root).parts:
            continue
        if path.suffix == ".py":
            findings, error = _python_distribution_imports(path)
        else:
            findings, error = _javascript_distribution_imports(path)
        relative = _relative(path, root)
        if error:
            violations.append(Violation("distribution-import", relative, f"cannot parse: {error}"))
            continue
        violations.extend(
            Violation(
                "distribution-import",
                f"{relative}:{line}",
                f"upstream source imports {specifier!r}",
            )
            for line, specifier in findings
        )
    return violations


def check_distribution_reads(root: Path, scan_roots: Sequence[str]) -> list[Violation]:
    """Reject literal runtime reads from a distribution in upstream sources."""

    violations: list[Violation] = []
    for path in sorted(set(_iter_files(root, scan_roots, SOURCE_SUFFIXES))):
        if "distributions" in path.relative_to(root).parts:
            continue
        if path.suffix == ".py":
            findings, error = _python_distribution_reads(path)
        else:
            findings, error = _javascript_distribution_reads(path)
        relative = _relative(path, root)
        if error:
            violations.append(Violation("distribution-read", relative, f"cannot parse: {error}"))
            continue
        violations.extend(
            Violation(
                "distribution-read",
                f"{relative}:{line}",
                f"upstream source reads distribution path {candidate!r}",
            )
            for line, candidate in findings
        )
    return violations


def _logical_dockerfile_lines(path: Path) -> Iterator[tuple[int, str]]:
    pending = ""
    start_line = 0
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = raw_line.strip()
        if not pending and (not stripped or stripped.startswith("#")):
            continue
        if not pending:
            start_line = line_number
        continued = stripped.endswith("\\")
        fragment = stripped[:-1].rstrip() if continued else stripped
        pending = f"{pending} {fragment}".strip()
        if continued:
            continue
        yield start_line, pending
        pending = ""
    if pending:
        yield start_line, pending


def _docker_copy_sources(path: Path) -> tuple[list[DockerCopy], list[Violation]]:
    copies: list[DockerCopy] = []
    violations: list[Violation] = []
    for line_number, logical_line in _logical_dockerfile_lines(path):
        match = re.match(r"^(COPY|ADD)\s+(.+)$", logical_line, re.IGNORECASE)
        if not match:
            continue
        instruction = match.group(1).upper()
        remainder = match.group(2).strip()
        from_stage = False
        while remainder.startswith("--"):
            option, separator, remainder = remainder.partition(" ")
            if not separator:
                violations.append(
                    Violation(
                        "dockerfile-input",
                        f"{path.as_posix()}:{line_number}",
                        f"malformed {instruction} instruction",
                    )
                )
                break
            if option.startswith("--from="):
                from_stage = True
            remainder = remainder.lstrip()
        else:
            try:
                if remainder.startswith("["):
                    values = json.loads(remainder)
                    if not isinstance(values, list) or not all(
                        isinstance(value, str) for value in values
                    ):
                        raise ValueError("JSON form must be a string array")
                    tokens = values
                else:
                    tokens = shlex.split(remainder, comments=True, posix=True)
            except (json.JSONDecodeError, ValueError) as exc:
                violations.append(
                    Violation(
                        "dockerfile-input",
                        f"{path.as_posix()}:{line_number}",
                        f"cannot parse {instruction}: {exc}",
                    )
                )
                continue
            if len(tokens) < 2:
                violations.append(
                    Violation(
                        "dockerfile-input",
                        f"{path.as_posix()}:{line_number}",
                        f"{instruction} requires at least one source and one destination",
                    )
                )
                continue
            copies.extend(
                DockerCopy(line_number, instruction, source, from_stage) for source in tokens[:-1]
            )
    return copies, violations


def _normalize_docker_source(source: str) -> str:
    normalized = PurePosixPath(source.replace("\\", "/")).as_posix()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.rstrip("/")


def _source_exists_in_inventory(source: str, inventory: set[str]) -> bool:
    normalized = _normalize_docker_source(source)
    if any(character in normalized for character in "*?["):
        import fnmatch

        return any(fnmatch.fnmatch(path, normalized) for path in inventory)
    if normalized in inventory:
        return True
    prefix = f"{normalized}/"
    return any(path.startswith(prefix) for path in inventory)


def build_upstream_inventory(root: Path, excluded_prefixes: Sequence[str]) -> set[str]:
    """Return a synthetic checkout inventory with distribution roots hidden."""

    normalized_prefixes = tuple(f"{prefix.strip('/')}/" for prefix in excluded_prefixes)
    inventory: set[str] = set()
    for path in root.rglob("*"):
        if any(part in SKIPPED_DIRECTORIES for part in path.parts):
            continue
        if not path.is_file() or path.is_symlink():
            continue
        relative = _relative(path, root)
        if relative == "distributions" or relative.startswith(normalized_prefixes):
            continue
        inventory.add(relative)
    return inventory


def check_upstream_inventory(
    root: Path,
    required_paths: Sequence[str],
    excluded_prefixes: Sequence[str],
) -> tuple[set[str], list[Violation]]:
    """Validate the file-list form of an upstream-only synthetic checkout."""

    inventory = build_upstream_inventory(root, excluded_prefixes)
    violations: list[Violation] = []
    for required in required_paths:
        normalized = required.strip("/")
        if normalized not in inventory and not any(
            path.startswith(f"{normalized}/") for path in inventory
        ):
            violations.append(
                Violation(
                    "upstream-inventory",
                    normalized,
                    "required upstream build/import input is missing",
                )
            )
    if any(path == "distributions" or path.startswith("distributions/") for path in inventory):
        violations.append(
            Violation(
                "upstream-inventory",
                "distributions",
                "synthetic upstream inventory unexpectedly contains a distribution",
            )
        )
    return inventory, violations


def check_backend_dockerfile(
    root: Path,
    dockerfile: str,
    inventory: set[str],
) -> list[Violation]:
    """Ensure the backend Dockerfile has only neutral upstream build inputs."""

    path = root / dockerfile
    try:
        copies, violations = _docker_copy_sources(path)
    except (OSError, UnicodeDecodeError) as exc:
        return [Violation("dockerfile-input", dockerfile, f"cannot read Dockerfile: {exc}")]

    for line_number, logical_line in _logical_dockerfile_lines(path):
        if re.match(r"^RUN\s+", logical_line, re.IGNORECASE) and re.search(
            r"--mount=(?:[^\s]*,)?(?:type=bind(?:,|\s|$)|(?=[^\s]*\bsource=))",
            logical_line,
            re.IGNORECASE,
        ):
            violations.append(
                Violation(
                    "dockerfile-input",
                    f"{dockerfile}:{line_number}",
                    "RUN bind mounts can consume files hidden from COPY/ADD inventory checks",
                )
            )

    for copy in copies:
        if copy.from_stage:
            continue
        source = _normalize_docker_source(copy.source)
        location = f"{dockerfile}:{copy.line}"
        if source in {"", "."}:
            violations.append(
                Violation(
                    "dockerfile-input",
                    location,
                    f"{copy.instruction} of the repository root can include distributions",
                )
            )
            continue
        if FORBIDDEN_DISTRIBUTION_COMPONENT_RE.search(source):
            violations.append(
                Violation(
                    "dockerfile-input",
                    location,
                    f"{copy.instruction} must not consume distribution path {copy.source!r}",
                )
            )
            continue
        if source == "config" or (
            source.startswith("config/")
            and source != "config/examples"
            and not source.startswith("config/examples/")
        ):
            violations.append(
                Violation(
                    "dockerfile-input",
                    location,
                    f"{copy.instruction} must not bake production config {copy.source!r}",
                )
            )
            continue
        if not _source_exists_in_inventory(source, inventory):
            violations.append(
                Violation(
                    "dockerfile-input",
                    location,
                    f"{copy.instruction} source {copy.source!r} is absent upstream-only",
                )
            )
    return violations


def check_dockerignore(root: Path, dockerignore: str) -> list[Violation]:
    """Ensure the shared Docker context hides distributions and real config."""

    path = root / dockerignore
    try:
        rules = [
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
    except (OSError, UnicodeDecodeError) as exc:
        return [Violation("dockerignore", dockerignore, f"cannot read: {exc}")]

    violations: list[Violation] = []
    distribution_excludes = {"distributions", "distributions/", "distributions/**"}
    if not any(rule in distribution_excludes for rule in rules):
        violations.append(
            Violation(
                "dockerignore",
                dockerignore,
                "must exclude the distributions tree from the shared build context",
            )
        )
    if any(rule.startswith("!distributions") for rule in rules):
        violations.append(
            Violation(
                "dockerignore",
                dockerignore,
                "must not re-include distribution content",
            )
        )

    config_excludes = {"config/*", "config/**"}
    config_exclude_positions = [
        index for index, rule in enumerate(rules) if rule in config_excludes
    ]
    example_directory_rules = {"!config/examples", "!config/examples/"}
    example_tree_rules = {"!config/examples/**", "!config/examples/**/*"}
    example_directory_positions = [
        index for index, rule in enumerate(rules) if rule in example_directory_rules
    ]
    example_tree_positions = [
        index for index, rule in enumerate(rules) if rule in example_tree_rules
    ]
    if not config_exclude_positions:
        violations.append(
            Violation(
                "dockerignore",
                dockerignore,
                "must exclude config contents from the shared build context",
            )
        )
    if not example_directory_positions or not example_tree_positions:
        violations.append(
            Violation(
                "dockerignore",
                dockerignore,
                "must re-include config/examples and its contents",
            )
        )
    elif config_exclude_positions and (
        min(example_directory_positions) < max(config_exclude_positions)
        or min(example_tree_positions) < max(config_exclude_positions)
    ):
        violations.append(
            Violation(
                "dockerignore",
                dockerignore,
                "config/examples re-inclusion rules must follow the config exclusion",
            )
        )
    if "config" in rules or "config/" in rules:
        violations.append(
            Violation(
                "dockerignore",
                dockerignore,
                "excluding the config directory itself prevents examples from being re-included",
            )
        )
    return violations


def _classified_directories(root: Path) -> set[str]:
    directories: set[str] = set()
    for top_level in root.iterdir():
        if (
            not top_level.is_dir()
            or top_level.is_symlink()
            or top_level.name in SKIPPED_DIRECTORIES
        ):
            continue
        directories.add(top_level.name)
        for second_level in top_level.iterdir():
            if (
                second_level.is_dir()
                and not second_level.is_symlink()
                and second_level.name not in SKIPPED_DIRECTORIES
            ):
                directories.add(f"{top_level.name}/{second_level.name}")
    return directories


def check_ownership_coverage(root: Path, policy: dict[str, Any]) -> list[Violation]:
    """Require a classification for every top-level and second-level directory."""

    raw_ownership = policy.get("ownership_directories")
    if not isinstance(raw_ownership, dict):
        return [
            Violation(
                "ownership-coverage",
                "policy",
                "ownership_directories must be an object",
            )
        ]

    violations: list[Violation] = []
    ownership: dict[str, str] = {}
    for raw_path, raw_label in raw_ownership.items():
        if not isinstance(raw_path, str) or not raw_path.strip():
            violations.append(
                Violation(
                    "ownership-coverage",
                    "policy",
                    "ownership path must be a non-empty string",
                )
            )
            continue
        normalized = PurePosixPath(raw_path.strip("/")).as_posix()
        if (
            normalized.startswith("../")
            or normalized.startswith("/")
            or len(PurePosixPath(normalized).parts) not in {1, 2}
        ):
            violations.append(
                Violation(
                    "ownership-coverage",
                    raw_path,
                    "classification path must be a top-level or second-level directory",
                )
            )
            continue
        if raw_label not in OWNERSHIP_LABELS:
            violations.append(
                Violation(
                    "ownership-coverage",
                    normalized,
                    f"classification must be one of {sorted(OWNERSHIP_LABELS)}",
                )
            )
            continue
        ownership[normalized] = raw_label

    actual = _classified_directories(root)
    classified = set(ownership)
    for path in sorted(actual - classified):
        violations.append(
            Violation(
                "ownership-coverage",
                path,
                "new directory has no ownership classification",
            )
        )
    for path in sorted(classified - actual):
        violations.append(
            Violation(
                "ownership-coverage",
                path,
                "classification is stale because the directory does not exist",
            )
        )

    ownership_document = policy.get("ownership_document")
    if not isinstance(ownership_document, str) or not ownership_document:
        violations.append(
            Violation(
                "ownership-coverage",
                "policy",
                "ownership_document must be a non-empty string",
            )
        )
    elif not (root / ownership_document).is_file():
        violations.append(
            Violation(
                "ownership-coverage",
                ownership_document,
                "ownership document is missing",
            )
        )
    return violations


def _validate_nonempty_string(
    entry: dict[str, Any],
    field: str,
    location: str,
    violations: list[Violation],
) -> str:
    value = entry.get(field)
    if not isinstance(value, str) or not value.strip():
        violations.append(
            Violation("brand-allowlist", location, f"{field} must be a non-empty string")
        )
        return ""
    return value.strip()


def check_brand_policy(
    root: Path,
    policy: dict[str, Any],
    *,
    today: date | None = None,
) -> list[Violation]:
    """Enforce exact, expiring allowances for brand/production tokens."""

    today = today or date.today()
    violations: list[Violation] = []
    rules_data = policy.get("brand_rules")
    allowlist_data = policy.get("brand_allowlist")
    if not isinstance(rules_data, list) or not isinstance(allowlist_data, list):
        return [
            Violation(
                "brand-allowlist",
                "policy",
                "brand_rules and brand_allowlist must both be arrays",
            )
        ]

    rules: dict[str, re.Pattern[str]] = {}
    for index, raw_rule in enumerate(rules_data):
        location = f"brand_rules[{index}]"
        if not isinstance(raw_rule, dict):
            violations.append(Violation("brand-allowlist", location, "rule must be an object"))
            continue
        rule_id = _validate_nonempty_string(raw_rule, "id", location, violations)
        pattern = _validate_nonempty_string(raw_rule, "pattern", location, violations)
        if not rule_id or not pattern:
            continue
        if rule_id in rules:
            violations.append(Violation("brand-allowlist", location, f"duplicate id {rule_id!r}"))
            continue
        try:
            rules[rule_id] = re.compile(pattern)
        except re.error as exc:
            violations.append(
                Violation("brand-allowlist", location, f"invalid regex for {rule_id!r}: {exc}")
            )

    allowances: dict[tuple[str, str], int] = {}
    for index, raw_entry in enumerate(allowlist_data):
        location = f"brand_allowlist[{index}]"
        if not isinstance(raw_entry, dict):
            violations.append(Violation("brand-allowlist", location, "entry must be an object"))
            continue
        rule_id = _validate_nonempty_string(raw_entry, "rule_id", location, violations)
        path = _validate_nonempty_string(raw_entry, "path", location, violations)
        _validate_nonempty_string(raw_entry, "owner", location, violations)
        _validate_nonempty_string(raw_entry, "reason", location, violations)
        expires_on = _validate_nonempty_string(raw_entry, "expires_on", location, violations)
        match_count = raw_entry.get("match_count")
        if not isinstance(match_count, int) or isinstance(match_count, bool) or match_count < 1:
            violations.append(
                Violation("brand-allowlist", location, "match_count must be a positive integer")
            )
        if rule_id and rule_id not in rules:
            violations.append(
                Violation("brand-allowlist", location, f"unknown rule_id {rule_id!r}")
            )
        if expires_on:
            try:
                expiry = date.fromisoformat(expires_on)
            except ValueError:
                violations.append(
                    Violation(
                        "brand-allowlist",
                        location,
                        "expires_on must use ISO YYYY-MM-DD format",
                    )
                )
            else:
                if expiry < today:
                    violations.append(
                        Violation(
                            "brand-allowlist",
                            location,
                            f"allowance expired on {expires_on}",
                        )
                    )
        key = (rule_id, path)
        if rule_id and path and isinstance(match_count, int) and match_count > 0:
            if key in allowances:
                violations.append(
                    Violation("brand-allowlist", location, f"duplicate allowance for {key!r}")
                )
            allowances[key] = match_count

    actual: dict[tuple[str, str], int] = {}
    scan_paths = policy.get("brand_scan_paths", [])
    if not isinstance(scan_paths, list) or not all(isinstance(item, str) for item in scan_paths):
        violations.append(
            Violation("brand-allowlist", "policy", "brand_scan_paths must be a string array")
        )
        return violations
    for path in sorted(set(_iter_files(root, scan_paths, TEXT_SUFFIXES))):
        relative = _relative(path, root)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            violations.append(Violation("brand-scan", relative, f"cannot read: {exc}"))
            continue
        for rule_id, pattern in rules.items():
            count = sum(1 for _ in pattern.finditer(text))
            if count:
                actual[(rule_id, relative)] = count

    for key in sorted(actual.keys() | allowances.keys()):
        actual_count = actual.get(key, 0)
        allowed_count = allowances.get(key)
        rule_id, path = key
        if allowed_count is None:
            violations.append(
                Violation(
                    "brand-scan",
                    path,
                    f"{rule_id} has {actual_count} unallowlisted match(es)",
                )
            )
        elif not actual_count:
            violations.append(
                Violation(
                    "brand-allowlist",
                    path,
                    f"stale {rule_id} allowance expects {allowed_count} match(es)",
                )
            )
        elif actual_count != allowed_count:
            violations.append(
                Violation(
                    "brand-scan",
                    path,
                    f"{rule_id} count changed: expected {allowed_count}, found {actual_count}",
                )
            )
    return violations


def check_import_smoke(root: Path, modules: Sequence[str]) -> list[Violation]:
    """Import representative backend modules from a detached upstream copy."""

    script = """
import importlib.abc
import sys

class DistributionBlocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "distributions" or fullname.startswith("distributions."):
            raise ImportError(f"distribution import blocked: {fullname}")
        return None

sys.meta_path.insert(0, DistributionBlocker())
sys.path.insert(0, sys.argv[1])
for module in sys.argv[2:]:
    __import__(module)
"""
    backend_root = root / "apps/backend"
    try:
        with tempfile.TemporaryDirectory(prefix="hybridinference-upstream-") as raw_temp:
            detached_root = Path(raw_temp)
            detached_backend = detached_root / "apps/backend"
            shutil.copytree(
                backend_root,
                detached_backend,
                ignore=shutil.ignore_patterns(*SKIPPED_DIRECTORIES),
            )
            result = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-c",
                    script,
                    str(detached_backend),
                    *modules,
                ],
                cwd=detached_backend,
                capture_output=True,
                text=True,
                check=False,
            )
    except OSError as exc:
        return [
            Violation(
                "upstream-import-smoke",
                "apps/backend",
                f"cannot materialize detached upstream copy: {exc}",
            )
        ]
    if result.returncode == 0:
        return []
    detail = (result.stderr or result.stdout).strip().splitlines()
    summary = detail[-1] if detail else f"subprocess exited {result.returncode}"
    return [Violation("upstream-import-smoke", "apps/backend", summary)]


def load_policy(path: Path) -> dict[str, Any]:
    """Load the versioned JSON boundary policy."""

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot load boundary policy {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"boundary policy {path} must be a JSON object")
    if data.get("schema_version") != 1:
        raise ValueError(f"boundary policy {path} has unsupported schema_version")
    return data


def run_checks(
    root: Path,
    policy: dict[str, Any],
    *,
    run_import_smoke: bool = True,
    today: date | None = None,
) -> list[Violation]:
    """Run every repository-boundary check and return all violations."""

    violations: list[Violation] = []
    import_roots = policy.get("import_scan_paths", [])
    read_roots = policy.get("distribution_read_scan_paths", [])
    required_paths = policy.get("upstream_required_paths", [])
    excluded_prefixes = policy.get("upstream_excluded_prefixes", ["distributions"])
    smoke_modules = policy.get("import_smoke_modules", [])
    dockerfile = policy.get("backend_dockerfile", "deploy/docker/Dockerfile.backend")
    dockerignore = policy.get("dockerignore", ".dockerignore")
    string_arrays = {
        "import_scan_paths": import_roots,
        "distribution_read_scan_paths": read_roots,
        "upstream_required_paths": required_paths,
        "upstream_excluded_prefixes": excluded_prefixes,
        "import_smoke_modules": smoke_modules,
    }
    for name, value in string_arrays.items():
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            violations.append(Violation("boundary-policy", name, "must be a string array"))
    if not isinstance(dockerfile, str) or not dockerfile:
        violations.append(
            Violation("boundary-policy", "backend_dockerfile", "must be a non-empty string")
        )
    if not isinstance(dockerignore, str) or not dockerignore:
        violations.append(
            Violation("boundary-policy", "dockerignore", "must be a non-empty string")
        )
    if violations:
        return violations

    violations.extend(check_distribution_imports(root, import_roots))
    violations.extend(check_distribution_reads(root, read_roots))
    inventory, inventory_violations = check_upstream_inventory(
        root,
        required_paths,
        excluded_prefixes,
    )
    violations.extend(inventory_violations)
    violations.extend(check_backend_dockerfile(root, dockerfile, inventory))
    violations.extend(check_dockerignore(root, dockerignore))
    violations.extend(check_ownership_coverage(root, policy))
    violations.extend(check_brand_policy(root, policy, today=today))
    if run_import_smoke:
        violations.extend(check_import_smoke(root, smoke_modules))
    return sorted(set(violations))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    default_root = Path(__file__).resolve().parents[2]
    parser.add_argument("--repo-root", type=Path, default=default_root)
    parser.add_argument(
        "--policy",
        type=Path,
        default=default_root / "ops/ci/distribution_boundary_policy.json",
    )
    parser.add_argument(
        "--skip-import-smoke",
        action="store_true",
        help="skip the isolated representative-module import subprocess",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point."""

    args = _build_parser().parse_args(argv)
    root = args.repo_root.resolve()
    try:
        policy = load_policy(args.policy.resolve())
    except ValueError as exc:
        print(f"boundary-policy: {exc}", file=sys.stderr)
        return 2
    violations = run_checks(root, policy, run_import_smoke=not args.skip_import_smoke)
    if violations:
        print("Distribution boundary check failed:", file=sys.stderr)
        for violation in violations:
            print(f"- {violation.render()}", file=sys.stderr)
        return 1
    print("Distribution boundary check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
