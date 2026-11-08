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

Every tenant shares one Qdrant, so what one account may make it do is bounded:
- at most :data:`MAX_INFLIGHT_PER_USER` requests in flight per account, the
  next refused with 429 rather than queued;
- a response is read up to :data:`MAX_RESPONSE_BYTES`, not the 256 MiB an
  inference response may take;
- a collection is created or changed only with the options that shape its own
  data (:func:`_sanitize_collection_options`): the ones that size the shared
  instance -- shards, replication, the optimizer, the write-ahead log, strict
  mode -- are dropped, and the HNSW graph is held to what IDE indexers ask for;
- an account keeps at most :data:`MAX_COLLECTIONS_PER_USER` collections.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

import aiohttp
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from serving.config.settings import get_settings
from serving.http import AsyncHTTPClient, UpstreamPayloadTooLarge
from serving.servers.auth import verify_api_key
from serving.utils import context as req_ctx
from serving.utils.logging import get_logger

logger = get_logger(__name__)
router = APIRouter()

if TYPE_CHECKING:
    from collections.abc import Iterator

#: Provider label for upstream Qdrant failures relayed to the client. This proxy
#: has no ModelConfig, so the label is fixed rather than resolved from routing.
_QDRANT_PROVIDER = "qdrant"

#: Most requests one account may have in flight through the proxy. IDE indexers
#: (Roo Code, Kilo Code) upsert about ten batches at a time; past this limit a
#: request is refused with 429 rather than queued, so one account cannot hold
#: the shared Qdrant's connections, or the gateway's memory, with as many
#: requests as it opens.
MAX_INFLIGHT_PER_USER = 16

#: Largest Qdrant response the proxy reads. A search or a page of points with
#: their payloads is well under a MiB; a scroll that returns the vectors of a
#: thousand points is about ten. A larger one is answered with 502, and asking
#: for fewer points (or no vectors) gets it through.
MAX_RESPONSE_BYTES = 16 * 1024 * 1024

#: Most collections one account may keep. An IDE indexer makes one per
#: workspace it indexes and never deletes them.
MAX_COLLECTIONS_PER_USER = 100

#: Requests each account has in flight, for :data:`MAX_INFLIGHT_PER_USER`.
_inflight: dict[str, int] = {}


@contextmanager
def _inflight_slot(user_id: str) -> Iterator[None]:
    """Hold one of *user_id*'s proxy slots, or refuse with 429 when all are taken.

    No ``await`` separates the check from the count, so this is race-free on
    the one event loop.
    """
    count = _inflight.get(user_id, 0)
    if count >= MAX_INFLIGHT_PER_USER:
        raise HTTPException(
            429,
            f"Too many Qdrant requests in flight: at most {MAX_INFLIGHT_PER_USER} per account",
            headers={"Retry-After": "1"},
        )
    _inflight[user_id] = count + 1
    try:
        yield
    finally:
        left = _inflight[user_id] - 1
        if left:
            _inflight[user_id] = left
        else:
            del _inflight[user_id]


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


#: The collection endpoint: PUT creates a collection, PATCH changes its options.
_COLLECTION_PATH_RE = re.compile(r"^collections/[^/]+$")

#: Collection options that size the shared instance rather than a tenant's own
#: data -- its shards and replicas, its optimizer and write-ahead log, its
#: quantized copies in RAM, strict mode -- dropped from what a tenant sends.
#: ``params`` is the update endpoint's name for the replication settings.
_INSTANCE_OPTIONS = frozenset(
    {
        "shard_number",
        "sharding_method",
        "replication_factor",
        "write_consistency_factor",
        "optimizers_config",
        "wal_config",
        "quantization_config",
        "strict_mode_config",
        "params",
    }
)

#: The HNSW options a tenant may set, each up to the value IDE indexers ask for
#: (Roo Code builds its index with ``m`` 64 and ``ef_construct`` 512); a larger
#: graph costs the shared instance memory and indexing CPU for every point.
_HNSW_LIMITS = {"m": 64, "payload_m": 64, "ef_construct": 512}
_HNSW_FLAGS = frozenset({"on_disk"})


def _sanitize_hnsw(config: Any) -> Any:
    if not isinstance(config, dict):
        return config
    kept: dict[str, Any] = {}
    for key, value in config.items():
        if key in _HNSW_FLAGS:
            kept[key] = value
        elif key in _HNSW_LIMITS:
            limit = _HNSW_LIMITS[key]
            kept[key] = min(value, limit) if isinstance(value, int) else value
    return kept


def _sanitize_vector_params(params: Any) -> Any:
    """One vector's options, without its own quantization and with a bounded HNSW graph."""
    if not isinstance(params, dict):
        return params
    kept = {key: value for key, value in params.items() if key != "quantization_config"}
    if "hnsw_config" in kept:
        kept["hnsw_config"] = _sanitize_hnsw(kept["hnsw_config"])
    return kept


def _sanitize_collection_options(data: dict[str, Any]) -> list[str]:
    """Drop or bound, in place, the options of a collection body that size the shared instance.

    Returns what was dropped or bounded, for the log. What shapes the tenant's
    own data -- its vectors' size and distance, sparse vectors, where payloads
    live -- passes as sent.
    """
    changed = sorted(key for key in data if key in _INSTANCE_OPTIONS)
    for key in changed:
        del data[key]
    if "hnsw_config" in data:
        bounded = _sanitize_hnsw(data["hnsw_config"])
        if bounded != data["hnsw_config"]:
            changed.append("hnsw_config")
        data["hnsw_config"] = bounded
    vectors = data.get("vectors")
    if isinstance(vectors, dict):
        # One unnamed vector's options, or named vectors, each with its own.
        named = not ("size" in vectors or "distance" in vectors)
        bounded_vectors = (
            {name: _sanitize_vector_params(params) for name, params in vectors.items()}
            if named
            else _sanitize_vector_params(vectors)
        )
        if bounded_vectors != vectors:
            changed.append("vectors")
        data["vectors"] = bounded_vectors
    return changed


def _is_collection_name(name: object) -> bool:
    """Whether *name* is a collection name a body may give, to be scoped like the path's."""
    return (
        isinstance(name, str)
        and name not in ("", ".", "..")
        and not any(char in name for char in "/\\\x00")
    )


def _scope_body(
    body: bytes, path: str, user_id: str, *, method: str = "PUT", changed: list[str] | None = None
) -> bytes | None:
    """Return *body* with every collection it names scoped, or ``None`` if it cannot be.

    For an endpoint that takes no collection in its body, or an empty body,
    *body* itself. Otherwise the body is parsed and written out again, so
    Qdrant reads exactly what was checked: one value per key, whatever the
    original repeated or escaped. ``None`` -- a refusal -- for a body that is
    not a JSON object, or a reference that is not an object with a collection
    name. A body that creates or changes a collection also loses the options
    that size the shared instance (:func:`_sanitize_collection_options`); what
    it lost is appended to *changed*.
    """
    fields = next((names for pattern, names in _BODY_COLLECTION_FIELDS if pattern.match(path)), ())
    configures = method in ("PUT", "PATCH") and _COLLECTION_PATH_RE.match(path) is not None
    if not (fields or configures) or not body.strip():
        return body
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if configures:
        dropped = _sanitize_collection_options(data)
        if changed is not None:
            changed.extend(dropped)
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


async def _refuse_past_collection_limit(
    client: Any, base_url: str, headers: dict[str, str], scoped_name: str, user_id: str
) -> None:
    """Refuse (403) a new collection for an account that has its share already.

    Reads the instance's collection list; a collection that already exists is
    left for Qdrant to answer. Fails closed: when the list cannot be read, the
    collection is not created.
    """
    status, body, _ = await client.request(
        method="GET",
        url=f"{base_url}/collections",
        headers={key: value for key, value in headers.items() if key == "api-key"},
        timeout=aiohttp.ClientTimeout(total=30),
        max_body_bytes=MAX_RESPONSE_BYTES,
    )
    if not 200 <= status < 300:
        raise HTTPException(502, "Qdrant backend unavailable")
    try:
        collections = json.loads(body)["result"]["collections"]
        names = {entry["name"] for entry in collections}
    except (ValueError, KeyError, TypeError):
        raise HTTPException(502, "Qdrant backend unavailable") from None
    if scoped_name in names:
        return
    owned = sum(1 for name in names if _unscope_collection(name, user_id) is not None)
    if owned >= MAX_COLLECTIONS_PER_USER:
        raise HTTPException(
            403,
            f"Collection limit reached: an account may keep at most {MAX_COLLECTIONS_PER_USER} "
            "collections. Delete one before creating another",
        )


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

    # Held from before the body is read until the response is built.
    with _inflight_slot(user_id):
        return await _forward(request, path, normalized, user_id, request_id)


async def _forward(
    request: Request, path: str, normalized: str, user_id: str, request_id: str
) -> Response:
    """Forward one allowed request for *user_id* and answer with Qdrant's response."""
    settings = get_settings()

    # Rewrite collection names for tenant isolation
    rewritten_path = _rewrite_path(normalized, user_id)

    # Build upstream URL preserving query params
    base_url = settings.qdrant_base_url.rstrip("/")
    upstream_url = f"{base_url}/{rewritten_path}"
    if request.url.query:
        upstream_url = f"{upstream_url}?{request.url.query}"

    body = await request.body()
    body_bytes = len(body)
    changed_options: list[str] = []
    scoped_body = _scope_body(
        body, normalized, user_id, method=request.method, changed=changed_options
    )
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
    if changed_options:
        logger.info(
            "qdrant_proxy collection_options_bounded",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": path,
                "user_id": user_id,
                "detail": ",".join(changed_options),
            },
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
        if request.method == "PUT" and _COLLECTION_PATH_RE.match(normalized):
            await _refuse_past_collection_limit(
                client, base_url, headers, rewritten_path.removeprefix("collections/"), user_id
            )
        timeout = aiohttp.ClientTimeout(total=30)
        status, resp_body, resp_content_type = await client.request(
            method=request.method,
            url=upstream_url,
            data=scoped_body if scoped_body else None,
            headers=headers,
            timeout=timeout,
            max_body_bytes=MAX_RESPONSE_BYTES,
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
    except UpstreamPayloadTooLarge as exc:
        logger.warning(
            "qdrant_proxy response_too_large",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": path,
                "user_id": user_id,
                "limit": MAX_RESPONSE_BYTES,
            },
        )
        raise HTTPException(
            502,
            f"Qdrant response exceeds the proxy's {MAX_RESPONSE_BYTES // (1024 * 1024)} MiB "
            "limit; ask for fewer points, or leave out their vectors",
        ) from exc
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
