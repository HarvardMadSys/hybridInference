"""RAG docs-assistant endpoints.

Answers questions about FreeInference using the public user docs as a knowledge
base. The handler is a thin orchestrator over the gateway's *own* public API:

* the query is embedded via ``POST {RAG_API_BASE_URL}/embeddings`` and
* the answer is generated via ``POST {RAG_API_BASE_URL}/chat/completions``,

both authenticated with ``RAG_API_KEY`` (a user API key). Calling the gateway as
a user means these requests flow through the standard ``/v1/embeddings`` and
``/v1/chat/completions`` handlers, so they are logged to ``api_logs`` and count
toward cost / quota / concurrency — unlike direct in-process router calls, which
bypass all of that.

Retrieval (cosine over the JSON index) stays in-process. The index is built
offline by ``python -m serving.rag.ingest`` and loaded lazily (cached, with
mtime-based invalidation).

Frontend access is gated by ``get_current_user`` (JWT — the same dependency the
dashboard uses); the upstream model calls are billed to the ``RAG_API_KEY``
account.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from serving.rag.config import RagSettings, load_rag_settings
from serving.rag.embedder import HashEmbedder
from serving.rag.pipeline import build_messages, sources_payload
from serving.rag.store import VectorStore
from serving.servers.deps import get_current_user
from serving.utils.logging import get_logger

logger = get_logger(__name__)
router = APIRouter(prefix="/v1/rag", tags=["RAG"])

# Streaming/embedding timeout: each streamed chunk resets the read clock, so the
# read timeout only bounds stalls, not total duration; embeddings are fast.
_HTTP_TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0)
# Non-streamed generation has no intermediate chunks to reset the read clock, so
# a slow-but-successful answer needs a long total read budget — otherwise it 503s
# spuriously while the upstream call keeps running and gets billed to RAG_API_KEY.
_HTTP_TIMEOUT_JSON = httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0)

# Lazily-loaded index, cached across requests and invalidated when the file's
# mtime changes so a re-ingest is picked up without a restart. The stat + parse
# run in a worker thread so the (~1 MB) load never blocks the event loop. No
# lock is used: a concurrent cold-cache load is harmless (both produce the same
# store), and a module-level asyncio.Lock would bind to one event loop.
_store: VectorStore | None = None
_store_path: str | None = None
_store_mtime: float | None = None


async def _load_store() -> VectorStore | None:
    """Return the cached index, (re)loading from disk off the event loop."""
    global _store, _store_path, _store_mtime
    path = load_rag_settings().index_path
    try:
        mtime = (await asyncio.to_thread(path.stat)).st_mtime
    except OSError:
        _store = None
        _store_path = None
        _store_mtime = None
        return None
    if _store is not None and _store_path == str(path) and _store_mtime == mtime:
        return _store
    try:
        loaded = await asyncio.to_thread(VectorStore.load, path)
    except Exception as exc:
        # A concurrent/partial re-ingest can momentarily yield an unparseable
        # file; keep serving the previously loaded index rather than 500.
        logger.warning(f"failed to load rag index at {path}: {exc}")
        return _store
    _store = loaded
    _store_path = str(path)
    _store_mtime = mtime
    logger.info(
        f"rag index loaded: {path} ({len(_store.records)} chunks, model={_store.embed_model})"
    )
    return _store


class RagMessage(BaseModel):
    """One chat turn."""

    role: str
    content: str


class RagChatRequest(BaseModel):
    """Request body for the RAG chat endpoint."""

    messages: list[RagMessage] = Field(..., min_length=1)
    top_k: int | None = Field(default=None, ge=1, le=20)
    stream: bool = True


# --------------------------------------------------------------------------- #
# Gateway-as-a-user HTTP helpers (module-level so tests can monkeypatch them). #
# --------------------------------------------------------------------------- #

# Sent on the self-calls so RAG-originated traffic is identifiable in api_logs
# (the /v1/* handlers record metadata.user_agent) and distinguishable from other
# users of the RAG_API_KEY account.
_USER_AGENT = "doc_assistant"


def _auth_headers(settings: RagSettings, on_behalf_of: str | None = None) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {settings.api_key}",
        "Content-Type": "application/json",
        "User-Agent": _USER_AGENT,
    }
    # Attribute the self-call to the real end user (verified by JWT at
    # /v1/rag/chat) rather than to the shared RAG_API_KEY account. The gateway
    # honors this header only for the trusted RAG service key, so a stray value
    # on any other key is ignored.
    if on_behalf_of:
        headers["X-On-Behalf-Of"] = on_behalf_of
    return headers


def _require_api_key(settings: RagSettings) -> None:
    if not settings.api_key:
        raise HTTPException(
            status_code=503,
            detail="RAG is not configured: set RAG_API_KEY to a valid user API key.",
        )


def _map_upstream_error(status: int, where: str) -> HTTPException:
    """Translate an upstream status into a client-facing error.

    429 (rate/quota) passes through so the caller sees it; an auth failure means
    our ``RAG_API_KEY`` is bad (a server misconfig, not the client's fault) and
    maps to 502; anything else maps to 502.
    """
    if status == 429:
        return HTTPException(status_code=429, detail=f"RAG {where}: rate/quota exceeded.")
    if status in (401, 403):
        return HTTPException(
            status_code=502, detail=f"RAG {where}: auth failed (check RAG_API_KEY)."
        )
    return HTTPException(status_code=502, detail=f"RAG {where}: upstream error ({status}).")


async def _gateway_embed(
    settings: RagSettings, model: str, text: str, on_behalf_of: str | None = None
) -> list[float]:
    """Embed ``text`` by calling the gateway's /v1/embeddings as a user."""
    _require_api_key(settings)
    url = f"{settings.api_base_url}/embeddings"
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.post(
                url,
                headers=_auth_headers(settings, on_behalf_of),
                json={"model": model, "input": text},
            )
    except httpx.HTTPError as exc:
        logger.warning(f"rag embed call failed: {exc}")
        raise HTTPException(
            status_code=503, detail="Embedding service temporarily unavailable."
        ) from exc
    if resp.status_code >= 400:
        raise _map_upstream_error(resp.status_code, "embedding")
    try:
        vector = [float(x) for x in resp.json()["data"][0]["embedding"]]
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=502, detail="Malformed embedding response") from exc
    if not vector:
        raise HTTPException(status_code=502, detail="Malformed embedding response")
    return vector


async def _gateway_chat_json(
    settings: RagSettings,
    model: str,
    messages: list[dict[str, Any]],
    on_behalf_of: str | None = None,
) -> str:
    """Generate a non-streamed answer via /v1/chat/completions as a user."""
    _require_api_key(settings)
    url = f"{settings.api_base_url}/chat/completions"
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "temperature": settings.temperature,
        "max_tokens": settings.max_tokens,
    }
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_JSON) as client:
            resp = await client.post(
                url, headers=_auth_headers(settings, on_behalf_of), json=payload
            )
    except httpx.HTTPError as exc:
        logger.warning(f"rag chat call failed: {exc}")
        raise HTTPException(
            status_code=503, detail="Generation service temporarily unavailable."
        ) from exc
    if resp.status_code >= 400:
        raise _map_upstream_error(resp.status_code, "generation")
    try:
        return resp.json()["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=502, detail="Malformed generation response") from exc


async def _open_chat_stream(
    settings: RagSettings,
    model: str,
    messages: list[dict[str, Any]],
    on_behalf_of: str | None = None,
):
    """Open a streamed /v1/chat/completions call as a user.

    Sends the request and checks the status *before* returning so a pre-first-
    byte upstream error (auth/quota/5xx) surfaces as a proper HTTP status. A
    failure that occurs mid-stream arrives in-band as a ``data: {"error":...}``
    event and is proxied through. Returns ``(byte_iterator, aclose)``; ``aclose``
    is idempotent and must be awaited once the stream is drained.
    """
    _require_api_key(settings)
    url = f"{settings.api_base_url}/chat/completions"
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "temperature": settings.temperature,
        "max_tokens": settings.max_tokens,
    }
    client = httpx.AsyncClient(timeout=_HTTP_TIMEOUT)
    request = client.build_request(
        "POST", url, headers=_auth_headers(settings, on_behalf_of), json=payload
    )
    try:
        resp = await client.send(request, stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        logger.warning(f"rag chat stream failed: {exc}")
        raise HTTPException(
            status_code=503, detail="Generation service temporarily unavailable."
        ) from exc
    if resp.status_code >= 400:
        await resp.aread()
        await resp.aclose()
        await client.aclose()
        raise _map_upstream_error(resp.status_code, "generation")

    closed = False

    async def _aclose() -> None:
        nonlocal closed
        if closed:
            return
        closed = True
        await resp.aclose()
        await client.aclose()

    # aiter_bytes content-decodes (unlike aiter_raw), so proxying stays correct
    # even if the upstream response is ever content-encoded.
    return resp.aiter_bytes(), _aclose


async def _embed_query(
    store: VectorStore, query: str, settings: RagSettings, on_behalf_of: str | None = None
) -> list[float]:
    """Embed the query, matching the embedder that built the index.

    A ``gateway`` index is embedded through the gateway (logged); a ``hash``
    index (offline dev/CI only) is embedded in-process.
    """
    if store.embedder_mode == "hash":
        return HashEmbedder(dim=store.dim, model=store.embed_model).embed_query(query)
    return await _gateway_embed(settings, store.embed_model, query, on_behalf_of)


@router.get("/status")
async def rag_status(
    _user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Report whether the index is built and which models it uses."""
    settings = load_rag_settings()
    store = await _load_store()
    return {
        "index_loaded": store is not None,
        "num_chunks": len(store.records) if store else 0,
        "embed_model": store.embed_model if store else settings.embed_model,
        "embedder_mode": store.embedder_mode if store else settings.embedder_mode,
        "chat_model": settings.chat_model,
    }


@router.post("/chat")
async def rag_chat(
    body: RagChatRequest,
    user: dict[str, Any] = Depends(get_current_user),
) -> Any:
    """Answer the latest user question, grounded in retrieved docs."""
    settings = load_rag_settings()
    # The gateway self-calls (embed + generate) act on behalf of this JWT-verified
    # end user, so their api_logs / cost / quota / concurrency attribute to the
    # real user rather than to the shared RAG_API_KEY service account.
    on_behalf_of = user.get("user_id")
    store = await _load_store()
    if store is None:
        raise HTTPException(
            status_code=503,
            detail="RAG index not built. Run `python -m serving.rag.ingest` first.",
        )

    # The query is the most recent user turn; history is every turn strictly
    # before it, so the query is never duplicated into history when the request
    # ends with a non-user (assistant/system) turn.
    query_idx = next(
        (i for i in range(len(body.messages) - 1, -1, -1) if body.messages[i].role == "user"),
        None,
    )
    if query_idx is None:
        raise HTTPException(status_code=400, detail="No user message to answer.")
    query = body.messages[query_idx].content.strip()
    if not query:
        raise HTTPException(status_code=400, detail="Empty user message.")
    history = [m.model_dump() for m in body.messages[:query_idx]]

    query_vec = await _embed_query(store, query, settings, on_behalf_of)
    if store.dim and len(query_vec) != store.dim:
        # Fail loud instead of letting cosine_similarity silently return 0.0 for
        # every record (which would yield plausible-looking but garbage sources).
        raise HTTPException(
            status_code=502,
            detail=(
                f"Query embedding dimension {len(query_vec)} does not match index "
                f"dimension {store.dim}; rebuild the index."
            ),
        )
    top_k = body.top_k or settings.top_k
    results = store.search(query_vec, top_k)
    messages = build_messages(query, results, history)
    sources = sources_payload(results)
    # The generation model is fixed server-side (not client-selectable) so this
    # endpoint can't be used to reach role-gated models by passing a model id.
    model = settings.chat_model

    if not body.stream:
        answer = await _gateway_chat_json(settings, model, messages, on_behalf_of)
        return {"answer": answer, "sources": sources, "model": model}

    # Open the upstream stream up front so a *pre-first-byte* auth/quota/5xx error
    # surfaces as a real HTTP status instead of a broken 200 stream.
    byte_iter, aclose = await _open_chat_stream(settings, model, messages, on_behalf_of)

    async def _generate():
        # Emit retrieved sources first so the UI can render citations before the
        # answer streams in, then proxy the upstream OpenAI-format SSE bytes.
        try:
            yield f"data: {json.dumps({'type': 'sources', 'sources': sources})}\n\n"
            async for chunk in byte_iter:
                yield chunk
        finally:
            await aclose()

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
        # Release the upstream httpx connection deterministically even if the
        # client disconnects mid-stream (when the generator's finally may only run
        # at async-gen finalization). aclose is idempotent, so a double call is safe.
        background=BackgroundTask(aclose),
    )
