# Docs RAG Assistant

A retrieval-augmented-generation (RAG) chat feature that answers user questions
about FreeInference using the **public user docs** as its knowledge base. Both
retrieval and generation route through the gateway itself.

## Architecture

```text
                 ingest (offline)                      serving (per request)
docs/free_inference/docs/source/*.md            ┌──────────────────────────────────┐
        │  chunk (by heading)                    │ POST /v1/rag/chat                 │
        ▼                                         │  1. embed query (bge-m3 adapter) │
   embed each chunk ──► /v1/embeddings (bge-m3)   │  2. cosine top-k over the index  │
        │                                         │  3. build grounded prompt        │
        ▼                                         │  4. RouteExecutor.stream_chat_…  │
   serving/rag/prebuilt/docs_index.json ─────────►│     → SSE (sources + answer)     │
                                                  └──────────────────────────────────┘
```

- **Corpus:** `docs/free_inference/docs/source/*.md` — the same markdown that
  builds the public doc site.
- **Vector store:** a plain JSON file
  (`apps/backend/serving/rag/prebuilt/docs_index.json`) scanned with pure-Python
  cosine similarity. The corpus is tiny, so no numpy / ANN index is needed. The
  index is **committed** (embeddings rounded to 6 decimals, ~0.9 MB) and lives
  inside the `serving` package so it ships in the Docker image — a fresh
  container serves retrieval immediately, with no build-time embedding call.
- **Embeddings & chat route through the gateway.** Ingest calls the
  OpenAI-compatible `/v1/embeddings` endpoint; the serving endpoint embeds the
  query with the in-process embedding adapter and generates the answer with the
  routing engine — no self-HTTP round-trip.

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

`RAG_GATEWAY_BASE_URL` defaults to `https://freeinference.org/v1`; the key must
be a valid user API key on that gateway. Chunks are embedded with the same
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

## Endpoints

Both live under the gateway and authenticate with the dashboard JWT
(`get_current_user`), so the Next.js chat page calls them with the session token
it already holds.

- `GET /v1/rag/status` — whether the index is built, chunk count, models.
- `POST /v1/rag/chat` — body `{ messages, model?, top_k?, stream? }`.
  - Streaming (default): SSE — first a `{"type":"sources", ...}` event, then
    OpenAI-format completion chunks, then `[DONE]`.
  - Non-streaming: `{ answer, sources, model }`.

```bash
curl -sN https://staging.freeinference.org/v1/rag/chat \
  -H "Authorization: Bearer <jwt>" -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"How do I get an API key?"}]}'
```

## Configuration

All optional; sensible defaults resolve relative to the repo root.

| Env var | Default | Purpose |
|---|---|---|
| `RAG_INDEX_PATH` | `serving/rag/prebuilt/docs_index.json` | Vector index location |
| `RAG_CORPUS_DIR` | `docs/free_inference/docs/source` | Markdown corpus |
| `RAG_EMBEDDER` | `gateway` | `gateway` (real bge-m3) or `hash` (offline) |
| `RAG_GATEWAY_BASE_URL` | `https://freeinference.org/v1` | Gateway used by ingest (gateway mode) |
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
