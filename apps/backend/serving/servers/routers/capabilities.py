"""Public discovery contract for effective control-plane capabilities."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel

from serving.config.distribution import get_distribution_capability_expectations
from serving.config.settings import get_settings
from serving.rag.config import load_rag_settings
from serving.utils.logging import get_logger

router = APIRouter(tags=["Capabilities"])
logger = get_logger(__name__)

_expectation_states: dict[str, tuple[bool, bool]] = {}


class CapabilitiesResponse(BaseModel):
    """Versioned, append-only capability discovery document."""

    schema_version: Literal[1] = 1
    control_api_version: Literal["1.0"] = "1.0"
    capabilities: dict[str, bool]


def _route_is_registered(request: Request, path: str, method: str) -> bool:
    """Return whether the application registered an exact path and method."""
    expected_method = method.upper()
    pending = list(request.app.routes)
    visited: set[int] = set()
    while pending:
        route = pending.pop()
        if id(route) in visited:
            continue
        visited.add(id(route))
        if getattr(route, "path", None) == path and expected_method in (
            getattr(route, "methods", None) or set()
        ):
            return True
        original_router = getattr(route, "original_router", None)
        nested_routes = getattr(original_router, "routes", None)
        if nested_routes:
            pending.extend(nested_routes)
    return False


async def _effective_bool(services: Any, key: str, fallback: bool) -> bool:
    """Resolve a runtime setting, failing closed if its configured store fails."""
    runtime_settings = getattr(services, "runtime_settings", None)
    if runtime_settings is None:
        return fallback
    try:
        return bool(await runtime_settings.get_bool(key))
    except Exception:
        return False


async def _operational_store_is_ready(services: Any) -> bool:
    """Actively verify the store needed by browser auth and user operations."""
    store = getattr(services, "operational_store", None)
    health_check = getattr(store, "health_check", None)
    if health_check is None:
        return False
    try:
        return bool(await health_check())
    except Exception:
        return False


def _playground_is_ready(services: Any) -> bool:
    router_exec = getattr(services, "router", None)
    routes = getattr(router_exec, "routes", None)
    if not isinstance(routes, dict):
        return False
    return any(
        getattr(route, "published", True)
        and any(weight > 0 for _adapter, weight in getattr(route, "adapters", []))
        for route in routes.values()
    )


def _routewise_is_ready(services: Any) -> bool:
    registry = getattr(services, "model_router_registry", None)
    if registry is None:
        return False
    try:
        model_ids = dict.fromkeys(
            [
                *registry.configured_model_ids(),
                *registry.registered_models(),
            ]
        )
        return any(registry.get_router_name(model_id) == "routewise" for model_id in model_ids)
    except Exception:
        return False


def _rag_is_ready() -> bool:
    try:
        settings = load_rag_settings()
        return bool(settings.api_key) and settings.index_path.is_file()
    except Exception:
        return False


async def effective_capabilities(request: Request) -> dict[str, bool]:
    """Compute public capabilities from routes, effective settings, and readiness."""
    services = getattr(request.app.state, "services", None)
    settings = get_settings()

    auth_enabled = await _effective_bool(
        services,
        "user_auth_enabled",
        settings.user_auth_enabled,
    )
    signup_enabled = await _effective_bool(
        services,
        "signup_enabled",
        settings.signup_enabled,
    )
    email_verification_required = await _effective_bool(
        services,
        "signup_require_email_verification",
        settings.signup_require_email_verification,
    )

    database_ready = await _operational_store_is_ready(services)
    runtime_settings_ready = getattr(services, "runtime_settings", None) is not None
    password_auth = (
        auth_enabled and database_ready and _route_is_registered(request, "/auth/login", "POST")
    )
    admin_core = (
        database_ready
        and runtime_settings_ready
        and _route_is_registered(request, "/admin/settings", "GET")
    )

    return {
        "auth.password": password_auth,
        "auth.public_signup": (
            password_auth
            and signup_enabled
            and _route_is_registered(request, "/auth/signup", "POST")
        ),
        "auth.email_verification": (
            password_auth
            and email_verification_required
            and bool(settings.smtp_user and settings.smtp_password)
            and _route_is_registered(request, "/auth/verify-email", "GET")
            and _route_is_registered(request, "/auth/resend-verification", "POST")
        ),
        "user.api_keys": (
            password_auth
            and bool(settings.api_key_secret)
            and _route_is_registered(request, "/user/api-keys", "POST")
            and _route_is_registered(request, "/user/api-keys", "GET")
        ),
        "user.usage": (password_auth and _route_is_registered(request, "/user/usage", "GET")),
        "playground.chat": (
            password_auth
            and _playground_is_ready(services)
            and _route_is_registered(request, "/control/v1/playground/models", "GET")
            and _route_is_registered(request, "/control/v1/playground/chat", "POST")
        ),
        "rag.chat": (
            password_auth
            and _rag_is_ready()
            and _route_is_registered(request, "/v1/rag/chat", "POST")
        ),
        "admin.core": admin_core,
        "admin.routing.routewise": (
            admin_core
            and _routewise_is_ready(services)
            and _route_is_registered(request, "/admin/routewise/settings", "GET")
        ),
    }


async def distribution_capability_mismatches(
    request: Request,
    *,
    capabilities: dict[str, bool] | None = None,
) -> dict[str, tuple[bool, bool]]:
    """Compare distribution policy with effective truth and log state transitions."""
    effective = capabilities if capabilities is not None else await effective_capabilities(request)
    expected = get_distribution_capability_expectations()
    mismatches: dict[str, tuple[bool, bool]] = {}
    for capability_id, expected_value in expected.items():
        effective_value = effective.get(capability_id, False)
        state = (expected_value, effective_value)
        previous = _expectation_states.get(capability_id)
        if expected_value != effective_value:
            mismatches[capability_id] = state
            if previous != state:
                logger.warning(
                    "distribution_capability_mismatch capability=%s expected=%d effective=%d",
                    capability_id,
                    expected_value,
                    effective_value,
                )
        elif previous is not None and previous[0] != previous[1]:
            logger.info(
                "distribution_capability_mismatch_resolved capability=%s expected=%d effective=%d",
                capability_id,
                expected_value,
                effective_value,
            )
        _expectation_states[capability_id] = state
    return mismatches


@router.get(
    "/capabilities",
    response_model=CapabilitiesResponse,
    operation_id="getCapabilities",
)
async def get_capabilities(request: Request) -> CapabilitiesResponse:
    """Return effective public capabilities without exposing readiness reasons."""
    values = await effective_capabilities(request)
    await distribution_capability_mismatches(request, capabilities=values)
    return CapabilitiesResponse(capabilities=values)
