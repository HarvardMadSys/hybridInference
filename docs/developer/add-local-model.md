# Adding a New Local Model

This guide explains how to register a self-hosted model behind the
HybridInference gateway. Use it when the model is already served by a local
OpenAI-compatible server such as vLLM, SGLang, Ollama, or your own
`/v1/chat/completions` service.

For remote providers or custom adapters, see
[Adding a New Model](adding-models.md).

## Overview

Adding a local model has three parts:

1. Start the local inference server.
2. Add an entry to your model registry that points at that server.
3. Restart the gateway and verify through the public `/v1` API.

The local server must expose OpenAI-compatible endpoints. The gateway forwards
chat requests to `/v1/chat/completions`, and embedding requests to
`/v1/embeddings` when the model is registered with `model_type: embedding`.

**Where your model registry lives.** There is no `config/models.yaml` in this
repository. The backend resolves the registry path in
`apps/backend/serving/config/distribution.py` with the precedence
`MODELS_CONFIG_PATH` → a distribution manifest's `paths.models` (only when
`DISTRIBUTION_CONFIG_PATH` names one *and* `DISTRIBUTION_CONFIG_MODE=active`) →
the bundled `config/examples/models.openrouter.yaml`. See
[Where your model registry lives](adding-models.md#where-your-model-registry-lives)
for the full rules and the two layouts a self-hoster can pick. Below, "your
model registry" means whichever file that resolution picks.

## Private Server (No Public Internet)

If your model runs on a different machine that is not exposed to the public
internet, keep it private and let the gateway reach it over a trusted network
path.

Point the route at the private address:

```yaml
    route:
      - kind: openai_compat
        weight: 1.0
        base_url: "http://10.0.12.34:8000/v1"
        provider_model_id: "your-served-model-name"
```

Or forward the port to the gateway host with an SSH reverse tunnel:

```bash
# Run this on the INTERNAL model host
ssh -N -R 8001:127.0.0.1:8000 <user>@<gateway-host>
```

Then the route target is a loopback address on the gateway host:

```yaml
base_url: "http://127.0.0.1:8001/v1"  # resolved on the gateway host
```

For reverse-tunnel setups, verify from the gateway host before changing any
gateway config:

```bash
curl http://127.0.0.1:8001/v1/models | jq
```

## Step 1: Start the Local Model Server

Start the model with your preferred serving runtime. The rest of this guide
registers the server as `kind: sglang`, so the example starts one:

```bash
python -m sglang.launch_server \
  --model-path <hf-org>/<hf-model> \
  --host 0.0.0.0 \
  --port 8007 \
  --served-model-name my-local-model
```

vLLM and Ollama work the same way — `vllm serve <hf-org>/<hf-model> --port 8007`
is the equivalent command. All three dispatch to the same `OpenAICompatAdapter`;
the `kind` you register selects the metrics label and the provider profile, so
use the one that matches the runtime you actually started.

Check that the local server responds before touching the gateway config:

```bash
curl http://localhost:8007/v1/models | jq
curl -s -X POST http://localhost:8007/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "my-local-model",
    "messages": [{"role": "user", "content": "Hello"}],
    "max_tokens": 32
  }' | jq
```

Local inference servers usually do not require an API key, which is why these
two calls carry no `Authorization` header. The gateway's own API does — see
[Step 5](#step-5-verify-through-the-gateway).

If the gateway runs in Docker, use `http://host.docker.internal:<port>` in your
registry so the container can reach the host. If it runs directly on the host,
`http://localhost:<port>` is fine.

## Step 2: Add the Model to Your Registry

Add a new entry under `models:`. Keep the public `id` short and stable, because
clients send it in the `model` field.

```yaml
  - id: my-local-model
    name: My Local Model
    provider: sglang
    quantization: "unknown"
    input_modalities: ["text"]
    output_modalities: ["text"]
    context_length: 65536
    max_output_length: 8192
    supports_tools: true
    supports_structured_output: true
    supported_params: [temperature, top_p, max_tokens, stop, stream]
    aliases: ["My-Local-Model"]
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
        provider_model_id: "my-local-model"
        pricing:
          prompt: "0"
          completion: "0"
```

Use these fields carefully:

- `id`: Public model ID returned by `/v1/models` and used by clients.
- `provider`: Top-level provider label for metadata, and the default
  `route[].kind` when no `route:` list is given. For local OpenAI-compatible
  servers, use `vllm`, `sglang`, `ollama`, or `openai_compat`.
- `route[].kind`: Adapter kind used by the gateway. Local OpenAI-compatible
  services can use `vllm`, `sglang`, `ollama`, or `openai_compat`.
- `base_url`: The local server root. It may include `/v1`, but does not have to.
- `provider_model_id`: Model name sent to the local server. It must match the
  serving runtime's model name (vLLM's `--served-model-name`, the Ollama tag,
  and so on).
- `aliases`: Optional extra public names that resolve to the same gateway model.
  They must not collide with another model's `id` or aliases — a duplicate
  resolves to whichever model loads last, and the backend logs a warning.
- `supported_params`: Only include parameters the local runtime accepts.
- `route[].provider`: Optional analytics label override — see
  [Naming a Route in the Dashboard](#naming-a-route-in-the-dashboard).
- `route[].provider_display_name`: Optional human-readable name for that label.

The full field reference, including everything a route entry accepts, is in
[Adding a New Model](adding-models.md#configuration-reference).

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
`kind` selects, `endpoint_id` is still derived from the model id, the route's
`kind`, and its base URL (`_make_provider_id` in
`apps/backend/serving/servers/registry.py`), and API keys stay
pooled under the kind — so one `LOCAL_API_KEY` continues to serve both boxes.

Rules and caveats:

- The label must be lowercase letters, numbers, dashes, or underscores (max 64
  characters), and may not borrow a built-in provider's name (`vllm`, `zai`,
  `openrouter`, …). Reusing one would fold this route's traffic into that
  provider's quota reporting and disable switch. A malformed or reserved label
  raises during the registry load, so the backend comes up with an incomplete
  model list rather than silently mislabelling traffic. Note the line is
  `Failed to load models.yaml` at **WARNING** level (`bootstrap.py`, inside a
  broad `except Exception`) — grepping for an error will not find it, unlike the
  missing-registry case, which logs at ERROR.
- `provider_display_name` works on its own too, if you want to rename a provider
  in the dashboard without splitting it.
- A label reserves its slug against custom providers created in the Providers
  tab. If a custom provider with that slug already exists, the custom provider
  keeps its keys and route target and the clash is logged as an error at
  startup — rename the label, since otherwise both report under one provider.
- Renaming does not rewrite history. Rows already written under the old label
  keep it, so both labels appear until the old data ages out of
  `provider_hourly_stats` (purged at 30 days) — expect a gap in the new label's
  charts right after the rename.
- Per-model route weight overrides key on `endpoint_id`, not the label, so a
  rename leaves them intact.

## Step 3: Add Optional Remote Fallbacks

For automatic fallback, add another route with a lower or equal weight:

```yaml
    route:
      - kind: sglang
        weight: 1.0
        base_url: "http://host.docker.internal:8007"
        provider_model_id: "my-local-model"
        pricing:
          prompt: "0"
          completion: "0"
      - kind: openrouter
        weight: 0
        base_url: https://openrouter.ai/api/v1
        api_key: ${OPENROUTER_API_KEY}
        provider_model_id: "<upstream-model-slug>"
        pricing:
          prompt: "0"
          completion: "0"
```

Set a fallback's `weight` to `0` to keep the route configured but unselected.
Set it above `0` to allow weighted routing and failover.
`config/examples/models.openrouter.yaml` ships a working local-first hybrid
entry built exactly this way.

## Step 4: Restart the Gateway

Restart the backend so it reloads the registry. With the bundled Docker Compose
setup:

```bash
make restart s=backend
```

For local development without Docker, start it directly:

```bash
PYTHONPATH=apps/backend \
  MODELS_CONFIG_PATH=/path/to/your/models.yaml \
  uv run uvicorn serving.servers.app:app --port 8080
```

At startup the backend logs `Registered N routes from <path>`. That line is the
fastest way to confirm which registry file it actually loaded.

## Step 5: Verify Through the Gateway

List registered models. `GET /v1/models` accepts an anonymous request — it
resolves an API key only to decide whether to include admin-visible entries:

```bash
curl -s http://localhost:8080/v1/models | jq
```

`POST /v1/chat/completions` goes through `verify_api_key`, so **it returns 401
without a valid gateway API key** unless the backend runs with
`USER_AUTH_ENABLED=false`. This is the key issued by your gateway, not the local
server's:

```bash
export GATEWAY_API_KEY=<your gateway API key>

curl -s -X POST http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer $GATEWAY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "my-local-model",
    "messages": [{"role": "user", "content": "Hello from the gateway"}],
    "max_tokens": 32
  }' | jq
```

Test streaming:

```bash
curl -N -s -X POST http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer $GATEWAY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "my-local-model",
    "messages": [{"role": "user", "content": "Stream one sentence"}],
    "stream": true,
    "max_tokens": 64
  }'
```

## Routing Notes

When a routing config file is present, `RoutingManager` can adjust route weights
after models are registered. Its path resolves the same way the registry does —
`ROUTING_CONFIG_PATH`, then a manifest's `paths.routing`, then
`config/examples/routing.minimal.yaml`. Without one, the gateway uses the weights
written in the model registry. See [Routing](routing.md).

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
      # A remote fallback must NOT set it — it is a fact about an sglang
      # server, not about the model.
      - kind: openrouter
        weight: 0.0
        base_url: https://openrouter.ai/api/v1
        api_key: ${OPENROUTER_API_KEY}
```

Priority is derived from the estimated *un-cached* prefill — the prompt size
minus the prefix this endpoint is expected to have cached. The three tiers
default to interactive 20, large 15, elephant 0
(`apps/backend/routing/prefill_load.py`), and clients cannot set their own:
a `priority` field in a client request body is dropped by `validate_params`.
Both `/v1/chat/completions` and `/v1/messages` stamp it. A model on
`router: routewise` keeps the upstream's default priority instead, because that
router has no prefill accounting to compute the discount from — so
`priority_scheduling: true` is inert there.

Set it only on routes pointing at a server launched with the flag. A server
without it ignores the field, but a remote provider that validates its request
body strictly would not.

### Recording a Prefix-Cache Miss on an sglang Route

An sglang server started with `--enable-cache-report` answers a prefix-cache
**miss** with `"prompt_tokens_details": null` rather than
`{"cached_tokens": 0}`. Left alone, the gateway reads that null as "this
provider says nothing about caching" and stores `NULL` in
`api_logs.cache_read_tokens` — the same value it stores for a provider that
cannot report at all, so a measured miss disappears from any hit-rate
denominator computed off that column.

A route can declare that its server does report, which turns the null into the
0 it means:

```yaml
    route:
      - kind: sglang
        weight: 1.0
        base_url: ${LOCAL_DEPLOYMENT_URL}
        api_keys:
          - ${LOCAL_API_KEY}
        null_cache_details_means_miss: true
```

**Verify before setting it.** The null is ambiguous: sglang *without*
`--enable-cache-report`, and vLLM without `--enable-prompt-tokens-details`,
send the identical null on every request — hit or miss
([vllm-project/vllm#44377](https://github.com/vllm-project/vllm/issues/44377)).
Declaring the flag there would replace an honest `NULL` with a fabricated
"measured miss", which is harder to notice later than the missing value it
replaces. The check is one cold request and one warm repeat of the same prompt
against the endpoint:

```bash
curl -s "$BASE_URL/chat/completions" -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' -d '{"model":"'"$MODEL"'","messages":[{"role":"user","content":"<a long, freshly generated prompt>"}],"max_tokens":1}' | jq .usage
```

Run it twice. The route qualifies only if the second call returns
`{"cached_tokens": N}` with `N > 0` while the first returned `null`. If both
return `null`, the server is not reporting — leave the flag off.

Two loader rules keep a wrong declaration from passing quietly:

- **Route-level only.** Declaring it on the model (including a shorthand model
  with no `route:` block) is a config error, because the claim is about one
  server's startup flags and inheritance would carry it to every fallback.
- **Real YAML booleans only.** A quoted `"false"` is a config error rather than
  a surprise opt-in, since `bool("false")` is `True`.

What the client sees differs by surface: a non-streaming response carries
`cache_read_tokens: 0` (the `Usage` response model drops the rest), while a
streaming final chunk also carries `cached_tokens: 0` and
`prompt_tokens_details: {"cached_tokens": 0}`. `/v1/messages` reports it as
`cache_read_input_tokens: 0`.

## Troubleshooting

### Model Does Not Appear in `/v1/models`

- Confirm which registry the backend loaded, from the
  `Registered N routes from <path>` startup log line. If no registry is found at
  the resolved path, it logs an error naming that path, `/v1/models` is empty,
  and every request reports the model as not found.
- Check YAML indentation under `models:`.
- Restart the backend after editing the registry.
- Confirm `id` and `aliases` do not collide with another model.
- A route whose `${VAR}`-backed `api_key`, `api_keys`, or `base_url` resolves to
  empty drops the whole model; the log names the skipped models and the unset
  variables. Mark such a route `optional: true` to skip only that route.

### Gateway Cannot Reach the Local Server

- From Docker, use `host.docker.internal` instead of `localhost`.
- From bare metal, use `localhost` or the host IP.
- For private remote servers, use a private IP/hostname or a private tunnel
  endpoint; avoid public internet exposure.
- Confirm the local server listens on `0.0.0.0`, not only `127.0.0.1`, if it has
  to be reached from a container.
- Verify `curl <base_url>/v1/models` works from the same environment as the
  backend.

### Requests Fail After Registration

- Make sure `provider_model_id` matches the model name exposed by the local
  runtime.
- Remove unsupported request params from `supported_params`.
- If the runtime's base URL already ends in `/v1`, keep it that way; the adapter
  appends `/chat/completions` under that base.
- Set `supports_tools` and `supports_structured_output` only when the local
  runtime actually supports them.

## See Also

- [Adding a New Model](adding-models.md) — the full field reference, adapter
  kinds, and how to integrate a new remote provider
- [Quickstart](router-tutorial.md) — a runnable deployment that ends by
  pointing at your own local vLLM/SGLang/Ollama server
- [Routing](routing.md) — weights, health checks, and strategies
