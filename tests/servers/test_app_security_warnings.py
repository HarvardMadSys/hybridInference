"""Security wording contracts emitted during application startup."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from fastapi import FastAPI

from serving.servers import app as app_module


@pytest.mark.asyncio
async def test_empty_admin_token_warning_describes_only_the_legacy_path(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    services = object()

    async def initialize():
        return services

    async def shutdown(received_services):
        assert received_services is services

    monkeypatch.setattr(app_module.bootstrap, "initialize", initialize)
    monkeypatch.setattr(app_module.bootstrap, "shutdown", shutdown)
    monkeypatch.setattr(
        app_module,
        "settings",
        SimpleNamespace(
            jwt_secret_key="configured-jwt-secret",
            api_key_secret="configured-api-key-secret",
            admin_token="",
        ),
    )
    caplog.set_level(logging.WARNING, logger=app_module.__name__)

    app = FastAPI()
    async with app_module.lifespan(app):
        pass

    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.name == app_module.__name__ and record.levelno == logging.WARNING
    ]
    assert warnings == ["admin_token is empty — legacy admin-token access is disabled"]
