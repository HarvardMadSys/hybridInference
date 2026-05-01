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

Example SSH reverse tunnel (internal host -> FreeInference host):

```bash
# Run this on the INTERNAL model host
ssh -N -R 8001:127.0.0.1:8000 jason@freeinference.org
```

Then set:

```yaml
base_url: "http://127.0.0.1:8001/v1"  # resolved on the FreeInference host
```

For reverse-tunnel setups, verify from the FreeInference side:

```bash
curl http://127.0.0.1:8001/v1/models | jq
```

## Step 1: Start the Local Model Server

Start the model with your preferred serving runtime. Example with vLLM:

```bash
vllm serve Qwen/Qwen3.5-27B \
  --host 0.0.0.0 \
  --port 8007 \
  --served-model-name Qwen3.5-27B
```

Check that the local server responds before changing the gateway config:

```bash
curl http://localhost:8007/v1/models | jq
curl -s -X POST http://localhost:8007/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "Qwen3.5-27B",
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
  - id: qwen3.5-27b
    name: Qwen3.5 27B
    provider: sglang
    quantization: "unknown"
    input_modalities: ["text"]
    output_modalities: ["text"]
    context_length: 65536
    max_output_length: 8192
    supports_tools: true
    supports_structured_output: true
    supported_params: [temperature, top_p, max_tokens, stop, stream]
    aliases: ["Qwen3.5-27B"]
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
        provider_model_id: "Qwen3.5-27B"
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

## Step 3: Add Optional Remote Fallbacks

If you want automatic fallback, add another route with a lower or equal weight:

```yaml
    route:
      - kind: sglang
        weight: 1.0
        base_url: "http://host.docker.internal:8007"
        provider_model_id: "Qwen3.5-27B"
        pricing:
          prompt: "0"
          completion: "0"
      - kind: featherless
        weight: 0
        base_url: ${FEATHERLESS_BASE_URL}
        api_key: ${FEATHERLESS_API_KEY}
        provider_model_id: "Qwen/Qwen3.5-27B"
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
    "model": "qwen3.5-27b",
    "messages": [{"role": "user", "content": "Hello from the gateway"}],
    "max_tokens": 32
  }' | jq
```

Test streaming:

```bash
curl -N -s -X POST http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3.5-27b",
    "messages": [{"role": "user", "content": "Stream one sentence"}],
    "stream": true,
    "max_tokens": 64
  }'
```

## Routing Notes

If `config/routing.yaml` is present, it can adjust route weights after models are
registered. Without `routing.yaml`, the gateway uses the weights in
`config/models.yaml`.

When `OFFLOAD=1`, startup removes local adapters whose `base_url` matches
`LOCAL_BASE_URL`. Use this for remote-only incidents, and make sure any local
route you want offloaded uses the same base URL value as `LOCAL_BASE_URL`.

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
