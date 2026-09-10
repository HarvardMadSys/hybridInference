"""Regression coverage for the scanner's exception policy and probe matrix."""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from ops.ci import check_gitleaks_config as check

CONFIG = check.REPO / ".gitleaks.toml"
BASE = check.tomllib.loads(CONFIG.read_text())
GROUPS = [(None, i) for i in range(len(BASE["allowlists"]))] + [
    (i, j) for i, rule in enumerate(BASE["rules"]) for j in range(len(rule.get("allowlists", [])))
]


@pytest.mark.parametrize("rule_index,group_index", GROUPS)
def test_each_exception_requires_explicit_and(rule_index, group_index):
    config = copy.deepcopy(BASE)
    groups = (
        config["allowlists"] if rule_index is None else config["rules"][rule_index]["allowlists"]
    )
    groups[group_index].pop("condition")
    with pytest.raises(ValueError, match="AND"):
        check.read_exceptions(config)


def test_archive_wide_allowlist_is_rejected_even_with_and():
    config = copy.deepcopy(BASE)
    group = copy.deepcopy(config["allowlists"][0])
    group["paths"] = [r"^docs/agents/(plans|specs)/archive/.*\.md$"]
    config["allowlists"].append(group)
    with pytest.raises(ValueError, match="literal"):
        check.read_exceptions(config)


def test_multiple_paths_cannot_share_a_set_of_values():
    config = copy.deepcopy(BASE)
    config["allowlists"][0]["paths"].append(r"^tests/another_file\.py$")
    with pytest.raises(ValueError, match="one rule and one file"):
        check.read_exceptions(config)


@pytest.mark.parametrize("field,value", [("regexTarget", "line"), ("commits", ["anything"])])
def test_exception_cannot_hide_an_entire_line_or_commit(field, value):
    config = copy.deepcopy(BASE)
    config["allowlists"][0][field] = value
    with pytest.raises(ValueError, match="secret-value"):
        check.read_exceptions(config)


def test_probes_cover_every_exception_file_and_both_archives():
    fixtures = check.read_exceptions(BASE)
    cases, expected = check.regression_cases(fixtures)
    for name in fixtures:
        assert name in cases
        assert expected[(name, "generic-api-key")] >= 1
        assert expected[(name, "aws-access-token")] == 1
        for rule in (
            "hybridinference-api-key",
            "hybridinference-grant",
            "hybridinference-worker-token",
        ):
            assert expected[(name, rule)] == 1
    for directory in ("plans", "specs"):
        assert any(name.startswith(f"docs/agents/{directory}/archive/") for name in cases)
    assert "DB_PASSWORD=\nDB_ENABLED=true" in cases[".env.example"]
    # These values were incorrectly shared between files before the review.
    assert expected[("tests/servers/test_admin_provider_quotas.py", "generic-api-key")] == 2
    assert expected[("tests/unit/test_admin_provider_definitions.py", "generic-api-key")] == 2


@pytest.mark.parametrize("exit_code", [0, 1, 2])
def test_missing_detections_never_pass(monkeypatch, exit_code):
    def run(command, **kwargs):
        report = Path(command[command.index("--report-path") + 1])
        report.write_text(json.dumps([]))
        return subprocess.CompletedProcess(command, exit_code, "", "")

    monkeypatch.setattr(check.subprocess, "run", run)
    assert check.check_config(CONFIG)


@pytest.mark.skipif(
    shutil.which("gitleaks") is None, reason="real scanner also runs in Security Scan"
)
def test_real_scanner_detects_new_keys_and_cross_file_fixtures():
    assert check.check_config(CONFIG) == []
