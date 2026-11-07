"""Reverse proxy for shared Qdrant vector database.

Forwards allowed requests under /v1/qdrant/ to the internal Qdrant instance,
injecting the server-side API key. Users authenticate with their
gateway API key and never see the Qdrant credentials.

Security measures:
- Anonymous requests are rejected even when USER_AUTH_ENABLED=0.
- Endpoint allowlist: only collections and points operations are proxied.
- Tenant isolation: collection names are prefixed with a user-scoped namespace,
  in the path and in the body fields that name another collection
  (``lookup_from`` on recommend, ``init_from`` on create); a body whose
  reference cannot be scoped is refused.
- One path: the path is normalized once, and that string is what the allowlist
  checks, the scoping rewrites and Qdrant receives; a path the two could read
  differently (a leading slash, an empty or dot segment, an encoded slash) is
  refused.
"""

from __future__ import annotations

import hashlib
import json
import re
import time

import aiohttp
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from serving.config.settings import get_settings
from serving.http import AsyncHTTPClient
from serving.servers.auth import verify_api_key
from serving.utils import context as req_ctx
from serving.utils.logging import get_logger

logger = get_logger(__name__)
router = APIRouter()

#: Provider label for upstream Qdrant failures relayed to the client. This proxy
#: has no ModelConfig, so the label is fixed rather than resolved from routing.
_QDRANT_PROVIDER = "qdrant"


async def _verify_qdrant_user(request: Request) -> dict:
    """Wrap ``verify_api_key`` to also accept the ``api-key`` header.

    Qdrant clients send credentials via the ``api-key`` header rather than
    the standard ``Authorization`` header, so we map it before delegating.

    Called directly rather than as a dependency, so every parameter is passed:
    one left out keeps its ``Header(...)`` default object, which is truthy.
    ``x_on_behalf_of`` is ``None`` because nobody acts for another user here --
    the collections a caller reaches are scoped by its own user id.
    """
    auth = request.headers.get("authorization")
    x_key = request.headers.get("x-api-key")
    q_key = request.headers.get("api-key")

    if not auth and not x_key and q_key:
        auth = f"Bearer {q_key}"

    services = request.app.state.services
    return await verify_api_key(
        request,
        authorization=auth,
        x_api_key=x_key,
        x_on_behalf_of=None,
        op_store=services.operational_store,
        log_store=services.log_store,
    )


# Allowed path patterns (regex).  Everything else returns 403.
# Patterns match the *path* portion after /v1/qdrant/.
_ALLOWED_PATTERNS: list[re.Pattern[str]] = [
    # Root health check (Roo Code probes GET /v1/qdrant/)
    re.compile(r"^$"),
    # Collection CRUD
    re.compile(r"^collections$"),
    re.compile(r"^collections/[^/]+$"),
    # Points operations
    re.compile(r"^collections/[^/]+/points$"),
    re.compile(r"^collections/[^/]+/points/(search|scroll|recommend|delete|count)$"),
    # Collection info sub-resources
    re.compile(r"^collections/[^/]+/points/payload$"),
    re.compile(r"^collections/[^/]+/index$"),
]

# Regex to capture the collection name segment from the path.
_COLLECTION_RE = re.compile(r"^collections/([^/]+)(/.*)?$")

_NAMESPACE_PREFIX = "fi"

#: What may not be left in a path once the gateway has decoded it: an encoded
#: slash or backslash that Qdrant would decode again, a backslash, and the
#: ``?`` and ``#`` that would end the path in the URL forwarded upstream.
_AMBIGUOUS_PATH_PARTS = ("%2f", "%5c", "\\", "?", "#")


def _normalize_path(path: str) -> str | None:
    """Return the one form of *path* the proxy checks, rewrites and forwards.

    *path* is the part after ``/v1/qdrant/``, already percent-decoded once.
    A trailing slash, which clients send, is dropped. ``None`` for a path the
    upstream could read differently from the allowlist: a leading slash (sent
    as ``//`` or ``%2F``, it passed the allowlist once stripped, skipped tenant
    scoping and reached Qdrant as ``//collections/<anyone's>``), an empty,
    ``.`` or ``..`` segment, and anything in :data:`_AMBIGUOUS_PATH_PARTS`.
    """
    if path.endswith("/"):
        path = path[:-1]
    if not path:
        return ""
    lowered = path.lower()
    if path.startswith("/") or any(part in lowered for part in _AMBIGUOUS_PATH_PARTS):
        return None
    if any(segment in ("", ".", "..") for segment in path.split("/")):
        return None
    return path


def _user_namespace(user_id: str) -> str:
    """Deterministic, short namespace derived from user_id."""
    return hashlib.sha256(user_id.encode()).hexdigest()[:12]


def _scope_collection(name: str, user_id: str) -> str:
    """Prefix a collection name with the tenant namespace."""
    ns = _user_namespace(user_id)
    return f"{_NAMESPACE_PREFIX}_{ns}_{name}"


# Body fields that name another collection, by the allowlisted endpoint that
# takes them: recommend's ``lookup_from`` reads the positive and negative
# examples' vectors from that collection, and creating a collection with
# ``init_from`` copies that one's points. No other allowlisted endpoint takes a
# collection in its body -- search, scroll, count, delete, retrieving or
# upserting points, payload and index changes do not -- and discover and the
# query API, which also take ``lookup_from``, are not allowlisted.
_BODY_COLLECTION_FIELDS: tuple[tuple[re.Pattern[str], tuple[str, ...]], ...] = (
    (re.compile(r"^collections/[^/]+/points/recommend$"), ("lookup_from",)),
    (re.compile(r"^collections/[^/]+$"), ("init_from",)),
)


def _is_collection_name(name: object) -> bool:
    """Whether *name* is a collection name a body may give, to be scoped like the path's."""
    return (
        isinstance(name, str)
        and name not in ("", ".", "..")
        and not any(char in name for char in "/\\\x00")
    )


def _scope_body(body: bytes, path: str, user_id: str) -> bytes | None:
    """Return *body* with every collection it names scoped, or ``None`` if it cannot be.

    For an endpoint that takes no collection in its body, or an empty body,
    *body* itself. Otherwise the body is parsed and written out again, so
    Qdrant reads exactly what was checked: one value per key, whatever the
    original repeated or escaped. ``None`` -- a refusal -- for a body that is
    not a JSON object, or a reference that is not an object with a collection
    name.
    """
    fields = next((names for pattern, names in _BODY_COLLECTION_FIELDS if pattern.match(path)), ())
    if not fields or not body.strip():
        return body
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    for field in fields:
        reference = data.get(field)
        if reference is None:
            # Absent or null: the request reads only its own collection.
            continue
        if not isinstance(reference, dict) or not _is_collection_name(reference.get("collection")):
            return None
        reference["collection"] = _scope_collection(reference["collection"], user_id)
    return json.dumps(data, separators=(",", ":")).encode()


def _unscope_collection(scoped_name: str, user_id: str) -> str | None:
    """Strip the tenant namespace prefix, returning None if it doesn't belong to this user."""
    prefix = f"{_NAMESPACE_PREFIX}_{_user_namespace(user_id)}_"
    if scoped_name.startswith(prefix):
        return scoped_name[len(prefix) :]
    return None


def _rewrite_path(path: str, user_id: str) -> str:
    """Rewrite collection names in the URL path to scoped names."""
    m = _COLLECTION_RE.match(path)
    if m:
        original_name = m.group(1)
        rest = m.group(2) or ""
        return f"collections/{_scope_collection(original_name, user_id)}{rest}"
    # GET /collections (list) — no rewrite needed at path level;
    # response filtering handles tenant isolation.
    return path


def _filter_collections_response(body: bytes, user_id: str) -> bytes:
    """For GET /collections, filter to only this user's collections and de-scope names."""
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return body

    collections = data.get("result", {}).get("collections")
    if collections is None:
        return body

    filtered = []
    for col in collections:
        name = col.get("name", "")
        unscoped = _unscope_collection(name, user_id)
        if unscoped is not None:
            col["name"] = unscoped
            filtered.append(col)

    data["result"]["collections"] = filtered
    return json.dumps(data).encode()


def _unscope_collection_info(body: bytes, user_id: str) -> bytes:
    """For GET /collections/{name}, strip the namespace prefix from result.name."""
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return body

    name = data.get("result", {}).get("name")
    if name:
        unscoped = _unscope_collection(name, user_id)
        if unscoped:
            data["result"]["name"] = unscoped
            return json.dumps(data).encode()
    return body


def _is_allowed(path: str) -> bool:
    """Check the normalized form of *path* against the endpoint allowlist."""
    normalized = _normalize_path(path)
    if normalized is None:
        return False
    return any(pattern.match(normalized) for pattern in _ALLOWED_PATTERNS)


@router.api_route(
    "/v1/qdrant/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
)
async def qdrant_proxy(
    path: str,
    request: Request,
    user_ctx: dict = Depends(_verify_qdrant_user),
) -> Response:
    """Proxy requests to the shared Qdrant instance."""
    request_id = getattr(request.state, "request_id", "")

    # Reject anonymous users even when auth is globally disabled
    if not user_ctx.get("authenticated"):
        logger.warning(
            "qdrant_proxy auth_rejected",
            extra={"request_id": request_id, "method": request.method, "path": path},
        )
        raise HTTPException(401, "Authentication required for Qdrant proxy")

    user_id: str = user_ctx["user_id"]

    # One normalized path is checked, rewritten, forwarded and used to filter
    # the response, so the allowlist and the tenant scoping see the same thing.
    normalized = _normalize_path(path)

    # Endpoint allowlist
    if normalized is None or not _is_allowed(normalized):
        logger.warning(
            "qdrant_proxy endpoint_blocked",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": path,
                "user_id": user_id,
            },
        )
        raise HTTPException(403, f"Qdrant endpoint not allowed: /{path}")

    settings = get_settings()

    # Rewrite collection names for tenant isolation
    rewritten_path = _rewrite_path(normalized, user_id)

    # Build upstream URL preserving query params
    upstream_url = f"{settings.qdrant_base_url.rstrip('/')}/{rewritten_path}"
    if request.url.query:
        upstream_url = f"{upstream_url}?{request.url.query}"

    body = await request.body()
    body_bytes = len(body)
    scoped_body = _scope_body(body, normalized, user_id)
    if scoped_body is None:
        logger.warning(
            "qdrant_proxy body_blocked",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": path,
                "user_id": user_id,
            },
        )
        raise HTTPException(
            403, "Qdrant request names a collection that cannot be scoped to your namespace"
        )
    headers: dict[str, str] = {}
    if settings.qdrant_api_key:
        headers["api-key"] = settings.qdrant_api_key
    content_type = request.headers.get("content-type")
    if content_type:
        headers["content-type"] = content_type

    start = time.perf_counter()
    try:
        client = AsyncHTTPClient.shared()
        timeout = aiohttp.ClientTimeout(total=30)
        status, resp_body, resp_content_type = await client.request(
            method=request.method,
            url=upstream_url,
            data=scoped_body if scoped_body else None,
            headers=headers,
            timeout=timeout,
        )

        latency_ms = int((time.perf_counter() - start) * 1000)
        log_extra = {
            "request_id": request_id,
            "method": request.method,
            "path": path,
            "rewritten_path": rewritten_path,
            "body_bytes": body_bytes,
            "upstream_status": status,
            "latency_ms": latency_ms,
            "user_id": user_id,
        }
        if status >= 400:
            # This proxy relays the upstream status verbatim, so an upstream 401
            # (Qdrant refusing the server-side ``QDRANT_API_KEY``) is otherwise
            # indistinguishable to RequestLogMiddleware and the failed-request
            # rule from the 401 this route raises itself for an anonymous caller
            # — the former is a gateway-credential outage, the latter routine
            # churn. Publishing attribution is what separates them.
            req_ctx.publish_upstream_provider(_QDRANT_PROVIDER)
            logger.warning("qdrant_proxy upstream_error", extra=log_extra)
        else:
            logger.info("qdrant_proxy ok", extra=log_extra)

        # Tenant isolation: rewrite collection names in responses
        if 200 <= status < 300:
            if normalized == "collections" and request.method == "GET":
                resp_body = _filter_collections_response(resp_body, user_id)
            elif _COLLECTION_RE.match(normalized) and request.method == "GET":
                resp_body = _unscope_collection_info(resp_body, user_id)

        return Response(
            content=resp_body,
            status_code=status,
            media_type=resp_content_type,
        )
    except (aiohttp.ClientError, TimeoutError) as exc:
        latency_ms = int((time.perf_counter() - start) * 1000)
        logger.error(
            "qdrant_proxy upstream_unreachable",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": path,
                "rewritten_path": rewritten_path,
                "body_bytes": body_bytes,
                "latency_ms": latency_ms,
                "user_id": user_id,
                "error": str(exc),
            },
        )
        raise HTTPException(502, "Qdrant backend unavailable") from exc
