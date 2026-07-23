"""Gates for the versioned stable-control surface and frontend rewrites."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from contracts.openapi.generate_control_snapshot import (
    CONTROL_ERROR_CODES,
    generate_allowlist,
    generate_snapshot,
    validate_control_dependency_policy,
)
from contracts.openapi.generate_frontend_operation_inventory import (
    generate_inventory,
    rewrite_covers_path,
)
from serving.servers.app import create_app
from serving.servers.middleware.exception_handler import http_control_error_code

REPO_ROOT = Path(__file__).resolve().parents[2]
INVENTORY_PATH = REPO_ROOT / "contracts/frontend-backend-routes.v1.json"
OPERATION_INVENTORY_PATH = REPO_ROOT / "contracts/frontend-backend-operations.v1.json"
POLICY_PATH = REPO_ROOT / "contracts/openapi/control-v1.policy.json"
ALLOWLIST_PATH = REPO_ROOT / "contracts/openapi/control-v1.allowlist.json"
SNAPSHOT_PATH = REPO_ROOT / "contracts/openapi/control-v1.openapi.json"
REQUIRED_REWRITE_BASES = {
    "/admin",
    "/anthropic",
    "/auth",
    "/capabilities",
    "/control",
    "/health",
    "/internal/playground",
    "/site-config",
    "/site-updates",
    "/user",
    "/v1",
}


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _route_base(source: str) -> str:
    return source.removesuffix("/:path*")


def _inventory_route_for_path(
    inventory: dict[str, Any],
    path: str,
) -> dict[str, Any] | None:
    for route in inventory["routes"]:
        base = _route_base(route["source"])
        if path == base or path.startswith(f"{base}/"):
            return route
    return None


@pytest.fixture(scope="module")
def inventory() -> dict[str, Any]:
    return _load_json(INVENTORY_PATH)


@pytest.fixture(scope="module")
def allowlist() -> dict[str, Any]:
    return _load_json(ALLOWLIST_PATH)


@pytest.fixture(scope="module")
def operation_inventory() -> dict[str, Any]:
    return _load_json(OPERATION_INVENTORY_PATH)


@pytest.fixture(scope="module")
def control_policy() -> dict[str, Any]:
    return _load_json(POLICY_PATH)


@pytest.fixture(scope="module")
def control_app():
    return create_app()


@pytest.fixture(scope="module")
def full_openapi(control_app) -> dict[str, Any]:
    return control_app.openapi()


def test_frontend_backend_rewrite_inventory_is_explicit_and_unique(
    inventory: dict[str, Any],
) -> None:
    declared = [(route["source"], route["destination"]) for route in inventory["routes"]]

    assert declared
    assert len(declared) == len(set(declared)), "route inventory contains duplicate rewrites"

    allowed_classifications = set(inventory["classification_values"])
    assert allowed_classifications == {
        "stable-control",
        "inference-protocol",
        "compatibility",
        "distribution-private",
        "ops-only",
    }
    for route in inventory["routes"]:
        assert route["classification"] in allowed_classifications
        assert route["notes"].strip()


def test_required_frontend_backend_prefixes_are_explicitly_inventoried(
    inventory: dict[str, Any],
) -> None:
    bases = {_route_base(route["source"]) for route in inventory["routes"]}
    assert bases >= REQUIRED_REWRITE_BASES


def test_allowlist_contains_only_stable_control_routes(
    inventory: dict[str, Any],
    operation_inventory: dict[str, Any],
    allowlist: dict[str, Any],
) -> None:
    for operation in allowlist["operations"]:
        route = _inventory_route_for_path(inventory, operation["path"])
        assert route is not None, f"{operation['path']} has no frontend route classification"
        assert route["classification"] == "stable-control"

    stable_operations = {
        (operation["path"], operation["method"])
        for operation in operation_inventory["operations"]
        if operation["classification"] == "stable-control"
    }
    allowlisted_operations = {
        (operation["path"], operation["method"]) for operation in allowlist["operations"]
    }
    assert allowlisted_operations == stable_operations


def test_checked_in_operation_inventory_expands_every_rewrite_covered_operation(
    inventory: dict[str, Any],
    operation_inventory: dict[str, Any],
    full_openapi: dict[str, Any],
) -> None:
    assert operation_inventory == generate_inventory(full_openapi, inventory)

    actual_keys = {
        (operation["path"], operation["method"]) for operation in operation_inventory["operations"]
    }
    expected_keys: set[tuple[str, str]] = set()
    for path, path_item in full_openapi["paths"].items():
        rewrites = [
            route for route in inventory["routes"] if rewrite_covers_path(route["source"], path)
        ]
        assert len(rewrites) <= 1, f"{path} matched overlapping frontend rewrites"
        if not rewrites:
            continue
        rewrite = rewrites[0]
        for method, operation in path_item.items():
            if method not in {
                "delete",
                "get",
                "head",
                "options",
                "patch",
                "post",
                "put",
                "trace",
            }:
                continue
            key = (path, method)
            expected_keys.add(key)
            classified = next(
                item
                for item in operation_inventory["operations"]
                if (item["path"], item["method"]) == key
            )
            assert classified["classification"] == rewrite["classification"]
            assert classified["rewrite_source"] == rewrite["source"]
            if rewrite["classification"] == "stable-control":
                assert classified["operation_id"] == operation["operationId"]
            else:
                assert "operation_id" not in classified

    assert actual_keys == expected_keys


def test_control_allowlist_is_the_explicit_policy_expansion(
    full_openapi: dict[str, Any],
    control_policy: dict[str, Any],
    allowlist: dict[str, Any],
) -> None:
    assert allowlist == generate_allowlist(full_openapi, control_policy)


def test_control_auth_and_permission_policy_matches_actual_dependencies(
    control_app,
    allowlist: dict[str, Any],
) -> None:
    validate_control_dependency_policy(control_app, allowlist)


def test_checked_in_control_openapi_snapshot_matches_app(
    full_openapi: dict[str, Any],
    allowlist: dict[str, Any],
) -> None:
    checked_in = _load_json(SNAPSHOT_PATH)
    assert checked_in == generate_snapshot(full_openapi, allowlist)


@pytest.mark.parametrize(
    ("path", "method", "status_code"),
    [
        ("/admin/broadcast-email/test", "post", "200"),
        ("/admin/broadcast-email/{broadcast_id}", "delete", "200"),
        ("/admin/site-updates/{update_id}", "delete", "200"),
    ],
)
def test_snapshot_generator_rejects_empty_non_204_success_schema(
    full_openapi: dict[str, Any],
    allowlist: dict[str, Any],
    path: str,
    method: str,
    status_code: str,
) -> None:
    mutated = copy.deepcopy(full_openapi)
    mutated["paths"][path][method]["responses"][status_code]["content"]["application/json"][
        "schema"
    ] = {}

    with pytest.raises(ValueError, match="must declare a non-empty schema"):
        generate_snapshot(mutated, allowlist)


@pytest.mark.parametrize(
    ("path", "method", "expected_media_type"),
    [
        ("/admin/export/requests", "get", "application/x-ndjson"),
        ("/control/v1/playground/chat", "post", "text/event-stream"),
    ],
)
def test_snapshot_generator_rejects_streaming_media_type_drift(
    full_openapi: dict[str, Any],
    allowlist: dict[str, Any],
    path: str,
    method: str,
    expected_media_type: str,
) -> None:
    mutated = copy.deepcopy(full_openapi)
    success = mutated["paths"][path][method]["responses"]["200"]
    media_contract = success["content"].pop(expected_media_type)
    success["content"]["application/octet-stream"] = media_contract

    with pytest.raises(ValueError, match="streaming response 200 media type changed"):
        generate_snapshot(mutated, allowlist)


def test_allowlisted_operation_ids_are_unique_and_paths_are_exact(
    full_openapi: dict[str, Any],
    allowlist: dict[str, Any],
) -> None:
    declared_keys: set[tuple[str, str]] = set()
    operation_ids: set[str] = set()
    for operation in allowlist["operations"]:
        key = (operation["path"], operation["method"].lower())
        assert key not in declared_keys
        declared_keys.add(key)

        actual = full_openapi["paths"][key[0]][key[1]]
        operation_id = actual["operationId"]
        assert operation_id == operation["operation_id"]
        assert operation_id not in operation_ids
        operation_ids.add(operation_id)
        assert bool(actual.get("deprecated", False)) is operation["deprecated"]

    snapshot = _load_json(SNAPSHOT_PATH)
    snapshot_keys = {
        (path, method) for path, path_item in snapshot["paths"].items() for method in path_item
    }
    assert snapshot_keys == declared_keys


def test_control_snapshot_contains_its_recursive_schema_reference_closure() -> None:
    snapshot = _load_json(SNAPSHOT_PATH)

    def refs(value: Any) -> set[str]:
        found: set[str] = set()
        if isinstance(value, dict):
            if isinstance(value.get("$ref"), str):
                found.add(value["$ref"])
            for nested in value.values():
                found.update(refs(nested))
        elif isinstance(value, list):
            for nested in value:
                found.update(refs(nested))
        return found

    for ref in refs(snapshot):
        assert ref.startswith("#/")
        current: Any = snapshot
        for part in ref[2:].split("/"):
            current = current[part.replace("~1", "/").replace("~0", "~")]


def test_every_stable_operation_declares_the_control_error_envelope() -> None:
    snapshot = _load_json(SNAPSHOT_PATH)
    error_codes: set[tuple[str, ...]] = set()
    for path_item in snapshot["paths"].values():
        for operation in path_item.values():
            default_response = operation["responses"]["default"]
            assert default_response["content"]["application/json"]["schema"] == {
                "$ref": "#/components/schemas/ControlErrorResponse"
            }
            if "422" in operation["responses"]:
                assert operation["responses"]["422"] == default_response
            error_codes.add(tuple(operation["x-control-error-codes"]))

    assert len(error_codes) == 1
    assert {"AUTHENTICATION_REQUIRED", "VALIDATION_ERROR", "INTERNAL_ERROR"} <= set(
        error_codes.pop()
    )


@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 409, 422, 429, 500, 503, 599])
def test_runtime_http_error_fallback_codes_are_in_the_openapi_vocabulary(
    status_code: int,
) -> None:
    assert http_control_error_code(status_code) in CONTROL_ERROR_CODES


@pytest.mark.asyncio
async def test_real_control_routes_return_stable_http_and_validation_errors(
    control_app,
) -> None:
    from routing.executor import RouteExecutor
    from serving.servers.deps import AppServices

    control_app.state.services = AppServices(router=RouteExecutor())
    transport = ASGITransport(app=control_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        missing_auth = await client.get("/user/me")
        missing_route = await client.get("/user/not-a-real-operation")
        invalid_body = await client.post("/auth/signup", json={})

    assert missing_auth.status_code == 401
    assert missing_auth.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"
    assert missing_route.status_code == 404
    assert missing_route.json()["error"]["code"] == "NOT_FOUND"
    assert invalid_body.status_code == 422
    assert invalid_body.json()["error"] == {
        "code": "VALIDATION_ERROR",
        "message": "Request validation failed",
        "details": {
            "issues": [
                {
                    "location": issue["location"],
                    "type": issue["type"],
                    "message": issue["message"],
                }
                for issue in invalid_body.json()["error"]["details"]["issues"]
            ]
        },
    }
    for response in (missing_auth, missing_route, invalid_body):
        body = response.json()
        assert isinstance(body["error"]["code"], str)
        assert body["request_id"] == response.headers["x-request-id"]
