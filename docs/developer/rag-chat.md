# Docs RAG Assistant

A retrieval-augmented-generation (RAG) chat feature that answers user questions
about FreeInference using the **public user docs** as its knowledge base. Both
retrieval and generation route through the gateway itself.

## Architecture

`/v1/rag/chat` is a thin orchestrator: it retrieves in-process, then calls the
gateway's **own** public API **as a user** for the model work.

```text
   POST /v1/rag/chat  (JWT-gated)
     1. embed query   ── HTTP ─►  POST {RAG_API_BASE_URL}/embeddings      (bge-m3)
     2. cosine top-k over the committed JSON index          (in-process)
     3. build grounded prompt with citations                (in-process)
     4. generate      ── HTTP ─►  POST {RAG_API_BASE_URL}/chat/completions (qwen3.6-35b)
     → SSE: sources event, then the proxied OpenAI chunks, then [DONE]

   Both HTTP calls carry RAG_API_KEY, so they flow through the standard
   /v1/embeddings and /v1/chat/completions handlers → logged to api_logs and
   counted toward cost / quota / concurrency.
```

- **Corpus:** the active distribution overlay's documentation source
  (`<overlay>/content/docs/docs/source/*.md`) — the same markdown that builds
  that deployment's public doc site. A checkout with no overlay has no corpus;
  set `RAG_CORPUS_DIR` to your own documentation.
- **Vector store:** a plain JSON file
  (`<overlay>/content/rag/docs_index.json`) scanned with pure-Python
  cosine similarity. The corpus is tiny, so no numpy / ANN index is needed. The
  index is **committed** (embeddings rounded to 6 decimals, ~0.9 MB) and lives
  inside the `serving` package so it ships in the Docker image — a fresh
  container serves retrieval immediately, with no build-time embedding call.
- **Why call the gateway as a user (over HTTP) instead of the in-process
  router?** So RAG requests are observable and metered. Direct
  `RouteExecutor` / adapter calls bypass the per-request logging, cost, quota,
  and concurrency that live in the `/v1/*` route handlers. Routing the model
  work back through those endpoints reuses all of it for free.

## Code map

| Path | Role |
|---|---|
| `apps/backend/serving/rag/chunker.py` | Heading-aware markdown chunking |
| `apps/backend/serving/rag/embedder.py` | `GatewayHTTPEmbedder` + offline `HashEmbedder` |
| `apps/backend/serving/rag/store.py` | JSON vector store + cosine search |
| `apps/backend/serving/rag/pipeline.py` | Prompt assembly + sources payload |
| `apps/backend/serving/rag/ingest.py` | `python -m serving.rag.ingest` CLI |
| `apps/backend/serving/servers/routers/rag.py` | `/v1/rag/status` + `/v1/rag/chat` |
| `apps/frontend/src/app/chat/page.tsx` | Chat UI (`/chat`, behind `ProtectedRoute`) |
| `apps/frontend/src/lib/api/chat.ts` | Streaming SSE client |

## Rebuilding the index

The committed index is prebuilt with real `bge-m3` embeddings. Regenerate it
(e.g. after the docs change) with the default `gateway` embedder:

```bash
RAG_GATEWAY_API_KEY=hyi-xxx make rag-ingest      # real bge-m3, 1024-dim
```

`RAG_GATEWAY_BASE_URL` defaults to `http://localhost:8080/v1` — embedding is
billable work, so a clone draws on its own gateway rather than on whoever wrote
the default. Point it at the gateway you want to embed through; the key must be
a valid user API key on *that* gateway. Chunks are embedded with the same
`bge-m3` model the serving endpoint uses at query time, so query and document
vectors share one space.

For an offline run with no gateway/key (weak retrieval — dev/CI only):

```bash
RAG_EMBEDDER=hash make rag-ingest
```

> The serving endpoint embeds the query with whichever embedder built the index
> (recorded in the index metadata) and **fails loud** (HTTP 502) if the query
> vector's dimension doesn't match the index. Always rebuild after switching
> embedders.

## Deployment

The index ships inside the image (it lives under `serving/`), so no extra deploy
step is required. To refresh it, rebuild the image after re-running
`make rag-ingest`. `/v1/rag/status` reports `index_loaded`, the embedder mode,
and chunk count for a post-deploy check. If the embedding backend is unavailable
at query time, `/v1/rag/chat` returns a graceful `503` rather than a 500.

**Required env per deployment:** set `RAG_API_KEY` to a valid user API key, and
point `RAG_API_BASE_URL` at the gateway's own address for that environment — the
default `http://localhost:8080/v1` matches the prod Docker container's port, but
staging (systemd) binds `8000`, so it needs `RAG_API_BASE_URL=http://localhost:8000/v1`.
Because the inner calls authenticate as a single service account, all RAG cost /
quota / logs attribute to `RAG_API_KEY`, and the outer `/v1/rag/chat` has no
per-user concurrency limit of its own — size the `RAG_API_KEY` account's quota
accordingly.

## Endpoints

Both live under the gateway and authenticate with the dashboard JWT
(`get_current_user`), so the Next.js chat page calls them with the session token
it already holds.

- `GET /v1/rag/status` — whether the index is built, chunk count, models.
- `POST /v1/rag/chat` — body `{ messages, top_k?, stream? }`. The generation
  model is fixed server-side (`RAG_CHAT_MODEL`); it is **not** client-selectable,
  so the endpoint can't be used to reach role-gated models.
  - Streaming (default): SSE — first a `{"type":"sources", ...}` event, then
    OpenAI-format completion chunks, then `[DONE]`.
  - Non-streaming: `{ answer, sources, model }`.

### Logging & quota

The RAG model calls go through the gateway's own `/v1/embeddings` and
`/v1/chat/completions`, so they land in `api_logs` and count toward cost, daily
quota, and per-user concurrency — attributed to the **`RAG_API_KEY` account**
(not the end user, who authenticates to `/v1/rag/chat` with a JWT). Set
`RAG_API_KEY` to a valid user API key; when it is unset the endpoint returns
`503`. An upstream `429` (quota/rate) is passed through to the caller.

```bash
curl -sN https://staging.freeinference.org/v1/rag/chat \
  -H "Authorization: Bearer <jwt>" -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"How do I get an API key?"}]}'
```

## Configuration

All optional; sensible defaults resolve relative to the repo root.

| Env var | Default | Purpose |
|---|---|---|
| `RAG_API_KEY` | _(unset)_ | User API key the handler calls the gateway with (**required** at serving time) |
| `RAG_API_BASE_URL` | `http://localhost:8080/v1` | Gateway the handler calls (self-call for logging/quota) |
| `RAG_INDEX_PATH` | the overlay's `content/rag/docs_index.json`, if one is present | Vector index location |
| `RAG_CORPUS_DIR` | the overlay's `content/docs/docs/source`, if one is present | Markdown corpus |
| `RAG_EMBEDDER` | `gateway` | `gateway` (real bge-m3) or `hash` (offline) |
| `RAG_GATEWAY_BASE_URL` | `http://localhost:8080/v1` | Gateway used by **ingest** (gateway mode) |
| `RAG_EMBED_MODEL` | `bge-m3` | Embedding model id (gateway mode) |
| `RAG_CHAT_MODEL` | `qwen3.6-35b` | Answer-generation model |
| `RAG_TOP_K` | `4` | Chunks retrieved per query |
| `RAG_MAX_TOKENS` | `1024` | Answer token budget |
| `RAG_TEMPERATURE` | `0.3` | Generation temperature |

## Prototype limitations

- The committed index is refreshed manually (`make rag-ingest` + rebuild image),
  not on a schedule — it can lag the docs until regenerated.
- The `HashEmbedder` fallback exists only so the pipeline runs without the
  gateway (dev/CI); its retrieval quality is weak.
- No answer caching, no reranking, and history is truncated to the last few
  turns. The store is loaded into memory per process (cached, mtime-invalidated).
