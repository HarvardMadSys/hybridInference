"""Contract tests for the effective public capability document."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from serving.servers.app import create_app
from serving.servers.routers import capabilities


class _RuntimeSettings:
    def __init__(self, **values: bool) -> None:
        self._values = values

    async def get_bool(self, key: str) -> bool:
        return self._values[key]


class _OperationalStore:
    async def health_check(self) -> bool:
        return True


class _RouterRegistry:
    def configured_model_ids(self) -> list[str]:
        return ["model-a"]

    def registered_models(self) -> list[str]:
        return []

    def get_router_name(self, model_id: str) -> str:
        return "routewise"


def _add_contract_routes(app: FastAPI) -> None:
    async def endpoint() -> None:
        return None

    for path, method in (
        ("/auth/login", "POST"),
        ("/auth/signup", "POST"),
        ("/auth/verify-email", "GET"),
        ("/auth/resend-verification", "POST"),
        ("/user/api-keys", "GET"),
        ("/user/api-keys", "POST"),
        ("/user/usage", "GET"),
        ("/control/v1/playground/models", "GET"),
        ("/control/v1/playground/chat", "POST"),
        ("/v1/rag/chat", "POST"),
        ("/admin/settings", "GET"),
        ("/admin/routewise/settings", "GET"),
    ):
        app.add_api_route(path, endpoint, methods=[method])


def _make_client(*, runtime_values: dict[str, bool] | None = None) -> TestClient:
    app = FastAPI()
    app.include_router(capabilities.router)
    _add_contract_routes(app)
    effective_runtime_values = {
        "user_auth_enabled": True,
        "signup_enabled": True,
        "signup_require_email_verification": True,
        **(runtime_values or {}),
    }
    app.state.services = SimpleNamespace(
        operational_store=_OperationalStore(),
        runtime_settings=_RuntimeSettings(**effective_runtime_values),
        router=SimpleNamespace(
            routes={
                "model-a": SimpleNamespace(
                    published=True,
                    adapters=[(object(), 1.0)],
                )
            }
        ),
        model_router_registry=_RouterRegistry(),
    )
    return TestClient(app)


def test_capabilities_returns_versioned_namespaced_bool_map(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "index.json"
    index_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        capabilities,
        "get_settings",
        lambda: SimpleNamespace(
            user_auth_enabled=True,
            signup_enabled=True,
            signup_require_email_verification=True,
            smtp_user="smtp-user",
            smtp_password="smtp-password",
            api_key_secret="api-key-secret",
        ),
    )
    monkeypatch.setattr(
        capabilities,
        "load_rag_settings",
        lambda: SimpleNamespace(api_key="hyi-test", index_path=index_path),
    )

    response = _make_client().get("/capabilities")

    assert response.status_code == 200
    assert response.json() == {
        "schema_version": 1,
        "control_api_version": "1.0",
        "capabilities": {
            "auth.password": True,
            "auth.public_signup": True,
            "auth.email_verification": True,
            "user.api_keys": True,
            "user.usage": True,
            "playground.chat": True,
            "rag.chat": True,
            "admin.core": True,
            "admin.routing.routewise": True,
        },
    }
    assert all(isinstance(value, bool) for value in response.json()["capabilities"].values())


def test_capabilities_uses_effective_runtime_settings(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        capabilities,
        "get_settings",
        lambda: SimpleNamespace(
            user_auth_enabled=True,
            signup_enabled=True,
            signup_require_email_verification=True,
            smtp_user="smtp-user",
            smtp_password="smtp-password",
            api_key_secret="api-key-secret",
        ),
    )
    monkeypatch.setattr(
        capabilities,
        "load_rag_settings",
        lambda: SimpleNamespace(api_key="", index_path=Path("/missing")),
    )

    response = _make_client(
        runtime_values={
            "signup_enabled": False,
            "signup_require_email_verification": False,
        }
    ).get("/capabilities")

    assert response.status_code == 200
    result = response.json()["capabilities"]
    assert result["auth.password"] is True
    assert result["auth.public_signup"] is False
    assert result["auth.email_verification"] is False
    assert result["rag.chat"] is False
    assert "reason" not in response.text


def test_capability_is_false_when_stable_route_is_not_registered(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        capabilities,
        "get_settings",
        lambda: SimpleNamespace(
            user_auth_enabled=True,
            signup_enabled=True,
            signup_require_email_verification=False,
            smtp_user="",
            smtp_password="",
            api_key_secret="api-key-secret",
        ),
    )
    app = FastAPI()
    app.include_router(capabilities.router)
    app.state.services = SimpleNamespace(
        operational_store=_OperationalStore(),
        runtime_settings=_RuntimeSettings(
            user_auth_enabled=True,
            signup_enabled=True,
            signup_require_email_verification=False,
        ),
        router=SimpleNamespace(routes={}),
        model_router_registry=None,
    )

    result = TestClient(app).get("/capabilities").json()["capabilities"]

    assert result["auth.password"] is False
    assert result["playground.chat"] is False
    assert result["admin.core"] is False


def test_route_registration_uses_the_real_nested_application_topology() -> None:
    request = SimpleNamespace(app=create_app())

    for path, method in (
        ("/auth/login", "POST"),
        ("/control/v1/playground/models", "GET"),
        ("/admin/settings", "GET"),
        ("/v1/rag/chat", "POST"),
    ):
        assert capabilities._route_is_registered(request, path, method)
    assert not capabilities._route_is_registered(request, "/not-registered", "GET")


def test_capability_expectation_mismatch_does_not_replace_effective_truth(
    monkeypatch: Any,
    caplog: Any,
) -> None:
    monkeypatch.setattr(
        capabilities,
        "get_settings",
        lambda: SimpleNamespace(
            user_auth_enabled=True,
            signup_enabled=True,
            signup_require_email_verification=False,
            smtp_user="",
            smtp_password="",
            api_key_secret="api-key-secret",
        ),
    )
    monkeypatch.setattr(
        capabilities,
        "get_distribution_capability_expectations",
        lambda: {"rag.chat": True},
    )
    monkeypatch.setattr(
        capabilities,
        "load_rag_settings",
        lambda: SimpleNamespace(api_key="", index_path=Path("/missing")),
    )
    capabilities._expectation_states.clear()

    response = _make_client().get("/capabilities")

    assert response.status_code == 200
    assert response.json()["capabilities"]["rag.chat"] is False
    assert (
        "distribution_capability_mismatch capability=rag.chat expected=1 effective=0" in caplog.text
    )
