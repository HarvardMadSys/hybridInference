#!/usr/bin/env python3
"""Exercise the real secret scanner against safe, synthetic regression cases.

Run after installing the pinned gitleaks version. A reviewed fixture must pass,
but another key beside it, or in another design document, must still fail.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def main() -> int:
    """Fail if exceptions hide new credentials or project token formats."""
    # Assemble synthetic values so this source file does not itself look like
    # a leaked credential. No value here is accepted by a running service.
    payload = "".join(["Q7mKp2XvR9", "tLbN4wZcE6", "yHsA1dJfG3", "uT8oViM5rP"])
    cases = {
        "tests/servers/test_admin_provider_quotas.py": (
            'api_key = "key1_long_enough_1234"; other_api_key = "' + payload + '"\n'
        ),
        "docs/agents/plans/new-plan.md": "hyi-" + payload,
        "docs/agents/specs/new-spec.md": "agr." + payload + "." + payload,
        "tests/new_worker_test.py": "ajt." + payload + "." + payload,
    }
    expected = {
        ("tests/servers/test_admin_provider_quotas.py", "generic-api-key"),
        ("docs/agents/plans/new-plan.md", "hybridinference-api-key"),
        ("docs/agents/specs/new-spec.md", "hybridinference-grant"),
        ("tests/new_worker_test.py", "hybridinference-worker-token"),
    }
    with tempfile.TemporaryDirectory(prefix="gitleaks-regression-") as temp:
        root = Path(temp)
        for name, content in cases.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        report = root / "findings.json"
        result = subprocess.run(
            [
                "gitleaks",
                "dir",
                ".",
                "--config",
                str(REPO / ".gitleaks.toml"),
                "--redact",
                "--no-banner",
                "--report-format",
                "json",
                "--report-path",
                str(report),
            ],
            cwd=root,
            capture_output=True,
            text=True,
        )
        if result.returncode != 1 or not report.exists():
            print("Synthetic secret scan did not report the expected detections.")
            return 1
        findings = json.loads(report.read_text())
        actual = {(item["File"], item["RuleID"]) for item in findings}
        if actual != expected or len(findings) != len(expected):
            print(
                f"Secret scan coverage changed: expected {sorted(expected)}, got {sorted(actual)}"
            )
            return 1
    print("Secret scan regression OK: fixtures pass and new credentials are detected.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
