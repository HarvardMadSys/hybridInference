"""Tests for the deterministic Phase 2 repository and evidence contracts."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path

import pytest

from ops.ci.generate_phase2_repo_baseline import (
    DEFAULT_OUTPUT,
    FRONTEND_HTML_SMOKE_SPEC,
    build_repo_baseline,
    collect_frontend_pages,
    ddl_sources_not_fingerprinted,
    main,
    render_repo_baseline,
    scan_ddl_sources,
)
from ops.ci.validate_phase2_deployment_evidence import (
    DeploymentEvidenceValidationError,
    validate_phase2_deployment_evidence,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
EVIDENCE_ROOT = REPO_ROOT / "contracts/evidence"


def _captured_evidence() -> dict:
    digest = f"sha256:{'a' * 64}"
    smoke_result = {"result": "passed", "report_reference": "run:smoke"}
    return {
        "schema_version": 1,
        "evidence_status": "captured",
        "deployment_target": "phase2-staging",
        "environment": "staging",
        "source_build": {
            "build_kind": "source-build",
            "source_revision": "b" * 40,
            "repo_baseline_sha256": digest,
            "local_image_id": digest,
            "repository_digest": None,
            "built_at": "2026-07-23T08:00:00Z",
            "build_log_reference": "run:build",
        },
        "selected_runtime_manifest": {
            "distribution_id": "freeinference",
            "schema_version": 2,
            "sha256": digest,
            "selectors": {
                "models": "shadow",
                "routing": "legacy",
                "alerts": "legacy",
            },
            "startup_result": "passed",
            "startup_comparison_reference": "artifact:comparison",
            "startup_hash_log_reference": "artifact:hash-log",
        },
        "live_database": {
            "repo_ddl_source_fingerprint": digest,
            "live_schema_fingerprint": digest,
            "server": "postgresql",
            "server_version": "17.5",
            "captured_at": "2026-07-23T08:15:00Z",
            "capture_reference": "artifact:database-catalog",
            "backup": {
                "backup_id": "phase2-staging-001",
                "captured_at": "2026-07-23T08:30:00Z",
                "location_reference": "backup:phase2-staging-001",
                "integrity_sha256": digest,
                "restore_readiness_verified": True,
            },
        },
        "dark_load": {
            "result": "passed",
            "window_start": "2026-07-23T09:00:00Z",
            "window_end": "2026-07-23T09:30:00Z",
            "request_count": 100,
            "success_count": 99,
            "success_rate": 0.99,
            "required_success_rate": 0.99,
            "production_traffic_mutated": False,
            "report_reference": "run:dark-load",
        },
        "smoke_tests": {
            "overall_result": "passed",
            "frontend_html": {
                "result": "passed",
                "spec_sha256": digest,
                "route_count": 26,
                "passed_route_count": 26,
                "failure_count": 0,
                "report_reference": "run:frontend-html",
            },
            "api": dict(smoke_result),
            "sse": dict(smoke_result),
            "auth": dict(smoke_result),
            "quota": dict(smoke_result),
            "model": dict(smoke_result),
            "routing": dict(smoke_result),
        },
        "metrics": {
            "result": "passed",
            "window_start": "2026-07-23T09:00:00Z",
            "window_end": "2026-07-23T10:00:00Z",
            "request_count": 100,
            "critical_alert_count": 0,
            "bucket_dimensions": ["model", "provider", "client"],
            "bucket_baselines": {
                dimension: {
                    "latency_report_reference": f"artifact:{dimension}-latency",
                    "error_report_reference": f"artifact:{dimension}-error",
                    "cost_report_reference": f"artifact:{dimension}-cost",
                }
                for dimension in ("model", "provider", "client")
            },
            "abort_thresholds": {
                "latency_p95_ms": 1000,
                "error_rate": 0.05,
                "cost_usd": 100,
            },
            "observed_maxima": {
                "latency_p95_ms": 900,
                "error_rate": 0.01,
                "cost_usd": 50,
            },
            "dashboard_reference": "dashboard:phase2",
        },
        "rollback": {
            "result": "passed",
            "mode": "rehearsal",
            "target": {
                "known_good_source_revision": "c" * 40,
                "local_image_id": digest,
                "record_reference": "artifact:known-good",
            },
            "execution_reference": "run:rollback",
            "duration_seconds": 30,
            "recovery_verified": True,
            "database_restore_required": False,
        },
        "attestation": {
            "operator": "operator-a",
            "reviewer": "reviewer-b",
            "recorded_at": "2026-07-23T10:30:00Z",
        },
    }


def _deployment_schema() -> dict:
    return json.loads(
        (EVIDENCE_ROOT / "phase2-deployment-evidence.schema.json").read_text(encoding="utf-8")
    )


def test_repo_baseline_generation_is_deterministic_and_checked_in() -> None:
    first = render_repo_baseline(build_repo_baseline(REPO_ROOT))
    second = render_repo_baseline(build_repo_baseline(REPO_ROOT))

    assert first == second
    assert DEFAULT_OUTPUT.read_text(encoding="utf-8") == first


def test_check_mode_rejects_a_stale_snapshot(tmp_path: Path, capsys) -> None:
    stale = tmp_path / "phase2-wave0.repo.json"
    stale.write_text("{}\n", encoding="utf-8")

    assert main(["--repo-root", str(REPO_ROOT), "--output", str(stale), "--check"]) == 1
    assert "omits DDL sources" in capsys.readouterr().out


def test_repo_baseline_contains_no_runtime_values_or_nondeterministic_metadata() -> None:
    baseline = build_repo_baseline(REPO_ROOT)
    rendered = render_repo_baseline(baseline)

    assert str(REPO_ROOT) not in rendered
    assert "${" not in rendered
    assert "ZAI_API_KEY" not in rendered
    assert "STAGING_API_KEY" not in rendered
    assert not re.search(r"/(?:Users|home|srv|tmp)/", rendered)
    assert not re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:", rendered)
    assert not ({"head", "commit", "generated_at", "timestamp"} & set(baseline))
    assert baseline["evidence_scope"] == "repository-only-not-deployment-evidence"


def test_ddl_scanner_fingerprints_every_detected_source() -> None:
    baseline = build_repo_baseline(REPO_ROOT)
    detected = scan_ddl_sources(REPO_ROOT)
    recorded = baseline["database_ddl"]["sources"]

    assert recorded == detected
    assert baseline["database_ddl"]["source_count"] == len(detected)
    assert ddl_sources_not_fingerprinted(REPO_ROOT, baseline) == []
    assert all(source["statement_count"] > 0 for source in detected)


def test_ddl_scanner_reports_a_new_unfingerprinted_source(tmp_path: Path) -> None:
    source = tmp_path / "apps/backend/new_schema.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        'DDL = "CREATE TABLE example (id INTEGER); CREATE INDEX example_id ON example(id)"\n',
        encoding="utf-8",
    )

    detected = scan_ddl_sources(tmp_path)
    assert [item["path"] for item in detected] == ["apps/backend/new_schema.py"]
    assert detected[0]["statement_count"] == 2
    assert ddl_sources_not_fingerprinted(tmp_path, {"database_ddl": {"sources": []}}) == [
        "apps/backend/new_schema.py"
    ]


def test_frontend_html_smoke_spec_covers_every_page_route() -> None:
    pages = collect_frontend_pages(REPO_ROOT)
    spec = json.loads((REPO_ROOT / FRONTEND_HTML_SMOKE_SPEC).read_text(encoding="utf-8"))
    routes = spec["routes"]

    assert spec["evidence_status"] == "specification-only"
    assert spec["execution_contract"]["live_or_source_build_execution_required"] is True
    assert {route["path"] for route in routes} == {page["route"] for page in pages}
    assert len(routes) == len(pages)
    assert all(route["marker"]["kind"].startswith("visible-text") for route in routes)
    for route in routes:
        if route["access"] == "public":
            assert route["http"]["accepted_statuses"] == [200]
            assert "redirect" not in route
        else:
            assert route["access"] == "protected"
            assert route["redirect"]["required_final_path"] == "/login"
            assert route["http"]["required_final_status"] == 200


def test_deployment_evidence_schema_requires_captured_observations() -> None:
    schema = _deployment_schema()

    assert schema["additionalProperties"] is False
    assert schema["properties"]["evidence_status"]["const"] == "captured"
    assert set(schema["required"]) == {
        "schema_version",
        "evidence_status",
        "deployment_target",
        "environment",
        "source_build",
        "selected_runtime_manifest",
        "live_database",
        "dark_load",
        "smoke_tests",
        "metrics",
        "rollback",
        "attestation",
    }

    source_build = schema["properties"]["source_build"]
    assert {"local_image_id", "repository_digest"} <= set(source_build["required"])
    assert source_build["properties"]["build_kind"] == {"const": "source-build"}
    assert {"type": "null"} in source_build["properties"]["repository_digest"]["oneOf"]

    runtime = schema["properties"]["selected_runtime_manifest"]
    assert {"distribution_id", "schema_version", "selectors"} <= set(runtime["required"])
    assert runtime["properties"]["startup_result"] == {"const": "passed"}
    assert set(runtime["properties"]["selectors"]["required"]) == {
        "models",
        "routing",
        "alerts",
    }

    live_database = schema["properties"]["live_database"]
    assert {"live_schema_fingerprint", "server", "server_version", "backup"} <= set(
        live_database["required"]
    )
    assert live_database["properties"]["backup"]["properties"]["restore_readiness_verified"] == {
        "const": True
    }

    for section in ("dark_load", "metrics", "rollback"):
        assert schema["properties"][section]["properties"]["result"] == {"const": "passed"}

    smoke = schema["properties"]["smoke_tests"]
    assert smoke["properties"]["overall_result"] == {"const": "passed"}
    assert {"frontend_html", "api", "sse", "auth", "quota", "model", "routing"} <= set(
        smoke["required"]
    )
    assert smoke["properties"]["frontend_html"]["properties"]["failure_count"] == {"const": 0}
    assert {"success_count", "required_success_rate"} <= set(
        schema["properties"]["dark_load"]["required"]
    )

    metrics = schema["properties"]["metrics"]
    dimensions = metrics["properties"]["bucket_dimensions"]["prefixItems"]
    assert [item["const"] for item in dimensions] == ["model", "provider", "client"]
    assert set(metrics["properties"]["bucket_baselines"]["required"]) == {
        "model",
        "provider",
        "client",
    }
    assert {"latency_p95_ms", "error_rate", "cost_usd"} <= set(
        metrics["properties"]["abort_thresholds"]["required"]
    )
    assert {"latency_p95_ms", "error_rate", "cost_usd"} <= set(
        metrics["properties"]["observed_maxima"]["required"]
    )
    assert metrics["properties"]["critical_alert_count"] == {"const": 0}

    rollback_target = schema["properties"]["rollback"]["properties"]["target"]
    assert {"known_good_source_revision", "local_image_id"} <= set(rollback_target["required"])
    assert schema["properties"]["rollback"]["properties"]["recovery_verified"] == {"const": True}


def test_captured_deployment_evidence_passes_structural_and_relational_validation() -> None:
    validate_phase2_deployment_evidence(_captured_evidence(), _deployment_schema())


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (("dark_load", "success_rate"), 0.01, "success_rate must equal"),
        (("dark_load", "required_success_rate"), 1.0, "below required_success_rate"),
        (
            ("smoke_tests", "frontend_html", "passed_route_count"),
            1,
            "every declared route must pass",
        ),
        (("dark_load", "window_end"), "2026-07-23T08:59:00Z", "window_end must be later"),
        (("metrics", "critical_alert_count"), 1, "schema const check failed"),
        (
            ("metrics", "observed_maxima", "latency_p95_ms"),
            1001,
            "exceeds abort threshold",
        ),
    ],
)
def test_captured_deployment_evidence_rejects_false_green_relations(
    path: tuple[str, ...],
    value,
    message: str,
) -> None:
    evidence = deepcopy(_captured_evidence())
    target = evidence
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value

    with pytest.raises(DeploymentEvidenceValidationError, match=message):
        validate_phase2_deployment_evidence(evidence, _deployment_schema())


def test_operator_template_is_explicitly_not_evidence() -> None:
    template = (EVIDENCE_ROOT / "phase2-deployment-evidence.template.md").read_text(
        encoding="utf-8"
    )

    assert "evidence_status: template" in template
    assert "not evidence" in template
    assert "live or source-build run report is still required" in template
    assert "required_success_rate" in template
    assert "observed_maxima" in template
