#!/usr/bin/env python3
"""Generate the deterministic stable-control OpenAPI subset.

The full FastAPI document intentionally contains inference, compatibility,
internal, and operator-only routes.  This generator copies only explicitly
classified control operations and the component definitions reachable from
their request/response schemas.  The checked-in allowlist is generated from a
small auditable auth/permission policy so every operation remains explicit.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
from pathlib import Path
from typing import Any

from serving.servers.app import create_app

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_POLICY = REPO_ROOT / "contracts/openapi/control-v1.policy.json"
DEFAULT_ALLOWLIST = REPO_ROOT / "contracts/openapi/control-v1.allowlist.json"
DEFAULT_SNAPSHOT = REPO_ROOT / "contracts/openapi/control-v1.openapi.json"
HTTP_METHODS = frozenset({"delete", "get", "head", "options", "patch", "post", "put", "trace"})
OPERATION_FIELDS = (
    "operationId",
    "parameters",
    "requestBody",
    "responses",
    "security",
)
PATH_CONVERTER_PATTERN = re.compile(r"{([^}:]+):[^}]+}")
CONTROL_ERROR_CODES = (
    "ACCOUNT_SUSPENDED",
    "API_KEY_NOT_FOUND",
    "AUTHENTICATION_REQUIRED",
    "BAD_REQUEST",
    "CONFLICT",
    "DUPLICATE_API_KEY",
    "EMAIL_NOT_VERIFIED",
    "HTTP_ERROR",
    "INTERNAL_ERROR",
    "INVALID_CREDENTIALS",
    "INVALID_TOKEN",
    "METHOD_NOT_ALLOWED",
    "NOT_FOUND",
    "PERMISSION_DENIED",
    "QUOTA_EXCEEDED",
    "RATE_LIMITED",
    "SERVICE_UNAVAILABLE",
    "SESSION_NOT_FOUND",
    "SESSION_REVOKED",
    "TOKEN_ALREADY_USED",
    "TOKEN_EXPIRED",
    "UPSTREAM_TIMEOUT",
    "UPSTREAM_UNAVAILABLE",
    "USER_ALREADY_EXISTS",
    "USER_NOT_FOUND",
    "VALIDATION_ERROR",
    "WEAK_PASSWORD",
)


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _resolve_local_ref(document: dict[str, Any], ref: str) -> Any:
    if not ref.startswith("#/"):
        raise ValueError(f"control snapshot cannot contain an external reference: {ref}")

    value: Any = document
    for raw_part in ref[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        value = value[part]
    return value


def _find_refs(value: Any) -> set[str]:
    refs: set[str] = set()
    if isinstance(value, dict):
        ref = value.get("$ref")
        if isinstance(ref, str):
            refs.add(ref)
        for nested in value.values():
            refs.update(_find_refs(nested))
    elif isinstance(value, list):
        for nested in value:
            refs.update(_find_refs(nested))
    return refs


def _insert_ref(snapshot: dict[str, Any], ref: str, value: Any) -> None:
    parts = [part.replace("~1", "/").replace("~0", "~") for part in ref[2:].split("/")]
    target = snapshot
    for part in parts[:-1]:
        target = target.setdefault(part, {})
    target[parts[-1]] = copy.deepcopy(value)


def _copy_reference_closure(source: dict[str, Any], snapshot: dict[str, Any]) -> None:
    copied: set[str] = set()
    pending = _find_refs(snapshot)
    while pending:
        ref = pending.pop()
        if ref in copied:
            continue
        value = _resolve_local_ref(source, ref)
        _insert_ref(snapshot, ref, value)
        copied.add(ref)
        pending.update(_find_refs(value) - copied)


def _rule_matches_path(rule: dict[str, Any], path: str) -> bool:
    match_type = rule["path_match"]
    if match_type == "exact":
        return path == rule["path"]
    if match_type == "prefix":
        return path.startswith(rule["path"])
    raise ValueError(f"unsupported control policy path_match: {match_type!r}")


def _iter_concrete_routes(routes: list[Any]):
    pending = list(routes)
    visited: set[int] = set()
    while pending:
        route = pending.pop()
        if id(route) in visited:
            continue
        visited.add(id(route))
        original_router = getattr(route, "original_router", None)
        nested = getattr(original_router, "routes", None)
        if nested:
            pending.extend(nested)
            continue
        if getattr(route, "path", None) and getattr(route, "methods", None):
            yield route


def _openapi_route_path(path: str) -> str:
    """Normalize Starlette path converters to their OpenAPI path form."""
    return PATH_CONVERTER_PATTERN.sub(r"{\1}", path)


def _dependency_contracts(dependant: Any) -> tuple[set[str], set[str]]:
    auth: set[str] = set()
    permission: set[str] = set()
    pending = list(getattr(dependant, "dependencies", []))
    visited: set[int] = set()
    while pending:
        dependency = pending.pop()
        if id(dependency) in visited:
            continue
        visited.add(id(dependency))
        call = getattr(dependency, "call", None)
        auth_marker = getattr(call, "__control_auth__", None)
        permission_marker = getattr(call, "__control_permission__", None)
        if isinstance(auth_marker, str):
            auth.add(auth_marker)
        if isinstance(permission_marker, str):
            permission.add(permission_marker)
        pending.extend(getattr(dependency, "dependencies", []))
    return auth, permission


def _dependency_inputs(dependant: Any) -> tuple[set[str], set[str], set[str], set[str]]:
    """Collect cookie/query/header names and body-model fields recursively."""
    cookies: set[str] = set()
    queries: set[str] = set()
    headers: set[str] = set()
    body_fields: set[str] = set()
    pending = [dependant]
    visited: set[int] = set()
    while pending:
        dependency = pending.pop()
        if id(dependency) in visited:
            continue
        visited.add(id(dependency))
        cookies.update(parameter.name for parameter in dependency.cookie_params)
        queries.update(parameter.name for parameter in dependency.query_params)
        headers.update(parameter.name for parameter in dependency.header_params)
        for parameter in dependency.body_params:
            annotation = getattr(parameter.field_info, "annotation", None)
            model_fields = getattr(annotation, "model_fields", None)
            if isinstance(model_fields, dict):
                body_fields.update(model_fields)
            else:
                body_fields.add(parameter.name)
        pending.extend(getattr(dependency, "dependencies", []))
    return cookies, queries, headers, body_fields


def validate_control_dependency_policy(app: Any, allowlist: dict[str, Any]) -> None:
    """Reject auth/permission policy claims not backed by actual dependencies."""
    route_index: dict[tuple[str, str], Any] = {}
    for route in _iter_concrete_routes(app.routes):
        for method in route.methods:
            route_index[(_openapi_route_path(route.path), method.lower())] = route

    violations: list[str] = []
    for operation in allowlist["operations"]:
        key = (operation["path"], operation["method"].lower())
        route = route_index.get(key)
        if route is None:
            violations.append(f"{operation['method'].upper()} {operation['path']}: route missing")
            continue
        actual_auth, actual_permissions = _dependency_contracts(route.dependant)
        cookies, queries, headers, body_fields = _dependency_inputs(route.dependant)
        expected_auth = operation["auth"]
        expected_permission = operation["permission"]

        required_auth = {
            "bearer-jwt": "bearer-jwt",
            "bearer-jwt+refresh-cookie": "bearer-jwt",
            "bearer-jwt-or-admin-token": "bearer-jwt-or-admin-token",
        }.get(expected_auth)
        if required_auth and required_auth not in actual_auth:
            violations.append(
                f"{operation['method'].upper()} {operation['path']}: "
                f"policy auth {expected_auth!r} lacks dependency marker {required_auth!r}"
            )
        if (
            expected_auth
            in {
                "none",
                "credentials-body",
                "refresh-cookie",
                "verification-token",
                "reset-token",
            }
            and actual_auth
        ):
            violations.append(
                f"{operation['method'].upper()} {operation['path']}: "
                f"policy auth {expected_auth!r} conflicts with {sorted(actual_auth)}"
            )

        required_transport: tuple[str, str] | None = {
            "credentials-body": ("body", "email"),
            "refresh-cookie": ("cookie", "refresh_token"),
            "verification-token": ("query", "token"),
            "reset-token": ("body", "token"),
        }.get(expected_auth)
        inputs = {
            "body": body_fields,
            "cookie": cookies,
            "header": headers,
            "query": queries,
        }
        if required_transport:
            transport, name = required_transport
            if name not in inputs[transport]:
                violations.append(
                    f"{operation['method'].upper()} {operation['path']}: "
                    f"policy auth {expected_auth!r} lacks {transport} field {name!r}"
                )
        if expected_auth == "credentials-body" and "password" not in body_fields:
            violations.append(
                f"{operation['method'].upper()} {operation['path']}: "
                "credentials-body auth lacks body field 'password'"
            )
        if expected_auth == "bearer-jwt+refresh-cookie" and "refresh_token" not in cookies:
            violations.append(
                f"{operation['method'].upper()} {operation['path']}: "
                "bearer-jwt+refresh-cookie auth lacks cookie field 'refresh_token'"
            )

        required_permission = {
            "active-user": "active-user",
            "active-verified-user": "active-user",
            "internal-or-admin": "internal-or-admin",
            "admin": "admin",
        }.get(expected_permission)
        if required_permission and required_permission not in actual_permissions:
            violations.append(
                f"{operation['method'].upper()} {operation['path']}: "
                f"policy permission {expected_permission!r} lacks dependency marker "
                f"{required_permission!r}"
            )
    if violations:
        raise ValueError("control dependency policy mismatch:\n- " + "\n- ".join(violations))


def generate_allowlist(
    openapi: dict[str, Any],
    policy: dict[str, Any],
) -> dict[str, Any]:
    """Expand control auth/permission rules into explicit registered operations."""
    exceptions: dict[tuple[str, str], dict[str, Any]] = {}
    for exception in policy["exceptions"]:
        key = (exception["path"], exception["method"].lower())
        if key in exceptions:
            raise ValueError(f"duplicate control policy exception: {key}")
        exceptions[key] = exception

    streaming_responses: dict[tuple[str, str], dict[str, Any]] = {}
    for response in policy.get("streaming_responses", []):
        method = response["method"].lower()
        key = (response["path"], method)
        if key in streaming_responses:
            raise ValueError(f"duplicate streaming response policy: {key}")
        status_code = response["status_code"]
        media_type = response["media_type"]
        if method not in HTTP_METHODS:
            raise ValueError(f"unsupported streaming response method: {method}")
        if not isinstance(status_code, int) or not 200 <= status_code < 300:
            raise ValueError(f"invalid streaming response status for {key}: {status_code!r}")
        if not isinstance(media_type, str) or not media_type:
            raise ValueError(f"invalid streaming response media type for {key}: {media_type!r}")
        streaming_responses[key] = {
            "status_code": status_code,
            "media_type": media_type,
        }

    operations: list[dict[str, Any]] = []
    used_exceptions: set[tuple[str, str]] = set()
    used_streaming_responses: set[tuple[str, str]] = set()
    for path, path_item in sorted(openapi["paths"].items()):
        matching_rules = [rule for rule in policy["rules"] if _rule_matches_path(rule, path)]
        if len(matching_rules) > 1:
            matches = [rule["path"] for rule in matching_rules]
            raise ValueError(f"control path {path!r} matches overlapping policy rules: {matches}")
        if not matching_rules:
            continue

        rule = matching_rules[0]
        for method, operation in sorted(path_item.items()):
            if method not in HTTP_METHODS:
                continue
            key = (path, method)
            effective = exceptions.get(key, rule)
            if key in exceptions:
                used_exceptions.add(key)
            operation_id = operation.get("operationId")
            if not isinstance(operation_id, str) or not operation_id:
                raise ValueError(f"{method.upper()} {path} has no operationId")
            declared = {
                "path": path,
                "method": method,
                "operation_id": operation_id,
                "auth": effective["auth"],
                "permission": effective["permission"],
                "deprecated": bool(operation.get("deprecated", False)),
                "policy_source": (
                    f"exception:{method.upper()} {path}"
                    if key in exceptions
                    else f"{rule['path_match']}:{rule['path']}"
                ),
            }
            if key in streaming_responses:
                declared["streaming_response"] = streaming_responses[key]
                used_streaming_responses.add(key)
            operations.append(declared)

    unused_exceptions = set(exceptions) - used_exceptions
    if unused_exceptions:
        raise ValueError(f"control policy has unused exceptions: {sorted(unused_exceptions)}")
    unused_streaming_responses = set(streaming_responses) - used_streaming_responses
    if unused_streaming_responses:
        raise ValueError(
            f"control policy has unused streaming responses: {sorted(unused_streaming_responses)}"
        )
    if not operations:
        raise ValueError("control policy matched no registered operations")
    return {
        "schema_version": policy["schema_version"],
        "control_api_version": policy["control_api_version"],
        "policy_file": "contracts/openapi/control-v1.policy.json",
        "operations": operations,
    }


def _validate_success_responses(
    path: str,
    method: str,
    operation: dict[str, Any],
    declared: dict[str, Any],
) -> None:
    """Reject underspecified success bodies and declared streaming drift."""
    responses = operation.get("responses")
    if not isinstance(responses, dict):
        raise ValueError(f"{method.upper()} {path} has no response contract")

    success_responses: dict[int, dict[str, Any]] = {}
    for raw_status, response in responses.items():
        try:
            status_code = int(raw_status)
        except (TypeError, ValueError):
            continue
        if 200 <= status_code < 300:
            if not isinstance(response, dict):
                raise ValueError(f"{method.upper()} {path} response {status_code} is not an object")
            success_responses[status_code] = response

    if not success_responses:
        raise ValueError(f"{method.upper()} {path} declares no successful response")

    for status_code, response in success_responses.items():
        if status_code == 204:
            continue
        content = response.get("content")
        if not isinstance(content, dict) or not content:
            raise ValueError(
                f"{method.upper()} {path} response {status_code} "
                "must declare response content with a non-empty schema"
            )
        for media_type, media_contract in content.items():
            schema = media_contract.get("schema") if isinstance(media_contract, dict) else None
            if not isinstance(schema, dict) or not schema:
                raise ValueError(
                    f"{method.upper()} {path} response {status_code} media type "
                    f"{media_type!r} must declare a non-empty schema"
                )

    streaming_response = declared.get("streaming_response")
    if not streaming_response:
        return
    status_code = streaming_response["status_code"]
    expected_media_type = streaming_response["media_type"]
    response = success_responses.get(status_code)
    actual_media_types = set(response.get("content", {})) if isinstance(response, dict) else set()
    if actual_media_types != {expected_media_type}:
        raise ValueError(
            f"{method.upper()} {path} streaming response {status_code} media type changed: "
            f"expected {expected_media_type!r}, got {sorted(actual_media_types)}"
        )


def generate_snapshot(
    openapi: dict[str, Any],
    allowlist: dict[str, Any],
) -> dict[str, Any]:
    """Return a canonical OpenAPI subset for the configured stable operations."""
    operations = allowlist.get("operations")
    if not isinstance(operations, list) or not operations:
        raise ValueError("control allowlist must contain at least one operation")

    snapshot: dict[str, Any] = {
        "openapi": openapi["openapi"],
        "info": {
            "title": "HybridInference Stable Control API",
            "version": allowlist["control_api_version"],
        },
        "paths": {},
    }

    seen_operations: set[tuple[str, str]] = set()
    seen_operation_ids: set[str] = set()
    for declared in sorted(operations, key=lambda item: (item["path"], item["method"])):
        path = declared["path"]
        method = declared["method"].lower()
        key = (path, method)
        if method not in HTTP_METHODS:
            raise ValueError(f"unsupported HTTP method in control allowlist: {method}")
        if key in seen_operations:
            raise ValueError(f"duplicate control operation: {method.upper()} {path}")
        seen_operations.add(key)

        try:
            operation = openapi["paths"][path][method]
        except KeyError as exc:
            raise ValueError(
                f"allowlisted operation is not registered: {method.upper()} {path}"
            ) from exc

        actual_operation_id = operation.get("operationId")
        expected_operation_id = declared["operation_id"]
        if actual_operation_id != expected_operation_id:
            raise ValueError(
                f"{method.upper()} {path} operationId changed: "
                f"expected {expected_operation_id!r}, got {actual_operation_id!r}"
            )
        if expected_operation_id in seen_operation_ids:
            raise ValueError(f"duplicate allowlisted operationId: {expected_operation_id}")
        seen_operation_ids.add(expected_operation_id)

        actual_deprecated = bool(operation.get("deprecated", False))
        expected_deprecated = declared["deprecated"]
        if actual_deprecated != expected_deprecated:
            raise ValueError(
                f"{method.upper()} {path} deprecation changed: "
                f"expected {expected_deprecated}, got {actual_deprecated}"
            )

        _validate_success_responses(path, method, operation, declared)

        canonical_operation: dict[str, Any] = {
            "operationId": actual_operation_id,
            "deprecated": actual_deprecated,
            "x-control-auth": declared["auth"],
            "x-control-permission": declared["permission"],
        }
        for field in OPERATION_FIELDS:
            if field in operation and field != "operationId":
                canonical_operation[field] = copy.deepcopy(operation[field])
        snapshot["paths"].setdefault(path, {})[method] = canonical_operation

    _copy_reference_closure(openapi, snapshot)
    snapshot.setdefault("components", {}).setdefault("schemas", {}).update(
        {
            "ControlError": {
                "type": "object",
                "additionalProperties": False,
                "required": ["code", "details", "message"],
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "Stable machine-readable error code.",
                    },
                    "details": {
                        "type": "object",
                        "additionalProperties": True,
                        "description": "Error-specific structured details; never required for routing.",
                    },
                    "message": {
                        "type": "string",
                        "description": "Human-readable diagnostic; clients must branch on code.",
                    },
                },
            },
            "ControlErrorResponse": {
                "type": "object",
                "required": ["error", "request_id"],
                "properties": {
                    "error": {"$ref": "#/components/schemas/ControlError"},
                    "request_id": {
                        "type": "string",
                        "description": "Request correlation identifier.",
                    },
                },
            },
        }
    )
    error_response = {
        "description": "Stable control-plane error response",
        "content": {
            "application/json": {"schema": {"$ref": "#/components/schemas/ControlErrorResponse"}}
        },
    }
    for path_item in snapshot["paths"].values():
        for operation in path_item.values():
            operation["x-control-error-codes"] = list(CONTROL_ERROR_CODES)
            responses = operation.setdefault("responses", {})
            if "422" in responses:
                responses["422"] = copy.deepcopy(error_response)
            responses["default"] = copy.deepcopy(error_response)
    return snapshot


def render_snapshot(snapshot: dict[str, Any]) -> str:
    """Serialize a snapshot in the single canonical repository format."""
    return json.dumps(snapshot, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def render_allowlist(allowlist: dict[str, Any]) -> str:
    """Serialize an expanded allowlist in canonical repository format."""
    return json.dumps(allowlist, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> int:
    """Generate or verify the stable control OpenAPI artifacts."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--allowlist", type=Path, default=DEFAULT_ALLOWLIST)
    parser.add_argument("--output", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail when the checked-in snapshot differs instead of rewriting it",
    )
    args = parser.parse_args()

    app = create_app()
    openapi = app.openapi()
    allowlist = generate_allowlist(openapi, _load_json(args.policy))
    validate_control_dependency_policy(app, allowlist)
    rendered_allowlist = render_allowlist(allowlist)
    rendered_snapshot = render_snapshot(generate_snapshot(openapi, allowlist))
    if args.check:
        stale: list[str] = []
        if (
            not args.allowlist.is_file()
            or args.allowlist.read_text(encoding="utf-8") != rendered_allowlist
        ):
            stale.append(str(args.allowlist))
        if (
            not args.output.is_file()
            or args.output.read_text(encoding="utf-8") != rendered_snapshot
        ):
            stale.append(str(args.output))
        if stale:
            raise SystemExit(
                f"stable-control generated contracts are stale ({', '.join(stale)}); run "
                "`uv run python contracts/openapi/generate_control_snapshot.py`"
            )
        return 0

    args.allowlist.parent.mkdir(parents=True, exist_ok=True)
    args.allowlist.write_text(rendered_allowlist, encoding="utf-8")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered_snapshot, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
