"""The refusal of a password sign-in that also says where the organization signs in.

It is the plain refusal of a wrong name or password with one more field, in
the shape the application gives every ``HTTPException``. The real application
wraps that in an ``error`` envelope and a test application does not, so both
are pinned here.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException as StarletteHTTPException

from serving.exceptions import WRONG_CREDENTIALS_DETAIL, SsoSignInRequiredError
from serving.servers.middleware.error import install_error_handlers
from serving.servers.middleware.exception_handler import install_exception_handlers
from serving.servers.routers import anthropic_messages


def _client(*, as_the_real_application: bool) -> TestClient:
    app = FastAPI()
    if as_the_real_application:
        # The order of ``serving.servers.app.create_app``: the last registration wins.
        install_error_handlers(app)
    install_exception_handlers(app)
    if as_the_real_application:
        for raised in (StarletteHTTPException, HTTPException):
            app.add_exception_handler(
                raised, anthropic_messages.anthropic_aware_http_exception_handler
            )

    @app.post("/plain")
    async def plain():
        raise HTTPException(status_code=401, detail=WRONG_CREDENTIALS_DETAIL)

    @app.post("/told")
    async def told():
        raise SsoSignInRequiredError("Example University")

    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("as_the_real_application", [True, False], ids=["real", "bare"])
def test_the_two_refusals_differ_in_one_field_alone(as_the_real_application):
    client = _client(as_the_real_application=as_the_real_application)

    plain = client.post("/plain")
    told = client.post("/told")

    assert plain.status_code == told.status_code == 401
    body = told.json()
    assert body.pop("sso_sign_in") == {"provider_name": "Example University"}
    assert body == plain.json()
    assert "sso_sign_in" not in plain.json()


def test_the_real_application_keeps_its_error_envelope():
    told = _client(as_the_real_application=True).post("/told").json()

    assert told["error"]["message"] == WRONG_CREDENTIALS_DETAIL
    assert told["error"]["code"] == 401
    assert "detail" not in told


def test_what_the_plain_refusal_carries_beside_its_body_stays():
    """An application that answers an ``HTTPException`` its own way is followed in full."""
    app = FastAPI()
    install_exception_handlers(app)

    async def its_own_way(request, exc):
        return JSONResponse(
            status_code=exc.status_code,
            content={"problem": exc.detail},
            headers={"WWW-Authenticate": "Bearer"},
        )

    app.add_exception_handler(StarletteHTTPException, its_own_way)

    @app.post("/told")
    async def told():
        raise SsoSignInRequiredError("Example University")

    answer = TestClient(app, raise_server_exceptions=False).post("/told")

    assert answer.status_code == 401
    assert answer.headers["www-authenticate"] == "Bearer"
    assert answer.json() == {
        "problem": WRONG_CREDENTIALS_DETAIL,
        "sso_sign_in": {"provider_name": "Example University"},
    }
    assert int(answer.headers["content-length"]) == len(answer.content)
