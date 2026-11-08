"""Read a route's JSON body only once its caller is authenticated.

FastAPI reads and decodes a route's declared body model before it runs any
dependency, authentication included: an anonymous request makes the server
decode whatever it sent before refusing it. Most routes that declare one take a
small body, and ``RequestBodyLimitMiddleware`` holds them to a small limit. A
route that legitimately takes a large body declares none, and reads it through
:func:`json_body_after` instead, a dependency that depends on the route's
authentication -- so nothing is read until the caller is known -- and answers
an invalid body exactly as FastAPI would, with the 422 that names the fields.
"""

from __future__ import annotations

import email.message
import json
from typing import TYPE_CHECKING, Any, TypeVar

from fastapi import Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ValidationError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

ModelT = TypeVar("ModelT", bound=BaseModel)


def _is_json(content_type: str | None) -> bool:
    """Whether FastAPI would decode a body of this type as JSON."""
    if not content_type:
        return False
    message = email.message.Message()
    message["content-type"] = content_type
    if message.get_content_maintype() != "application":
        return False
    subtype = message.get_content_subtype()
    return subtype == "json" or subtype.endswith("+json")


async def validated_json_body(request: Request, model: type[ModelT]) -> ModelT:
    """Decode and validate *request*'s body as a declared body model of *model*.

    The answers are FastAPI's own: 400 when the body cannot be read, and a
    ``RequestValidationError`` (422) for invalid JSON, a missing body or a
    schema mismatch, with each error's ``loc`` under ``body``. A JSON body is
    decoded through ``Request.json``, which keeps the result, so a dependency
    that already read it (the concurrency gate reads the model) is not
    decoded again.
    """
    try:
        raw = await request.body()
    except Exception:
        raise HTTPException(status_code=400, detail="There was an error parsing the body") from None
    payload: Any = None
    if raw:
        if _is_json(request.headers.get("content-type")):
            try:
                payload = await request.json()
            except json.JSONDecodeError as exc:
                raise RequestValidationError(
                    [
                        {
                            "type": "json_invalid",
                            "loc": ("body", exc.pos),
                            "msg": "JSON decode error",
                            "input": {},
                            "ctx": {"error": exc.msg},
                        }
                    ]
                ) from None
            except Exception:
                raise HTTPException(
                    status_code=400, detail="There was an error parsing the body"
                ) from None
        else:
            payload = raw
    if payload is None:
        raise RequestValidationError(
            [{"type": "missing", "loc": ("body",), "msg": "Field required", "input": None}]
        )
    try:
        # As FastAPI validates a body field (``from_attributes`` changes only
        # the error a non-JSON body gets).
        return model.model_validate(payload, from_attributes=True)
    except ValidationError as exc:
        raise RequestValidationError(
            [{**error, "loc": ("body", *error["loc"])} for error in exc.errors(include_url=False)]
        ) from None


def json_body_after(
    dependency: Callable[..., Any], model: type[ModelT]
) -> Callable[..., Awaitable[ModelT]]:
    """A dependency that reads *model* from the body once *dependency* has run.

    *dependency* is the route's authentication. FastAPI runs a dependency's
    own dependencies first and caches each per request, so the route may name
    *dependency* again for its result without running it twice.
    """

    async def read_body(request: Request, _caller: Any = Depends(dependency)) -> ModelT:
        return await validated_json_body(request, model)

    return read_body


def json_body_schema(model: type[BaseModel]) -> dict[str, Any]:
    """``openapi_extra`` that still describes *model* as the route's JSON body."""
    return {
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": model.model_json_schema()}},
        }
    }
