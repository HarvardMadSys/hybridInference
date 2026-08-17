# Adding a New Local Model

This guide explains how to register a self-hosted model behind the HybridInference
gateway. Use this when the model is already served by a local OpenAI-compatible
server such as vLLM, SGLang, Ollama, or a custom `/v1/chat/completions` service.

For new remote providers or custom adapters, see [Adding a New Model](adding-models.md).

## Overview

Adding a local model has three parts:

1. Start the local inference server.
2. Add a `config/models.yaml` entry that points to that server.
3. Restart HybridInference and verify the public `/v1` API.

The local server must expose OpenAI-compatible endpoints. The gateway forwards
chat requests to `/v1/chat/completions` and embedding requests to `/v1/embeddings`
when the model is registered as an embedding model.

## Private Server (No Public Internet)

If your model runs on a different server that is not exposed to the public
internet, keep it private and make the gateway reach it over trusted network
paths.

Recommended options:

```yaml
    route:
      - kind: openai_compat
        weight: 1.0
        base_url: "http://10.0.12.34:8000/v1"
        provider_model_id: "your-served-model-name"
```

Example SSH reverse tunnel (internal model host -> gateway host):

```bash
# Run this on the INTERNAL model host
ssh -N -R 8001:127.0.0.1:8000 <user>@<gateway-host>
```

Then set:

```yaml
base_url: "http://127.0.0.1:8001/v1"  # resolved on the gateway host
```

For reverse-tunnel setups, verify from the gateway host:

```bash
curl http://127.0.0.1:8001/v1/models | jq
```

## Step 1: Start the Local Model Server

Start the model with your preferred serving runtime. Example with vLLM:

```bash
vllm serve Qwen/Qwen3-32B-example \
  --host 0.0.0.0 \
  --port 8007 \
  --served-model-name Qwen3-32B-example
```

Check that the local server responds before changing the gateway config:

```bash
curl http://localhost:8007/v1/models | jq
curl -s -X POST http://localhost:8007/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "Qwen3-32B-example",
    "messages": [{"role": "user", "content": "Hello"}],
    "max_tokens": 32
  }' | jq
```

If HybridInference runs in Docker, use `http://host.docker.internal:<port>` in
`config/models.yaml` so the container can reach the host. If it runs directly on
the host, `http://localhost:<port>` is fine.

## Step 2: Add the Model to `config/models.yaml`

Add a new entry under `models:`. Keep the public `id` short and stable because
clients use it in the `model` field.

```yaml
  - id: qwen3-32b-example
    name: Qwen3 32B (example)
    provider: sglang
    quantization: "unknown"
    input_modalities: ["text"]
    output_modalities: ["text"]
    context_length: 65536
    max_output_length: 8192
    supports_tools: true
    supports_structured_output: true
    supported_params: [temperature, top_p, max_tokens, stop, stream]
    aliases: ["Qwen3-32B-example"]
    pricing:
      prompt: "0"
      completion: "0"
      image: "0"
      request: "0"
      input_cache_reads: "0"
      input_cache_writes: "0"
    route:
      - kind: sglang
        weight: 1.0
        base_url: "http://host.docker.internal:8007"
        provider_model_id: "Qwen3-32B-example"
        pricing:
          prompt: "0"
          completion: "0"
```

Use these fields carefully:

- `id`: Public model ID returned by `/v1/models` and used by clients.
- `provider`: Top-level provider label for metadata. For local OpenAI-compatible
  servers, use `vllm`, `sglang`, or `openai_compat`.
- `route[].kind`: Adapter kind used by the gateway. Local OpenAI-compatible
  services can use `vllm`, `sglang`, or `openai_compat`.
- `base_url`: The local server root. It may include `/v1`, but does not have to.
- `provider_model_id`: Model name sent to the local server. This must match the
  serving runtime's model name.
- `aliases`: Optional extra public names that resolve to the same gateway model.
- `supported_params`: Only include parameters that the local runtime accepts.
- `route[].provider`: Optional label override — see
  [Naming a route in the dashboard](#naming-a-route-in-the-dashboard).
- `route[].provider_display_name`: Optional human-readable name for that label.

### Naming a Route in the Dashboard

By default a route reports its `kind` as the provider label, and that label is
what `api_logs.provider` records and what every provider-scoped admin view
groups on: Token Usage, Provider Performance, Provider Observability, the
provider disable switch, and the provider registry. Two local boxes both served
by `kind: vllm` therefore land in one row and cannot be compared.

Give each route its own label to split them, and optionally a display name:

```yaml
    route:
      - kind: vllm
        weight: 1.0
        provider: local-a
        provider_display_name: "Local box A"
        base_url: ${LOCAL_A_URL}
        api_key: ${LOCAL_API_KEY}
      - kind: vllm
        weight: 1.0
        provider: local-b
        provider_display_name: "Local box B"
        base_url: ${LOCAL_B_URL}
        api_key: ${LOCAL_API_KEY}
```

The dashboard then shows `Local box A · local-a` and `Local box B · local-b` as
separate providers, each with its own error rate, cache-hit rate, token totals,
and enable/disable switch.

Only the analytics label changes. The route still talks to the upstream its
`kind` selects, `endpoint_id` still keys on kind and port, and API keys stay
pooled under the kind — so one `LOCAL_API_KEY` continues to serve both boxes.

Rules and caveats:

- The label must be lowercase letters, numbers, dashes, or underscores (max 64
  characters), and may not borrow a built-in provider's name (`vllm`, `zai`,
  `openrouter`, …). Reusing one would fold this route's traffic into that
  provider's quota reporting and disable switch. A malformed label fails the
  config load at startup rather than silently mislabelling traffic.
- `provider_display_name` works on its own too, if you want to rename a
  provider in the dashboard without splitting it.
- A label reserves its slug against custom providers created in the Providers
  tab. If a custom provider with that slug already exists, the custom provider
  keeps its keys and route target and the clash is logged as an error at
  startup — rename the label, since otherwise both report under one provider.
- Renaming does not rewrite history. Rows already written under the old label
  keep it, so both labels appear until the old data ages out of
  `provider_hourly_stats` (30 days) — expect a gap in the new label's charts
  before the rename.
- Per-model route weight overrides key on `endpoint_id`, not the label, so a
  rename leaves them intact.

## Step 3: Add Optional Remote Fallbacks

If you want automatic fallback, add another route with a lower or equal weight:

```yaml
    route:
      - kind: sglang
        weight: 1.0
        base_url: "http://host.docker.internal:8007"
        provider_model_id: "Qwen3-32B-example"
        pricing:
          prompt: "0"
          completion: "0"
      - kind: featherless
        weight: 0
        base_url: ${FEATHERLESS_BASE_URL}
        api_key: ${FEATHERLESS_API_KEY}
        provider_model_id: "Qwen/Qwen3-32B-example"
        pricing:
          prompt: "0"
          completion: "0"
```

Set fallback `weight` to `0` when you want to keep the route configured but
disabled. Set it above `0` to allow weighted routing and failover.

## Step 4: Restart the Gateway

Restart the backend so it reloads `config/models.yaml`:

```bash
make restart s=backend
```

For local development without Docker:

```bash
uvicorn serving.servers.app:app --host 0.0.0.0 --port 8080
```

## Step 5: Verify Through HybridInference

List registered models:

```bash
curl http://localhost:8080/v1/models | jq
```

Run a chat completion through the gateway:

```bash
curl -s -X POST http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3-32b-example",
    "messages": [{"role": "user", "content": "Hello from the gateway"}],
    "max_tokens": 32
  }' | jq
```

Test streaming:

```bash
curl -N -s -X POST http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3-32b-example",
    "messages": [{"role": "user", "content": "Stream one sentence"}],
    "stream": true,
    "max_tokens": 64
  }'
```

## Routing Notes

If `config/routing.yaml` is present, it can adjust route weights after models are
registered. Without `routing.yaml`, the gateway uses the weights in
`config/models.yaml`.

### Prioritizing Decode on an sglang Route

A route whose server was started with sglang's `--enable-priority-scheduling`
can declare that, and the gateway will stamp a per-request `priority` on the
upstream body so a mega-prefill is scheduled behind interactive traffic rather
than ahead of it:

```yaml
    route:
      - kind: sglang
        weight: 1.0
        base_url: ${LOCAL_DEPLOYMENT_URL}
        api_keys:
          - ${LOCAL_API_KEY}
        priority_scheduling: true
      # The remote fallback must NOT set it — it is a fact about an sglang
      # server, not about the model.
      - kind: deepseek
        weight: 0.0
        api_key: ${DEEPSEEK_API_KEY}
```

Priority is derived from the estimated *un-cached* prefill — the prompt size
minus the prefix this endpoint is expected to have cached (interactive 20, large
15, elephant 0; see `apps/backend/routing/prefill_load.py`) — and clients cannot
set their own. Both `/v1/chat/completions` and `/v1/messages` stamp it; a model
on `router: routewise` keeps the upstream's default priority, because that
router has no prefill accounting to compute the discount from. Set it only on routes
pointing at a server launched with the flag;
the matching proxy-side config is `priority_scheduling` in
`ops/local_deployment_proxy/README.md` ("Prioritizing decode over prefill"),
which also explains the tier spacing and how it interacts with
`chunked_prefill_size`.

## Troubleshooting

### Model Does Not Appear in `/v1/models`

- Check YAML indentation under `models:`.
- Restart the backend after editing `config/models.yaml`.
- Confirm `id` and `aliases` do not collide with another model.
- Check backend logs for model registry errors.

### Gateway Cannot Reach the Local Server

- From Docker, use `host.docker.internal` instead of `localhost`.
- From bare metal, use `localhost` or the host IP.
- For private remote servers, use private IP/hostname or a private tunnel
  endpoint; avoid public internet exposure.
- Confirm the local server listens on `0.0.0.0`, not only `127.0.0.1`, if it must
  be reached from a container.
- Verify `curl <base_url>/v1/models` works from the same environment as the
  backend.

### Requests Fail After Registration

- Make sure `provider_model_id` matches the model name exposed by the local
  runtime.
- Remove unsupported request params from `supported_params`.
- If the runtime's base URL already ends in `/v1`, keep it that way; the adapter
  will use `/chat/completions` under that base.
- For tool calls or JSON output, set `supports_tools` and
  `supports_structured_output` only when the local runtime supports them.
