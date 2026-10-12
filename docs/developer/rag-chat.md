# Docs RAG Assistant

The docs assistant is a retrieval-augmented-generation (RAG) chat feature: it
answers users' questions about a deployment from that deployment's own **public
user docs**. Both retrieval and generation go through the gateway itself.

It is an early feature with known gaps; see [Limitations](#limitations). To
run it, a deployment needs its documentation as Markdown, an index built from
it, and an API key the assistant can call the gateway with. A checkout with no
distribution has none of these, so the assistant answers `503` until you
supply them.

## How it works

`/v1/rag/chat` is a thin orchestrator: it retrieves in-process, then calls the
gateway's **own** public API **as a user** for the model work.

```text
   POST /v1/rag/chat  (JWT-gated)
     1. embed query   ── HTTP ─►  POST {RAG_API_BASE_URL}/embeddings  (RAG_EMBED_MODEL)
     2. cosine top-k over the JSON vector index             (in-process)
     3. build grounded prompt with citations                (in-process)
     4. generate      ── HTTP ─►  POST {RAG_API_BASE_URL}/chat/completions (RAG_CHAT_MODEL)
     → SSE: sources event, then the proxied OpenAI chunks, then [DONE]
```

Both inner calls present `RAG_API_KEY`, the assistant's own API key, so they go
through the normal `/v1/embeddings` and `/v1/chat/completions` handlers: they
are logged in `api_logs` and counted toward cost, quota and concurrency like any
other request. They also carry `X-On-Behalf-Of: <user id>`, so that usage is
charged to the signed-in user who asked rather than to the shared key, and
counts against that user's own daily quota. The gateway honours the header only
on requests made with `RAG_API_KEY`, and ignores it on every other key.

- **Corpus:** the active distribution overlay's documentation source
  (`<overlay>/content/docs/docs/source/*.md`) — the same markdown that builds
  that deployment's public doc site. A checkout with no overlay has no corpus;
  set `RAG_CORPUS_DIR` to your own documentation.
- **Vector store:** a plain JSON file scanned with pure-Python cosine
  similarity; a docs corpus is small enough not to need anything more. Like the
  corpus, the index belongs to the distribution, not to this repository: its
  default path is `<overlay>/content/rag/docs_index.json`, inside whichever
  distribution the gateway runs. `RAG_INDEX_PATH` and `RAG_CORPUS_DIR` override
  these two paths.
- **Why call the gateway as a user (over HTTP) instead of the in-process
  router?** So RAG requests are observable and metered. Calling the router or
  an adapter directly would skip the per-request logging, cost, quota and
  concurrency checks that live in the `/v1/*` handlers.

## Setting it up

Set `RAG_API_KEY` to a valid user API key, and point `RAG_API_BASE_URL` at the
gateway's own address; both are settings under **Integrations** on the admin
console's Configuration tab. The default, `http://localhost:8080/v1`, matches
the port the Compose backend listens on; a deployment that binds the backend
somewhere else must set it, or every RAG request fails at the self-call.

### Rebuilding the index

Build the index with the default `gateway` embedder, which calls a real
embedding model through a gateway:

```bash
RAG_CORPUS_DIR=path/to/docs RAG_GATEWAY_API_KEY=hyi-xxx make rag-ingest
```

When the `DB_*` settings in `.env` reach the gateway's database, the indexer
reads the stored settings first, so a value stored on the Configuration tab
wins over one set on the command line. When they do not, it warns and uses the
environment alone.

`RAG_CORPUS_DIR` is only needed when your corpus is not the active overlay's
`content/docs/docs/source`; without an overlay, ingest fails with a message
telling you to set it.

`RAG_GATEWAY_BASE_URL` defaults to `http://localhost:8080/v1` — embedding is
billable work, so a clone draws on its own gateway rather than on whoever wrote
the default. Point it at the gateway you want to embed through; the key must be
a valid user API key on *that* gateway. Chunks are embedded with the same
`RAG_EMBED_MODEL` the serving endpoint uses at query time, so query and
document vectors share one space.

For an offline run with no gateway/key (weak retrieval — dev/CI only):

```bash
RAG_EMBEDDER=hash make rag-ingest
```

> The serving endpoint embeds the query with whichever embedder built the index
> (recorded in the index metadata) and **fails loud** (HTTP 502) if the query
> vector's dimension doesn't match the index. Always rebuild after switching
> embedders.

### Deploying the index

The index is a deployment artifact, not part of the image build: build it with
`make rag-ingest` and make the resulting JSON file readable at
`RAG_INDEX_PATH`. In the Compose deployment the overlay directory is mounted
into the backend, so an index written into the overlay's `content/rag/` is
picked up without an image rebuild, and replacing the file is enough.
`/v1/rag/status` reports `index_loaded`, the embedder mode, and chunk count for
a post-deploy check. If the embedding backend is unavailable at query time,
`/v1/rag/chat` returns a graceful `503` rather than a 500.

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

A request log row from the assistant has `metadata.user_agent = "doc_assistant"`.
An upstream `429` is passed through to the caller, and may mean the user's own
daily quota is used up.

```bash
curl -sN https://your-gateway.example/v1/rag/chat \
  -H "Authorization: Bearer <jwt>" -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"How do I get an API key?"}]}'
```

## Turning it off

With an active distribution manifest, `features.rag: false` turns both
endpoints off for everyone, admins included: they answer `403` before reading
the index or calling a model. `true`, `null` or leaving the field out keeps the
assistant on. A broken manifest configuration makes both endpoints answer
`503`; see [Activating a manifest](distribution-customization.md#activating-a-manifest).
The general model APIs are not affected either way.

## Configuration

Only `RAG_API_KEY` is required. The path defaults point inside the active distribution.
Each of these is a [setting](configuration.md#settings-stored-in-the-database),
under **Integrations** on the Configuration tab.

| Setting | Default | Purpose |
|---|---|---|
| `RAG_API_KEY` | _(unset)_ | User API key the handler calls the gateway with (**required** at serving time) |
| `RAG_API_BASE_URL` | `http://localhost:8080/v1` | Gateway the handler calls (self-call for logging/quota) |
| `RAG_INDEX_PATH` | the overlay's `content/rag/docs_index.json`, if one is present | Vector index location |
| `RAG_CORPUS_DIR` | the overlay's `content/docs/docs/source`, if one is present | Markdown corpus |
| `RAG_EMBEDDER` | `gateway` | `gateway` (a real embedding model, via `RAG_EMBED_MODEL`) or `hash` (offline) |
| `RAG_GATEWAY_BASE_URL` | `http://localhost:8080/v1` | Gateway used by **ingest** (gateway mode) |
| `RAG_EMBED_MODEL` | `bge-m3` | Embedding model id (gateway mode) |
| `RAG_CHAT_MODEL` | `qwen3.6-35b` | Answer-generation model. Set it to a model your gateway serves: the default names a model this repository does not ship. |
| `RAG_GATEWAY_API_KEY` | falls back to `LOCAL_API_KEY` | User API key **ingest** presents to `RAG_GATEWAY_BASE_URL`; must be valid on *that* gateway |
| `RAG_TOP_K` | `4` | Chunks retrieved per query |
| `RAG_MAX_TOKENS` | `1024` | Answer token budget |
| `RAG_TEMPERATURE` | `0.3` | Generation temperature |

## Limitations

- The index is refreshed manually (`make rag-ingest`), not on a schedule — it
  can lag the docs until regenerated.
- The `HashEmbedder` fallback exists only so the pipeline runs without the
  gateway (dev/CI); its retrieval quality is weak.
- No answer caching, no reranking, and history is truncated to the last few
  turns. The store is loaded into memory per process (cached, mtime-invalidated).

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
