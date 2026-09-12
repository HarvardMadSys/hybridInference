"""Response redaction across all five error envelopes.

Option A: internal detail is removed from the **response**; the HTTP status is
whatever it was before. A client that backs off on 429 and retries on 503 sees
exactly the same status sequence as it did yesterday -- that is the invariant
the first test class pins, because it is the one a redaction patch is most
likely to break by collapsing everything to 500.

This gateway has five error envelopes, not one, and they do not share a builder:

  0. ``middleware/error.py::http_exc_handler`` -- shadowed, never invoked. Pinned
     here so "we fixed the handler" can never again mean "we fixed the dead one".
  1. the Anthropic envelope built inline in
     ``anthropic_messages.anthropic_aware_http_exception_handler``.
  2. the OpenRouter envelope the same handler builds for every other path.
  3. the dict-detail short-circuit in that handler, which returns a caller-built
     body nearly verbatim.
  4. the domain-exception envelope in ``middleware/exception_handler.py``.
  5. FastAPI's ``RequestValidationError`` 422, now overridden.
"""

from __future__ import annotations

import ast
import logging
import pathlib

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from serving import quota
from serving.exceptions import (
    AccountSuspendedError,
    QuotaExceededError,
    UserAlreadyExistsError,
    UserNotFoundError,
    scrub_error_for_user,
)
from serving.servers.middleware.error import install_error_handlers
from serving.servers.middleware.exception_handler import install_exception_handlers
from serving.servers.middleware.request_id import RequestIdMiddleware
from serving.servers.routers import anthropic_messages
from serving.utils.identity_keys import ENV_PRIVATE_KEY

# A string with one of each thing the policy calls internal: a file path, a key
# path, a parse error, a provider name, and an internal identifier. If any
# fragment of it reaches a response body, the redaction failed.
SECRET_DETAIL = (
    "IDENTITY_JWT_PRIVATE_KEY at /etc/hybridinference/keys/identity.pem: "
    "PEM parse failed (anthropic upstream pool key kid=abc123)"
)
SECRET_FRAGMENTS = ("/etc/", "identity.pem", "PEM parse", "kid=abc123", "IDENTITY_JWT_PRIVATE_KEY")

# Developer-authored request-contract messages. These are the whole answer for a
# client that sent a bad request, and redaction must leave them alone.
CONTRACT_MESSAGE = "Missing required field: model"


def assert_no_secret(payload: str) -> None:
    """Assert no fragment of the internal detail survived into a response."""
    for fragment in SECRET_FRAGMENTS:
        assert fragment not in payload, f"leaked {fragment!r} in: {payload}"


class Body(BaseModel):
    """Body model for the RequestValidationError probe."""

    model: str
    max_tokens: int


def _wire_like_create_app(app: FastAPI) -> None:
    """Install exception handlers exactly as ``serving.servers.app.create_app`` does.

    Order matters and is the point of envelope 0: ``install_error_handlers``
    registers an HTTPException handler, and the two registrations after it
    replace that entry (Starlette's ``add_exception_handler`` is a dict
    assignment). A test app that installs only the first one exercises a handler
    production never reaches.
    """
    install_error_handlers(app)
    install_exception_handlers(app)
    app.add_exception_handler(
        StarletteHTTPException, anthropic_messages.anthropic_aware_http_exception_handler
    )
    app.add_exception_handler(
        HTTPException, anthropic_messages.anthropic_aware_http_exception_handler
    )
    app.add_exception_handler(
        RequestValidationError, anthropic_messages.anthropic_aware_validation_exception_handler
    )


@pytest.fixture
def client() -> TestClient:
    """A probe app wired like production, with one route per leak shape.

    Routes are mounted twice, under ``/v1/messages/...`` and under ``/probe/...``,
    because the handler picks its envelope by path prefix: the first set lands in
    envelope 1 (Anthropic) and the second in envelope 2 (OpenRouter).
    """
    app = FastAPI()

    def add(path: str, endpoint, **kwargs) -> None:
        app.add_api_route(f"/v1/messages/probe{path}", endpoint, **kwargs)
        app.add_api_route(f"/probe{path}", endpoint, **kwargs)

    def contract() -> None:
        raise HTTPException(400, CONTRACT_MESSAGE)

    def object_detail() -> None:
        # A non-string detail: an exception object dropped into the message
        # slot. str() of it is internal by construction.
        raise HTTPException(503, detail=RuntimeError(SECRET_DETAIL))  # type: ignore[arg-type]

    def redacted_at_raise_site() -> None:
        # What a production raise site does with exception-derived text: redact
        # where it enters the response, keeping the status and the request id.
        raise HTTPException(429, scrub_error_for_user(RuntimeError(SECRET_DETAIL), "req-abc", 429))

    def dict_detail() -> None:
        raise HTTPException(
            500,
            detail={"error": {"type": "identity_key_misconfigured", "message": SECRET_DETAIL}},
        )

    def dict_detail_object_inner() -> None:
        raise HTTPException(502, detail={"error": {"internal": SECRET_DETAIL}})

    def quota_dict_detail() -> None:
        body, headers = quota.exceeded_payload(quota_usd=10.0, spent_usd=10.0)
        raise HTTPException(429, detail=body, headers=headers)

    def status_probe(code: int) -> None:
        raise HTTPException(code, CONTRACT_MESSAGE)

    def validate(body: Body) -> dict:
        return {"ok": True}

    def domain_user_not_found() -> None:
        raise UserNotFoundError("usr_01H8INTERNALID")

    def domain_user_exists() -> None:
        raise UserAlreadyExistsError("victim@example.test")

    def domain_quota() -> None:
        raise QuotaExceededError(quota=10.0, spent=10.5)

    def domain_suspended() -> None:
        raise AccountSuspendedError("suspended", "Contact billing to restore access.")

    add("/contract", contract)
    add("/object", object_detail)
    add("/redacted", redacted_at_raise_site)
    add("/dict", dict_detail)
    add("/dict-object-inner", dict_detail_object_inner)
    add("/quota", quota_dict_detail)
    add("/status/{code}", status_probe)
    add("/validate", validate, methods=["POST"])
    add("/domain/user-not-found", domain_user_not_found)
    add("/domain/user-exists", domain_user_exists)
    add("/domain/quota", domain_quota)
    add("/domain/suspended", domain_suspended)

    app.add_middleware(RequestIdMiddleware)
    _wire_like_create_app(app)
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# (a) Status codes are unchanged
# ---------------------------------------------------------------------------


class TestStatusCodesSurvive:
    """429 stays 429, 503 stays 503, 400 stays 400 -- nothing collapses to 500.

    Clients here back off on the status. One of them already ignores
    Retry-After, so a 429 rewritten as a 500 would turn a paced client into an
    immediate-retry client against a gateway that is already refusing it.
    """

    @pytest.mark.parametrize("code", [400, 401, 403, 404, 409, 422, 429, 500, 502, 503, 504])
    @pytest.mark.parametrize("prefix", ["/v1/messages/probe", "/probe"])
    def test_status_passes_through_both_envelopes(
        self, client: TestClient, prefix: str, code: int
    ) -> None:
        assert client.get(f"{prefix}/status/{code}").status_code == code

    @pytest.mark.parametrize("prefix", ["/v1/messages/probe", "/probe"])
    def test_redaction_does_not_move_a_status(self, client: TestClient, prefix: str) -> None:
        assert client.get(f"{prefix}/object").status_code == 503
        assert client.get(f"{prefix}/redacted").status_code == 429
        assert client.get(f"{prefix}/dict").status_code == 500

    def test_validation_error_keeps_422(self, client: TestClient) -> None:
        # FastAPI's default answers 422; the override must not "improve" it to
        # 400, which would change a documented contract for every client.
        assert client.post("/probe/validate", json={"model": 1.5}).status_code == 422
        assert client.post("/v1/messages/probe/validate", json={"model": 1.5}).status_code == 422

    def test_domain_envelope_statuses(self, client: TestClient) -> None:
        assert client.get("/probe/domain/user-not-found").status_code == 404
        assert client.get("/probe/domain/user-exists").status_code == 409
        assert client.get("/probe/domain/quota").status_code == 429
        assert client.get("/probe/domain/suspended").status_code == 403


# ---------------------------------------------------------------------------
# (0) The obvious patch point is dead code
# ---------------------------------------------------------------------------


def test_the_live_http_handler_is_the_anthropic_one() -> None:
    """``install_error_handlers``' own handler never runs on the real app.

    ``create_app`` re-registers the Anthropic-aware handler for both the
    Starlette and FastAPI exception classes after calling it, and Starlette's
    registry is a plain dict. If this assertion ever flips, the redaction in the
    Anthropic-aware handler stops being the one that matters and every other
    test in this file is testing a path production does not take.
    """
    import os

    os.environ.setdefault("DB_ENABLED", "false")
    from serving.servers.app import create_app

    app = create_app()
    for exc_class in (StarletteHTTPException, HTTPException):
        assert (
            app.exception_handlers[exc_class]
            is anthropic_messages.anthropic_aware_http_exception_handler
        )
    assert (
        app.exception_handlers[RequestValidationError]
        is anthropic_messages.anthropic_aware_validation_exception_handler
    )


# ---------------------------------------------------------------------------
# (b) Exception-derived text does not reach the body, on each envelope
# ---------------------------------------------------------------------------


class TestEnvelope1AnthropicInline:
    """The ``{"type": "error", "error": {...}}`` body built inline in the handler."""

    def test_object_detail_is_replaced(self, client: TestClient) -> None:
        resp = client.get("/v1/messages/probe/object")
        assert_no_secret(resp.text)
        body = resp.json()
        assert body["type"] == "error"
        assert body["error"]["type"] == "overloaded_error"
        assert body["error"]["message"] == "Internal server error"

    def test_exception_text_redacted_at_the_raise_site(self, client: TestClient) -> None:
        resp = client.get("/v1/messages/probe/redacted")
        assert_no_secret(resp.text)
        assert resp.json()["error"]["type"] == "rate_limit_error"


class TestEnvelope2OpenRouterShape:
    """The OpenRouter body the same handler builds for non-Anthropic paths."""

    def test_object_detail_is_replaced(self, client: TestClient) -> None:
        resp = client.get("/probe/object")
        assert_no_secret(resp.text)
        assert resp.json()["error"]["message"] == "Internal server error"
        assert resp.json()["error"]["code"] == 503

    def test_exception_text_redacted_at_the_raise_site(self, client: TestClient) -> None:
        resp = client.get("/probe/redacted")
        assert_no_secret(resp.text)
        assert resp.json()["error"]["code"] == 429


class TestEnvelope3DictShortCircuit:
    """The short-circuit that returns a caller-built dict body nearly verbatim.

    The real leak on this path was ``routers/identity.py``, which put ``str(exc)``
    -- an operator's key-configuration fault -- into a dict body from an
    endpoint that needs no credential at all. It is fixed at that raise site, so
    the test drives the real endpoint rather than a stand-in.
    """

    @pytest.fixture
    def identity_client(self, monkeypatch: pytest.MonkeyPatch) -> TestClient:
        from serving.servers.routers import identity
        from serving.utils import identity_keys

        monkeypatch.setattr(identity_keys, "_PRIVATE_CACHE", {}, raising=False)
        monkeypatch.setenv(ENV_PRIVATE_KEY, "definitely not a key")
        app = FastAPI()
        app.include_router(identity.router)
        app.add_middleware(RequestIdMiddleware)
        _wire_like_create_app(app)
        return TestClient(app, raise_server_exceptions=False)

    def test_jwks_misconfiguration_is_not_published(self, identity_client: TestClient) -> None:
        resp = identity_client.get("/v1/identity/jwks")

        assert resp.status_code == 500
        body = resp.json()
        # The unset/broken distinction is the contract and survives ...
        assert body["error"]["type"] == "identity_key_misconfigured"
        # ... the operator's configuration detail does not.
        assert body["error"]["message"] == (
            "Cross-service identity is misconfigured on this deployment."
        )
        assert "PEM" not in resp.text
        assert ENV_PRIVATE_KEY not in resp.text

    def test_jwks_logs_the_detail_it_stopped_publishing(
        self, identity_client: TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.ERROR, logger="serving.servers.routers.identity"):
            identity_client.get("/v1/identity/jwks")

        records = [r for r in caplog.records if r.msg == "identity_configuration_error"]
        assert records, "the fault must still reach the log"
        assert "PEM" in records[0].detail
        assert records[0].error_type == "IdentityKeyMisconfigured"

    def test_quota_429_keeps_its_body_and_headers_off_the_anthropic_surface(
        self, client: TestClient
    ) -> None:
        """The high-volume 429, unchanged. This is the body ~122k requests/day see."""
        resp = client.get("/probe/quota")

        assert resp.status_code == 429
        body = resp.json()
        assert body["error"] == "Daily cost quota exceeded"
        assert body["quota_usd"] == 10.0
        assert body["spent_usd"] == 10.0
        assert body["remaining_usd"] == 0
        assert int(resp.headers["retry-after"]) > 0
        assert resp.headers["x-ratelimit-limit-cost"] == "10.0"

    def test_quota_429_still_re_wraps_on_the_anthropic_surface(self, client: TestClient) -> None:
        """A string inner error keeps being surfaced, redaction or not.

        Claude Code parses error.type/error.message and shows an opaque failure
        for anything else, so this body is re-wrapped rather than forwarded --
        pre-existing behaviour that the ``str(inner)`` removal must not disturb.
        The 429 and its Retry-After, which is what the client actually paces on,
        pass straight through.
        """
        resp = client.get("/v1/messages/probe/quota")

        assert resp.status_code == 429
        assert resp.json()["error"]["message"] == "Daily cost quota exceeded"
        assert resp.json()["error"]["type"] == "rate_limit_error"
        assert int(resp.headers["retry-after"]) > 0

    def test_object_inside_a_dict_body_is_not_stringified(self, client: TestClient) -> None:
        # An inner error with no message/code at all used to be rendered with
        # str(inner), dumping the whole dict into the message a client shows.
        resp = client.get("/v1/messages/probe/dict-object-inner")
        assert resp.status_code == 502
        assert_no_secret(resp.text)


class TestEnvelope4DomainExceptions:
    """The ``{error_code, message, timestamp}`` bodies that bypass everything above."""

    def test_internal_id_is_not_echoed(self, client: TestClient) -> None:
        resp = client.get("/probe/domain/user-not-found")
        assert resp.status_code == 404
        assert resp.json()["error_code"] == "USER_NOT_FOUND"
        assert "usr_01H8INTERNALID" not in resp.text

    def test_signup_409_is_not_a_membership_oracle(self, client: TestClient) -> None:
        resp = client.get("/probe/domain/user-exists")
        assert resp.status_code == 409
        assert resp.json()["error_code"] == "USER_ALREADY_EXISTS"
        assert "victim@example.test" not in resp.text
        assert "email" not in resp.json()

    def test_quota_429_keeps_the_callers_own_numbers(self, client: TestClient) -> None:
        # The fourth quota-429 shape. Nothing here is internal: it is this
        # user's spend against this user's cap, which is what they need in order
        # to know how long to wait.
        resp = client.get("/probe/domain/quota")
        assert resp.status_code == 429
        body = resp.json()
        assert body["quota"] == 10.0
        assert body["spent"] == 10.5
        assert "10.50" in body["message"]

    def test_suspension_message_survives(self, client: TestClient) -> None:
        # Admin-authored, written for this user. Not internal detail.
        resp = client.get("/probe/domain/suspended")
        assert resp.json()["suspension_message"] == "Contact billing to restore access."
        assert resp.json()["status"] == "suspended"

    def test_detail_still_reaches_the_log(
        self, client: TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        logger_name = "serving.servers.middleware.exception_handler"
        with caplog.at_level(logging.WARNING, logger=logger_name):
            client.get("/probe/domain/user-not-found")

        records = [r for r in caplog.records if r.msg == "domain_error"]
        assert records, "the domain exception must still be logged"
        assert "usr_01H8INTERNALID" in records[0].detail


class TestEnvelope5RequestValidation:
    """FastAPI's default 422 echoed the caller's ``input`` and our ``loc`` paths."""

    @pytest.mark.parametrize("prefix", ["/v1/messages/probe", "/probe"])
    def test_input_and_loc_are_gone(self, client: TestClient, prefix: str) -> None:
        resp = client.post(
            f"{prefix}/validate",
            json={"model": {"nested": "sk-secret-value"}, "max_tokens": "not-an-int"},
        )

        assert resp.status_code == 422
        assert "sk-secret-value" not in resp.text
        assert "not-an-int" not in resp.text
        assert "loc" not in resp.text
        assert '"input"' not in resp.text
        assert "max_tokens" not in resp.text

    def test_anthropic_surface_keeps_its_envelope_shape(self, client: TestClient) -> None:
        body = client.post("/v1/messages/probe/validate", json={}).json()
        assert body["type"] == "error"
        assert body["error"]["type"] == "invalid_request_error"
        assert body["error"]["message"] == "Invalid request"

    def test_other_surfaces_keep_theirs(self, client: TestClient) -> None:
        body = client.post("/probe/validate", json={}).json()
        assert body["error"]["code"] == 422
        assert body["error"]["type"] == "validation_error"

    def test_detail_still_reaches_the_log(
        self, client: TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        logger_name = "serving.servers.routers.anthropic_messages"
        with caplog.at_level(logging.WARNING, logger=logger_name):
            client.post("/probe/validate", json={"model": {"a": "sk-secret-value"}})

        records = [r for r in caplog.records if r.msg == "request_validation_error"]
        assert records, "the validation detail must still be logged"
        assert "sk-secret-value" in records[0].detail


# ---------------------------------------------------------------------------
# (c) The messages that must survive
# ---------------------------------------------------------------------------


class TestContractMessagesSurvive:
    """Redaction that eats the request-contract messages is a worse bug than the leak.

    ``_anthropic_error`` is called with "Missing required field: model" and with
    a scrubbed upstream message from the same function, which is exactly why the
    redaction is applied where exception text *enters* a response rather than as
    a blanket wrap on the envelope builder.
    """

    @pytest.mark.parametrize("prefix", ["/v1/messages/probe", "/probe"])
    def test_static_contract_message_is_verbatim(self, client: TestClient, prefix: str) -> None:
        resp = client.get(f"{prefix}/contract")
        assert resp.status_code == 400
        assert resp.json()["error"]["message"] == CONTRACT_MESSAGE

    @pytest.mark.parametrize(
        "path",
        [
            "/v1/messages/probe/contract",
            "/probe/contract",
            "/v1/messages/probe/object",
            "/probe/object",
            "/probe/domain/quota",
            "/probe/validate",
        ],
    )
    def test_request_id_survives_every_envelope(self, client: TestClient, path: str) -> None:
        """The correlation id is how support maps a complaint to a log line.

        It rides on the response header, so it survives a body that was reduced
        to a generic sentence -- which is the whole reason the generic sentence
        is an acceptable answer.
        """
        method = client.post if path.endswith("/validate") else client.get
        resp = method(path, headers={"X-Request-ID": "req-support-1234"})
        assert resp.headers["x-request-id"] == "req-support-1234"

    def test_request_id_in_a_redacted_body_is_kept(self, client: TestClient) -> None:
        resp = client.get("/probe/redacted")
        assert "req-abc" in resp.json()["error"]["message"]


# ---------------------------------------------------------------------------
# The invariant that keeps this fixed
# ---------------------------------------------------------------------------


_EXCEPTION_NAMES = {"exc", "e", "err", "ex", "exception"}
#: Bare names that build a client-facing error, plus ``internal_auth.error``,
#: which is the same builder reached through its module. Matching on the bare
#: attribute ``.error`` instead would sweep in every ``logger.error(f"... {exc}")``
#: in these files -- server-side logging, which this change deliberately keeps.
_RESPONSE_BUILDERS = {"HTTPException", "_error", "_anthropic_error"}
_QUALIFIED_BUILDERS = {("internal_auth", "error"), ("quota", "exceeded_payload")}

#: Public surfaces only. ``routers/admin/`` is deliberately not in this set: it
#: is an operator surface behind admin authentication, where the exception text
#: in a 400/503 is the diagnostic the operator called the endpoint to get. If
#: that judgement ever changes, add the directory here and the failures will
#: enumerate the work.
_GUARDED = (
    "serving/servers/auth.py",
    "serving/servers/concurrency.py",
    "serving/servers/deps.py",
    "serving/servers/middleware/error.py",
    "serving/servers/middleware/exception_handler.py",
    "serving/servers/routers/anthropic_messages.py",
    "serving/servers/routers/auth_routes.py",
    "serving/servers/routers/completions.py",
    "serving/servers/routers/embeddings.py",
    "serving/servers/routers/identity.py",
    "serving/servers/routers/internal_auth.py",
    "serving/servers/routers/qdrant_proxy.py",
    "serving/servers/routers/rag.py",
    "serving/servers/routers/responses.py",
    "serving/servers/routers/user_routes.py",
)


def _exception_derived(node: ast.AST) -> bool:
    """True if ``node`` renders an exception into text: ``str(exc)`` or ``f"{exc}"``."""
    for child in ast.walk(node):
        if (
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Name)
            and child.func.id == "str"
            and child.args
            and isinstance(child.args[0], ast.Name)
            and child.args[0].id in _EXCEPTION_NAMES
        ):
            return True
        if isinstance(child, ast.FormattedValue):
            inner = child.value
            if isinstance(inner, ast.Attribute):
                inner = inner.value
            if isinstance(inner, ast.Name) and inner.id in _EXCEPTION_NAMES:
                return True
    return False


def test_no_public_response_builder_is_handed_exception_text() -> None:
    """The provenance rule, enforced rather than documented.

    An envelope cannot tell developer-authored contract text from exception text
    -- both arrive as ``str`` -- so the rule has to hold where the text is
    produced: no call that builds a client-facing error may be handed
    ``str(exc)`` or an f-string interpolating one. Redact at the raise site
    (``scrub_error_for_user``) or write a static message.
    """
    backend = pathlib.Path(__file__).resolve().parents[3] / "apps" / "backend"
    offenders: list[str] = []

    for relative in _GUARDED:
        path = backend / relative
        assert path.exists(), f"guard list is stale: {relative}"
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name):
                if node.func.id not in _RESPONSE_BUILDERS:
                    continue
            elif isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
                if (node.func.value.id, node.func.attr) not in _QUALIFIED_BUILDERS:
                    continue
            else:
                continue
            if any(_exception_derived(arg) for arg in node.args) or any(
                _exception_derived(kw.value) for kw in node.keywords
            ):
                offenders.append(f"{relative}:{node.lineno}")

    assert not offenders, (
        "exception-derived text is being handed to a client-facing error builder at: "
        + ", ".join(offenders)
    )
