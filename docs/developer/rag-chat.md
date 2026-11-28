# Docs RAG Assistant

The docs assistant answers users' questions from the deployment's public user docs, using retrieval-augmented generation (RAG). It needs the docs as Markdown, an index built from them, and an API key to call the gateway with. Until all three exist it answers `503`; a checkout without a distribution has none of them. It is an early feature with known [limitations](#limitations).

## How it works

`/v1/rag/chat` retrieves in-process and calls the gateway's own public API, as a user, for the model work:

```text
   POST /v1/rag/chat  (JWT-gated)
     1. embed query   ── HTTP ─►  POST {RAG_API_BASE_URL}/embeddings  (RAG_EMBED_MODEL)
     2. cosine top-k over the JSON vector index             (in-process)
     3. build grounded prompt with citations                (in-process)
     4. generate      ── HTTP ─►  POST {RAG_API_BASE_URL}/chat/completions (RAG_CHAT_MODEL)
     → SSE: sources event, then the proxied OpenAI chunks, then [DONE]
```

Both calls present `RAG_API_KEY`, so they go through the normal `/v1/embeddings` and `/v1/chat/completions` handlers and are logged and counted toward cost, quota and concurrency like any request. Calling the router in-process would skip those checks. The calls also carry `X-On-Behalf-Of: <user id>`, so the signed-in user is charged and their daily and monthly quotas apply (an account without an API key gets the daily quota its first key would). The gateway honors that header only on requests made with `RAG_API_KEY`. Log rows carry `metadata.user_agent = "doc_assistant"`. An upstream `429` is passed through and may mean the user's daily quota is spent.

The corpus is the active distribution overlay's `<overlay>/content/docs/docs/source/*.md`, the Markdown that builds its public doc site. The index is a JSON file searched with pure-Python cosine similarity, by default `<overlay>/content/rag/docs_index.json`. `RAG_CORPUS_DIR` and `RAG_INDEX_PATH` override these paths; without an overlay you must set them.

## Setting it up

1. Set `RAG_API_KEY` to a user API key, and `RAG_API_BASE_URL` to the gateway's own address. The default, `http://localhost:8080/v1`, is the Compose backend's port; if your backend listens elsewhere, every RAG request fails until you set it. Both are on the admin console's **Settings** tab, under **Setup** → **Integrations**.
2. Build the index with a real embedding model, through a gateway:

   ```bash
   RAG_CORPUS_DIR=path/to/docs RAG_GATEWAY_API_KEY=hyi-xxx make rag-ingest
   ```

   `RAG_GATEWAY_BASE_URL` chooses the gateway; it defaults to `http://localhost:8080/v1`, your own, because embedding is billable work. The key must be a user API key on that gateway. If the `DB_*` settings in `.env` reach the gateway's database, stored settings win over the command line; if not, the tool warns and uses the environment (with `DB_ENABLED=false` it doesn't try).
3. Make the JSON file readable at `RAG_INDEX_PATH`. Compose mounts the overlay into the backend, so replacing the file under the overlay's `content/rag/` is enough; each process reloads the index when the file's modification time changes.
4. Check `GET /v1/rag/status`.

Queries are embedded with the embedder recorded in the index. If the query vector's dimension doesn't match the index, the endpoint fails with `502`, so rebuild the index whenever you change embedders. For development and CI without a gateway, `RAG_EMBEDDER=hash make rag-ingest` builds an offline index with weak retrieval.

To check whether the committed index still matches the corpus, without embedding anything or calling a gateway:

```bash
PYTHONPATH=apps/backend uv run python -m serving.rag.ingest --check
```

It exits `0` when the index is current, `1` when it has drifted and `2` when it cannot tell.

## Endpoints

Both authenticate with the dashboard JWT (`get_current_user`), so the Next.js chat page calls them with the session token it already holds.

- `GET /v1/rag/status` returns `index_loaded`, `num_chunks`, `embed_model`, `embedder_mode` and `chat_model`.
- `POST /v1/rag/chat` takes `{ messages, top_k?, stream? }` (`top_k` 1–20). The model is `RAG_CHAT_MODEL`, fixed on the server, so the endpoint cannot reach role-gated models. Streaming (the default) sends a `{"type":"sources", ...}` event, the OpenAI-format chunks, then `[DONE]`; non-streaming returns `{ answer, sources, model }`. If the embedding or generation backend is unavailable, it answers `503`.

## Turning it off

`features.rag: false` in the `SITE_CONFIGURATION` setting turns both endpoints off for everyone, admins included: they answer `403` before reading the index or calling a model. `true`, `null` or no value keeps them on. An invalid site configuration makes both answer `503`; see [Distribution customization](distribution-customization.md#activating-a-manifest). The other model APIs are not affected.

## Configuration

Only `RAG_API_KEY` is required. Each of these is a [stored setting](configuration.md#settings-stored-in-the-database) under **Integrations**.

| Setting | Default | Purpose |
|---|---|---|
| `RAG_API_KEY` | _(unset)_ | User API key the assistant calls the gateway with |
| `RAG_API_BASE_URL` | `http://localhost:8080/v1` | Gateway the assistant calls |
| `RAG_INDEX_PATH` | the overlay's `content/rag/docs_index.json` | Vector index |
| `RAG_CORPUS_DIR` | the overlay's `content/docs/docs/source` | Markdown corpus |
| `RAG_EMBEDDER` | `gateway` | `gateway` (a real model, `RAG_EMBED_MODEL`) or `hash` (offline) |
| `RAG_GATEWAY_BASE_URL` | `http://localhost:8080/v1` | Gateway the ingest tool embeds through |
| `RAG_GATEWAY_API_KEY` | falls back to `LOCAL_API_KEY` | User API key the ingest tool presents to `RAG_GATEWAY_BASE_URL` |
| `RAG_EMBED_MODEL` | `bge-m3` | Embedding model id (`gateway` mode) |
| `RAG_CHAT_MODEL` | `qwen3.6-35b` | Answer model. Set it to a model your gateway serves; this repository ships no such model. |
| `RAG_TOP_K` | `4` | Chunks retrieved per query |
| `RAG_MAX_TOKENS` | `1024` | Answer token budget |
| `RAG_TEMPERATURE` | `0.3` | Generation temperature |
| `RAG_CHUNK_MAX_CHARS` | `1200` | Largest chunk the ingest tool cuts, in characters |
| `RAG_CHUNK_OVERLAP_CHARS` | `150` | Characters shared by neighboring chunks |

## Limitations

- The index is rebuilt by hand (`make rag-ingest`), so it can lag behind the docs.
- There is no answer caching or reranking, and only the last six messages of history are sent with a question.

## Code map

| Path | Role |
|---|---|
| `apps/backend/serving/rag/chunker.py` | Heading-aware Markdown chunking |
| `apps/backend/serving/rag/embedder.py` | `GatewayHTTPEmbedder` and the offline `HashEmbedder` |
| `apps/backend/serving/rag/store.py` | JSON vector store and cosine search |
| `apps/backend/serving/rag/pipeline.py` | Prompt assembly and sources payload |
| `apps/backend/serving/rag/ingest.py` | `python -m serving.rag.ingest` CLI |
| `apps/backend/serving/servers/routers/rag.py` | `/v1/rag/status` and `/v1/rag/chat` |
| `apps/frontend/src/app/chat/page.tsx` | Chat UI (`/chat`, behind `ProtectedRoute`) |
| `apps/frontend/src/lib/api/chat.ts` | Streaming SSE client |
