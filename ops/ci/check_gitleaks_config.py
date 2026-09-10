#!/usr/bin/env python3
"""Check exception scope and exercise gitleaks with synthetic credentials.

Run with the pinned CI Python (3.12) and gitleaks. Every exception file and both
archive directories receive new credentials that must still be detected.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 development environments (pytest installs tomli).
    import tomli as tomllib

REPO = Path(__file__).resolve().parents[2]


def _literal(pattern: str) -> str:
    """Decode the limited regex syntax allowed for exact fixture exceptions."""
    if not re.fullmatch(r"\^(?:[A-Za-z0-9_/=:-]|\\[.\-]|\[[A-Za-z0-9]\])+\$", pattern):
        raise ValueError("exceptions must use anchored literal paths and values")
    return re.sub(r"\\(.)|\[([A-Za-z0-9])\]", lambda m: m[1] or m[2], pattern[1:-1])


def read_exceptions(config: dict) -> dict[str, list[tuple[str, str]]]:
    """Require one file per block, AND semantics and exact secret values."""
    if "allowlist" in config:
        raise ValueError("legacy global allowlist is not supported")
    groups = [(group, group.get("targetRules", [])) for group in config.get("allowlists", [])]
    for rule in config.get("rules", []):
        if "allowlist" in rule:
            raise ValueError("legacy rule allowlist is not supported")
        groups.extend((group, [rule["id"]]) for group in rule.get("allowlists", []))

    fixtures = defaultdict(list)
    for group, rules in groups:
        if group.get("condition") != "AND":
            raise ValueError("every exception must explicitly use condition = AND")
        if len(rules) != 1 or len(group.get("paths", [])) != 1:
            raise ValueError("each exception must name exactly one rule and one file")
        if group.get("regexTarget", "secret") != "secret" or set(group) - {
            "description",
            "targetRules",
            "condition",
            "paths",
            "regexes",
            "regexTarget",
        }:
            raise ValueError("only exact secret-value exceptions are permitted")
        name = _literal(group["paths"][0])
        if Path(name).is_absolute() or ".." in Path(name).parts:
            raise ValueError("exception paths must stay inside the repository")
        if not group.get("regexes"):
            raise ValueError("an exception needs at least one literal fixture value")
        fixtures[name].extend((rules[0], _literal(pattern)) for pattern in group["regexes"])
    return dict(fixtures)


def regression_cases(fixtures: dict[str, list[tuple[str, str]]]) -> tuple[dict, Counter]:
    """Exercise all exception files, archives and the reviewed cross-file cases."""
    # Assemble values so the source file does not carry credential literals.
    payload = "".join(["Q7mKp2XvR9", "tLbN4wZcE6", "yHsA1dJfG3", "uT8oViM5rPabc"])
    sentinels = {
        "generic-api-key": payload,
        "aws-access-token": "AKIA" + "Z7F5B2Q4W6N3K2H5",
        "hybridinference-api-key": "hyi-" + payload,
        "hybridinference-grant": "agr." + payload + "." + payload,
        "hybridinference-worker-token": "ajt." + payload + "." + payload,
    }
    names = set(fixtures) | {
        "docs/agents/plans/new-plan.md",
        "docs/agents/specs/new-spec.md",
        "docs/agents/plans/archive/new-plan.md",
        "docs/agents/specs/archive/new-spec.md",
        "tests/new_worker_test.py",
    }
    cases = {}
    expected = Counter()
    for name in sorted(names):
        rows = []
        for rule, value in fixtures.get(name, []):
            if name == ".env.example" and value == "DB_ENABLED=true":
                # Exercise the blank-value false positive in its actual form,
                # without a comment between the password and boolean setting.
                rows.extend(["DB_PASSWORD=", value])
            else:
                rows.append(f'api_key = "{value}"' if rule == "generic-api-key" else value)
        for rule, value in sentinels.items():
            # Put the generic sentinel beside a permitted fixture, guarding
            # against whole-line exceptions as well as path-only exceptions.
            text = f'other_api_key = "{value}"' if rule == "generic-api-key" else value
            if rule == "generic-api-key" and rows:
                rows[-1] += "; " + text
            else:
                rows.append(text)
            expected[(name, rule)] += 1
        cases[name] = "\n".join(rows) + "\n"

    # A value approved in one file must not migrate to another file's allowlist.
    foreign = {
        "tests/servers/test_admin_provider_quotas.py": "cpk_" + "abcdef1234567890xyz",
        "tests/unit/test_admin_provider_definitions.py": "sk-zai-toggle-" + "abcdefghij12",
    }
    for name, value in foreign.items():
        cases[name] = cases.get(name, "") + f'api_key = "{value}"\n'
        expected[(name, "generic-api-key")] += 1
    return cases, expected


def check_config(config_path: Path) -> list[str]:
    """Return errors without printing credentials or scanner output."""
    try:
        fixtures = read_exceptions(tomllib.loads(config_path.read_text()))
        cases, expected = regression_cases(fixtures)
    except (ValueError, KeyError, TypeError) as exc:
        return [f"Invalid scanner exceptions: {exc}"]
    with tempfile.TemporaryDirectory(prefix="gitleaks-regression-") as temp:
        root = Path(temp)
        tree = root / "tree"
        for name, content in cases.items():
            path = tree / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        report = root / "findings.json"
        result = subprocess.run(
            [
                "gitleaks",
                "dir",
                ".",
                "--config",
                str(config_path.resolve()),
                "--redact",
                "--no-banner",
                "--report-format",
                "json",
                "--report-path",
                str(report),
            ],
            cwd=tree,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 1 or not report.exists():
            return ["Synthetic secret scan did not report the expected detections."]
        findings = json.loads(report.read_text())
        actual = Counter((item["File"], item["RuleID"]) for item in findings)
        if actual != expected:
            return [
                f"Secret scan coverage changed: missing {expected - actual}, extra {actual - expected}"
            ]
    return []


def main() -> int:
    """Fail if exceptions hide new credentials or project token formats."""
    errors = check_config(REPO / ".gitleaks.toml")
    if errors:
        print("\n".join(errors))
        return 1
    print("Secret scan regression OK: every exception file and both archives checked.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
