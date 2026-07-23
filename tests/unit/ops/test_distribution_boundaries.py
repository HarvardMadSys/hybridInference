"""Tests for upstream/distribution repository boundary checks."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from ops.ci.check_distribution_boundaries import (
    build_upstream_inventory,
    check_backend_dockerfile,
    check_brand_policy,
    check_distribution_imports,
    check_distribution_reads,
    check_dockerignore,
    check_import_smoke,
    check_ownership_coverage,
    load_policy,
    run_checks,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
POLICY_PATH = REPO_ROOT / "ops/ci/distribution_boundary_policy.json"


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.mark.parametrize(
    ("relative", "source"),
    [
        ("apps/backend/service.py", "from distributions.freeinference import settings\n"),
        (
            "apps/backend/dynamic.py",
            'from importlib import import_module\nimport_module("distributions.freeinference")\n',
        ),
        (
            "apps/frontend/client.ts",
            'import config from "../../distributions/freeinference/site";\n',
        ),
        (
            "services/worker.ts",
            'const config = await import("../distributions/acme/config");\n',
        ),
    ],
)
def test_distribution_imports_are_rejected(
    tmp_path: Path,
    relative: str,
    source: str,
) -> None:
    _write(tmp_path / relative, source)

    violations = check_distribution_imports(tmp_path, ["apps", "services"])

    assert len(violations) == 1
    assert violations[0].check == "distribution-import"
    assert relative in violations[0].path


def test_distribution_owned_sources_are_outside_import_gate(tmp_path: Path) -> None:
    _write(
        tmp_path / "distributions/freeinference/frontend.ts",
        'import config from "../distributions/freeinference/config";\n',
    )

    assert check_distribution_imports(tmp_path, ["distributions"]) == []


@pytest.mark.parametrize(
    ("relative", "source"),
    [
        (
            "apps/backend/read_path.py",
            'from pathlib import Path\nPath("../../distributions/example/config.yaml").read_text()\n',
        ),
        (
            "apps/backend/read_open.py",
            'open("distributions/example/config.yaml", encoding="utf-8")\n',
        ),
        (
            "services/read-config.mjs",
            'readFileSync("../../distributions/example/config.yaml", "utf8");\n',
        ),
    ],
)
def test_distribution_runtime_reads_are_rejected(
    tmp_path: Path,
    relative: str,
    source: str,
) -> None:
    _write(tmp_path / relative, source)

    violations = check_distribution_reads(tmp_path, ["apps/backend", "services"])

    assert len(violations) == 1
    assert violations[0].check == "distribution-read"
    assert relative in violations[0].path


def test_upstream_inventory_hides_distribution_tree(tmp_path: Path) -> None:
    _write(tmp_path / "apps/backend/serving/__init__.py", "")
    _write(tmp_path / "distributions/freeinference/distribution.yaml", "schema_version: 1\n")

    inventory = build_upstream_inventory(tmp_path, ["distributions"])

    assert "apps/backend/serving/__init__.py" in inventory
    assert not any(path.startswith("distributions/") for path in inventory)


def test_import_smoke_runs_from_physical_copy_without_distributions(tmp_path: Path) -> None:
    _write(
        tmp_path / "apps/backend/probe.py",
        "\n".join(
            [
                "from pathlib import Path",
                'parts = ["..", "..", "distributions", "example", "value.txt"]',
                "Path(*parts).read_text()",
                "",
            ]
        ),
    )
    _write(tmp_path / "distributions/example/value.txt", "must not be visible\n")

    violations = check_import_smoke(tmp_path, ["probe"])

    assert len(violations) == 1
    assert violations[0].check == "upstream-import-smoke"


@pytest.mark.parametrize(
    "copy_line",
    [
        "COPY . .\n",
        "COPY distributions/freeinference/ /app/distribution/\n",
        "COPY config/ config/\n",
        "COPY config/models.yaml config/models.yaml\n",
    ],
)
def test_backend_dockerfile_rejects_distribution_build_inputs(
    tmp_path: Path,
    copy_line: str,
) -> None:
    dockerfile = tmp_path / "deploy/docker/Dockerfile.backend"
    _write(dockerfile, f"FROM python:3.12\n{copy_line}")
    _write(tmp_path / "config/examples/distribution.yaml", "{}\n")
    inventory = build_upstream_inventory(tmp_path, ["distributions"])

    violations = check_backend_dockerfile(
        tmp_path,
        "deploy/docker/Dockerfile.backend",
        inventory,
    )

    assert len(violations) == 1
    assert violations[0].check == "dockerfile-input"


def test_backend_dockerfile_allows_neutral_examples_and_build_stage_copy(tmp_path: Path) -> None:
    _write(tmp_path / "config/examples/distribution.yaml", "{}\n")
    _write(
        tmp_path / "deploy/docker/Dockerfile.backend",
        "\n".join(
            [
                "FROM python:3.12 AS builder",
                "COPY config/examples/ config/examples/",
                "FROM python:3.12",
                "COPY --from=builder /app/.venv .venv",
                "",
            ]
        ),
    )
    inventory = build_upstream_inventory(tmp_path, ["distributions"])

    assert (
        check_backend_dockerfile(
            tmp_path,
            "deploy/docker/Dockerfile.backend",
            inventory,
        )
        == []
    )


@pytest.mark.parametrize(
    "run_line",
    [
        "RUN --mount=type=bind,source=distributions/example,target=/src true\n",
        "RUN --mount=source=.,target=/src true\n",
    ],
)
def test_backend_dockerfile_rejects_run_bind_mounts(
    tmp_path: Path,
    run_line: str,
) -> None:
    _write(tmp_path / "config/examples/distribution.yaml", "{}\n")
    dockerfile = tmp_path / "deploy/docker/Dockerfile.backend"
    _write(dockerfile, f"FROM python:3.12\n{run_line}")
    inventory = build_upstream_inventory(tmp_path, ["distributions"])

    violations = check_backend_dockerfile(
        tmp_path,
        "deploy/docker/Dockerfile.backend",
        inventory,
    )

    assert len(violations) == 1
    assert violations[0].check == "dockerfile-input"
    assert "bind mounts" in violations[0].detail


def test_dockerignore_hides_distributions_and_keeps_neutral_examples(tmp_path: Path) -> None:
    _write(
        tmp_path / ".dockerignore",
        "\n".join(
            [
                "distributions/",
                "config/*",
                "!config/examples/",
                "!config/examples/**",
                "",
            ]
        ),
    )

    assert check_dockerignore(tmp_path, ".dockerignore") == []


@pytest.mark.parametrize(
    "rules",
    [
        "config/*\n!config/examples/\n!config/examples/**\n",
        "distributions/\nconfig/*\n",
        "distributions/\nconfig/\n!config/examples/\n!config/examples/**\n",
        "distributions/\n!distributions/freeinference/\nconfig/*\n!config/examples/\n"
        "!config/examples/**\n",
    ],
)
def test_dockerignore_rejects_open_or_unusable_context_rules(
    tmp_path: Path,
    rules: str,
) -> None:
    _write(tmp_path / ".dockerignore", rules)

    assert check_dockerignore(tmp_path, ".dockerignore")


def test_ownership_coverage_rejects_new_unclassified_directory(tmp_path: Path) -> None:
    (tmp_path / "apps/backend").mkdir(parents=True)
    _write(tmp_path / "ownership.md", "# Ownership\n")
    policy = {
        "ownership_document": "ownership.md",
        "ownership_directories": {
            "apps": "mixed",
        },
    }

    violations = check_ownership_coverage(tmp_path, policy)

    assert any(
        violation.path == "apps/backend" and "no ownership" in violation.detail
        for violation in violations
    )


def test_ownership_coverage_accepts_top_and_second_level_directories(tmp_path: Path) -> None:
    (tmp_path / "apps/backend").mkdir(parents=True)
    _write(tmp_path / "ownership.md", "# Ownership\n")
    policy = {
        "ownership_document": "ownership.md",
        "ownership_directories": {
            "apps": "mixed",
            "apps/backend": "upstream",
        },
    }

    assert check_ownership_coverage(tmp_path, policy) == []


def test_ownership_coverage_rejects_unknown_and_stale_classifications(tmp_path: Path) -> None:
    (tmp_path / "apps").mkdir()
    _write(tmp_path / "ownership.md", "# Ownership\n")
    policy = {
        "ownership_document": "ownership.md",
        "ownership_directories": {
            "apps": "unknown",
            "missing": "upstream",
        },
    }

    violations = check_ownership_coverage(tmp_path, policy)

    assert any(
        violation.path == "apps" and "must be one of" in violation.detail
        for violation in violations
    )
    assert any(
        violation.path == "missing" and "stale" in violation.detail for violation in violations
    )


def _brand_policy(tmp_path: Path) -> dict[str, object]:
    _write(tmp_path / "apps/backend/service.py", 'NAME = "Acme Hosted"\n')
    return {
        "brand_rules": [{"id": "hosted-brand", "pattern": "(?i)acme hosted"}],
        "brand_scan_paths": ["apps/backend"],
        "brand_allowlist": [
            {
                "rule_id": "hosted-brand",
                "path": "apps/backend/service.py",
                "owner": "platform-maintainers",
                "reason": "Legacy default is removed in the next migration wave.",
                "expires_on": "2026-12-31",
                "match_count": 1,
            }
        ],
    }


def test_brand_allowlist_accepts_exact_owned_unexpired_debt(tmp_path: Path) -> None:
    policy = _brand_policy(tmp_path)

    assert check_brand_policy(tmp_path, policy, today=date(2026, 7, 23)) == []


@pytest.mark.parametrize("field", ["owner", "reason"])
def test_brand_allowlist_requires_accountability_fields(tmp_path: Path, field: str) -> None:
    policy = _brand_policy(tmp_path)
    policy["brand_allowlist"][0][field] = ""

    violations = check_brand_policy(tmp_path, policy, today=date(2026, 7, 23))

    assert any(field in violation.detail for violation in violations)


def test_brand_allowlist_rejects_expired_entry(tmp_path: Path) -> None:
    policy = _brand_policy(tmp_path)
    policy["brand_allowlist"][0]["expires_on"] = "2026-07-22"

    violations = check_brand_policy(tmp_path, policy, today=date(2026, 7, 23))

    assert any("expired" in violation.detail for violation in violations)


def test_brand_allowlist_rejects_growth_and_stale_counts(tmp_path: Path) -> None:
    policy = _brand_policy(tmp_path)
    _write(
        tmp_path / "apps/backend/service.py",
        'PRIMARY = "Acme Hosted"\nSECONDARY = "Acme Hosted"\n',
    )

    violations = check_brand_policy(tmp_path, policy, today=date(2026, 7, 23))

    assert any("expected 1, found 2" in violation.detail for violation in violations)


def test_brand_scan_includes_explicit_suffixless_files_and_json_directories(
    tmp_path: Path,
) -> None:
    _write(tmp_path / "Dockerfile", "RUN echo 'Acme Hosted'\n")
    _write(tmp_path / "contracts/schema.json", '{"title": "Acme Hosted"}\n')
    policy = {
        "brand_rules": [{"id": "hosted-brand", "pattern": "(?i)acme hosted"}],
        "brand_scan_paths": ["Dockerfile", "contracts"],
        "brand_allowlist": [],
    }

    violations = check_brand_policy(tmp_path, policy, today=date(2026, 7, 23))

    assert {violation.path for violation in violations} == {
        "Dockerfile",
        "contracts/schema.json",
    }


def test_current_repository_satisfies_distribution_boundary_policy() -> None:
    policy = load_policy(POLICY_PATH)

    violations = run_checks(REPO_ROOT, policy)

    assert violations == [], "\n".join(violation.render() for violation in violations)
