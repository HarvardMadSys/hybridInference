# Adding a New Local Model

Use this guide for a model served by an OpenAI-compatible server you run:
vLLM, SGLang, Ollama or your own `/v1/chat/completions` service. The gateway
sends chat requests to `/v1/chat/completions`, and embedding requests to
`/v1/embeddings` for a model with `model_type: embedding`. For hosted
providers, see [Adding a New Model](adding-models.md).

## Step 1: Start the local model server

This guide registers the server as `kind: sglang`:

```bash
python -m sglang.launch_server \
  --model-path <hf-org>/<hf-model> \
  --host 0.0.0.0 \
  --port 8007 \
  --served-model-name my-local-model
```

For vLLM, run
`vllm serve <hf-org>/<hf-model> --port 8007 --served-model-name my-local-model`.
All local kinds use `OpenAICompatAdapter`; the `kind` sets the dashboard label
and provider profile, so pick the one that matches the runtime you started.

Check the server before registering it. Local servers usually need no API key:

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

A gateway in Docker reaches the host at `http://host.docker.internal:<port>`;
a gateway running directly on the host uses `http://localhost:<port>`. For a
server on another machine, see
[A server on another machine](#a-server-on-another-machine).

## Step 2: Register the model

Create the model in **Admin → Routing → Create model** with a route to the
server; it applies at once. For aliases or other metadata, add an entry to the
`models` list of the model catalog (see
[Where your model registry lives](adding-models.md#where-your-model-registry-lives))
and run `make restart s=backend` when Admin marks it **Restart required**:

```json
[
  {
    "id": "my-local-model",
    "name": "My Local Model",
    "provider": "sglang",
    "quantization": "unknown",
    "input_modalities": [
      "text"
    ],
    "output_modalities": [
      "text"
    ],
    "context_length": 65536,
    "max_output_length": 8192,
    "supports_tools": true,
    "supports_structured_output": true,
    "supported_params": [
      "temperature",
      "top_p",
      "max_tokens",
      "stop",
      "stream"
    ],
    "aliases": [
      "My-Local-Model"
    ],
    "pricing": {
      "prompt": "0",
      "completion": "0",
      "image": "0",
      "request": "0",
      "input_cache_reads": "0",
      "input_cache_writes": "0"
    },
    "route": [
      {
        "kind": "sglang",
        "weight": 1.0,
        "base_url": "http://host.docker.internal:8007",
        "provider_model_id": "my-local-model",
        "pricing": {
          "prompt": "0",
          "completion": "0"
        }
      }
    ]
  }
]
```

For a local server, check these fields:

- `id`: the name clients send as `model`. Keep it short and stable.
- `route[].kind`: `vllm`, `sglang`, `ollama` or `openai_compat`.
- `base_url`: the server root, with or without `/v1`; the adapter appends
  `/chat/completions`.
- `provider_model_id`: the name the runtime serves (`--served-model-name`, the
  Ollama tag).
- `supported_params`, `supports_tools`, `supports_structured_output`: only what
  the runtime supports.
- `aliases`: must not collide with another model's `id` or aliases.

Every field is in the
[Configuration Reference](adding-models.md#configuration-reference).

### Naming a route in the dashboard

A route's dashboard label defaults to its `kind`. Every provider-scoped admin
view (Token Usage, Provider Performance, Provider Observability, the provider
disable switch and the provider registry) groups on that label, which
`api_logs.provider` records. Two boxes both served as `kind: vllm` therefore
share one row. Give each route its own label, and optionally a display name:

```json
{
  "route": [
    {
      "kind": "vllm",
      "weight": 1.0,
      "provider": "local-a",
      "provider_display_name": "Local box A",
      "base_url": "${LOCAL_A_URL}",
      "api_key": "${LOCAL_API_KEY}"
    },
    {
      "kind": "vllm",
      "weight": 1.0,
      "provider": "local-b",
      "provider_display_name": "Local box B",
      "base_url": "${LOCAL_B_URL}",
      "api_key": "${LOCAL_API_KEY}"
    }
  ]
}
```

The dashboard then shows `Local box A · local-a` and `Local box B · local-b`
as separate providers, each with its own metrics and enable switch. Only the
label changes. The adapter, the server and the
[`endpoint_id`](glossary.md#routes-and-providers) (with its circuit breaker,
latency history and weight overrides) stay the same, and API keys stay pooled
under the kind, so one `LOCAL_API_KEY` serves both boxes.

- A label is lowercase letters, digits, `-` or `_`, at most 64 characters, and
  may not reuse a built-in provider's name (`vllm`, `zai`, `openrouter`, …),
  which would merge its traffic into that provider's reporting and disable
  switch. Admin refuses such a catalog. A stored one registers no catalog
  model, and the backend logs
  `The model catalog (MODEL_CATALOG in Admin > Providers > Models) was not registered: …`.
- `provider_display_name` alone renames a provider without splitting it.
- If a custom provider created in the Providers tab already uses the label, it
  keeps its keys and target and the backend logs the clash as an error at
  startup. Rename the label.
- Rows already written keep their old label until they age out of
  `provider_hourly_stats` after 30 days, so charts show both labels for a while.

## Step 3: Add a remote fallback (optional)

Add a second route with a small weight. With `0.01`, the fallback takes about
one request in a hundred first, plus every request that fails on the local
server. A route with weight `0` is never used, not even as a fallback.

```json
{
  "route": [
    {
      "kind": "sglang",
      "weight": 1.0,
      "base_url": "http://host.docker.internal:8007",
      "provider_model_id": "my-local-model",
      "pricing": {
        "prompt": "0",
        "completion": "0"
      }
    },
    {
      "kind": "openrouter",
      "weight": 0.01,
      "base_url": "https://openrouter.ai/api/v1",
      "api_key": "${OPENROUTER_API_KEY}",
      "provider_model_id": "<upstream-model-slug>",
      "pricing": {
        "prompt": "0",
        "completion": "0"
      }
    }
  ]
}
```

`config/examples/models.openrouter.json` has a working local-first entry
built this way.

## Step 4: Verify through the gateway

`GET /v1/models` answers without a key:

```bash
curl -s http://localhost:8080/v1/models | jq
```

`POST /v1/chat/completions` returns `401` without a gateway API key (the key
your gateway issued, not the local server's), unless the backend runs with
`USER_AUTH_ENABLED=false`:

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

## A server on another machine

Keep a model server on another machine off the public internet, and point the
route at its private address:

```json
{
  "route": [
    {
      "kind": "openai_compat",
      "weight": 1.0,
      "base_url": "http://10.0.12.34:8000/v1",
      "deployment_type": "self_hosted",
      "provider_model_id": "your-served-model-name"
    }
  ]
}
```

`deployment_type: self_hosted` marks the route as an engine you operate even
though its address is remote. It skips the provider-key concurrency limit and
lets a configured [engine queue limit](routing.md#engine-queue-limit) apply to
its streaming requests; it does not turn on queue offload by itself. Inside a
gateway container, `localhost` is the container, not the model server.

In **Admin → Routing** you can set the deployment type when you create or
repoint a route, or change it later. **Self-hosted engine** does not skip the
base URL checks. Changing the base URL or provider resets it to **Auto** unless
you choose again. If a saved override cannot be applied at startup (its target
no longer passes the base URL checks, say), the route shows
**Override not applied** until you edit it or reset its config.

You can also forward the port to the gateway host with an SSH reverse tunnel,
run on the model host:

```bash
# Run this on the INTERNAL model host
ssh -N -R 8001:127.0.0.1:8000 <user>@<gateway-host>
```

The route then targets `http://127.0.0.1:8001/v1`. Check it from the gateway
host first:

```bash
curl http://127.0.0.1:8001/v1/models | jq
```

## Serving from an HPC cluster

On a cluster behind a batch scheduler (Slurm, PBS, LSF), each allocation gives
you a different compute node for a few hours. The node is usually unreachable
from the gateway and often cannot reach a container registry, and the
allocation costs money or queue priority while you hold it. So the server
binds the node's loopback, an SSH reverse tunnel carries it to a fixed loopback
port on the gateway host, and the route names that port. A new allocation
needs a new tunnel, never a catalog change.

```text
compute node (new hostname each allocation)        gateway host
┌──────────────────────────────┐                   ┌──────────────────────────┐
│ vLLM in a container          │  ssh -R           │ 127.0.0.1:8001           │
│ published on 127.0.0.1:8000  │ ────────────────► │   ▲                      │
└──────────────────────────────┘                   │   │ base_url             │
                                                   │ gateway                  │
                                                   └──────────────────────────┘
```

Every scheduler flag, path and port below is a placeholder. Partitions, GPU
resource names, walltime limits and the container runtime differ per site.

### 1. Allocate a node

With Slurm:

```bash
salloc \
  --partition=<gpu-partition> \
  --gres=gpu:<count> \
  --cpus-per-task=<cpus> \
  --mem=<memory> \
  --time=<hh:mm:ss>
```

Note the job ID (to release the allocation) and the node name (to log in).
The node name changes with every allocation, so never put it in the catalog.

```bash
squeue --me
ssh <allocated-node>
```

### 2. Make the container image available offline

Compute nodes often have no outbound network. Pull the image where there is
network, save it to shared storage and load it on the node. Pin an exact tag or
digest, not `:latest`, so every load is the same image:

```bash
# On a host with registry access
podman pull docker.io/vllm/vllm-openai:<version>
podman save --output /path/to/shared/vllm-openai-<version>.tar \
  docker.io/vllm/vllm-openai:<version>

# On the compute node
podman load --input /path/to/shared/vllm-openai-<version>.tar
podman images
```

`docker save` and `docker load` take the same arguments.

### 3. Start the model server

```bash
podman run --rm \
  --name model-server \
  --device nvidia.com/gpu=all \
  --ipc=host \
  --publish 127.0.0.1:8000:8000 \
  --volume /path/to/model-weights:/models:Z \
  docker.io/vllm/vllm-openai:<version> \
  --model /models/<model-directory> \
  --served-model-name <served-model-name> \
  --host 0.0.0.0 \
  --port 8000 \
  --tensor-parallel-size <gpu-count> \
  --gpu-memory-utilization 0.95
```

- `--publish 127.0.0.1:8000:8000` keeps the server off the cluster network. A
  bare `-p 8000:8000` exposes it, unauthenticated, to every machine on the
  fabric.
- `--host 0.0.0.0` is the container's interface. Binding `127.0.0.1` inside the
  container would leave the published port answering nothing.
- `--tensor-parallel-size` must equal the number of GPUs in the allocation.
- `--ipc=host` gives vLLM's workers enough shared memory. Where your site
  forbids it, set a large `--shm-size`.
- `--device nvidia.com/gpu=all` is Podman's CDI syntax; Docker uses
  `--gpus all`. `:Z` relabels the volume for SELinux; drop it where SELinux is
  not enforced.
- `--served-model-name` becomes your `provider_model_id`. Without it, the model
  is named by its `--model` path.

Check it on the node:

```bash
curl -s http://127.0.0.1:8000/v1/models
```

### 4. Tunnel it to the gateway host

From the compute node, open the tunnel and record its PID:

```bash
ssh -N \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 \
  -R 127.0.0.1:8001:127.0.0.1:8000 \
  <user>@<gateway-host> &
echo $! > ~/model-tunnel.pid

# Confirm it is actually up: with ExitOnForwardFailure the ssh may already be
# gone, and the line above would have recorded a dead PID.
sleep 1 && kill -0 "$(cat ~/model-tunnel.pid)" && echo tunnel up
```

- Bind the remote end to `127.0.0.1`. `-R 0.0.0.0:8001:...` would publish an
  unauthenticated model server on every network of the gateway host; it also
  needs `GatewayPorts yes` in that host's `sshd`, which is safer left off.
- With `ExitOnForwardFailure=yes`, the tunnel fails if port 8001 is still held
  by a previous allocation's tunnel. That process runs on the gateway host, so
  clear it there:

  ```bash
  # on the gateway host
  ss -lntp 'sport = :8001'      # or: lsof -nP -iTCP:8001 -sTCP:LISTEN
  kill <the sshd/ssh pid it names>
  ```
- The tunnel ends with the allocation. `autossh`, a systemd user unit or a
  retry loop rides out network blips, but nothing survives the job ending.

Check from the gateway host, where the gateway resolves it:

```bash
curl -s http://127.0.0.1:8001/v1/models
```

### 5. Register the route

Register the model as in [Step 2](#step-2-register-the-model), with
`http://127.0.0.1:8001/v1` as `base_url` and your `--served-model-name` as
`provider_model_id`.

### 6. Release everything when you are done

Otherwise the job keeps burning walltime, the route points at a closed port,
and a stale listener on the gateway host breaks the next tunnel.

1. Disable the route in **Admin → Routing**, so traffic stops before the port
   closes.
2. On the compute node, close the tunnel and stop the server:

   ```bash
   kill "$(cat ~/model-tunnel.pid)" && rm ~/model-tunnel.pid
   podman stop model-server
   ```

3. Release the allocation: exit the `salloc` shell, or from the login node run
   `scancel <job-id>`.
4. From the gateway host, confirm the port is free. This should now fail to
   connect:

   ```bash
   curl -s --max-time 5 http://127.0.0.1:8001/v1/models
   ```

### Showing the queue in the admin console

A host that starts a model's job only when the model is requested answers
`503` with error code `model_queued` while the job waits. Interactive callers
get that answer and batch requests wait; neither counts against the endpoint's
circuit. A request that runs longer than the proxy waits for one answer gets
`504` with `upstream_timeout`. The caller sees it, but it does not count
against the circuit either, so one caller's long generations cannot shut the
model off for everyone.

To see the queue, have the proxy publish its status and give the gateway the
address in **Admin → Settings → Providers & Routing → Advanced settings**:

| Setting | Meaning |
|---|---|
| `MODEL_QUEUE_STATUS_URL` | The status address as the backend reaches it, for example `http://host.docker.internal:8010/status` |
| `MODEL_QUEUE_STATUS_API_KEY` | Bearer key for that address; empty sends `LOCAL_API_KEY` |

**Admin → Batches → Model queue** then lists each model the proxy serves, with
its state and jobs, the gateway models routed to it and the batch requests
waiting for them. A route belongs to the proxy when its `base_url` has the
status address's scheme, host and port. The gateway follows no redirect from
the address, and reports a failure on the page instead of failing requests.

The status is a JSON object with times in epoch seconds. Each model needs only
`model` (the name the proxy serves it under) and `state` (`serving`, `loading`,
`queued` or `idle`); other fields are optional, and unknown ones are ignored:

```json
{
  "cluster": "Cluster A",
  "scheduler": "slurm",
  "refreshed_at": 1800000050,
  "models": [
    {
      "model": "meta-llama/Llama-3.1-8B-Instruct",
      "state": "queued",
      "last_request_at": 1800000000,
      "walltime_seconds": 3600,
      "avoided_nodes": ["node-7"],
      "jobs": [
        {
          "job_id": "1001",
          "state": "PENDING",
          "reason": "Priority",
          "partition": "gpu_requeue",
          "submitted_at": 1799993000,
          "expected_start_at": 1800002000
        }
      ]
    }
  ],
  "failed_nodes": [
    {"node": "node-7", "jobs": ["od-llama31-8b"], "avoided_until": 1800020600}
  ]
}
```

`refreshed_at` is when the proxy last read the scheduler's queue. A running
job's `nodes`, `started_at` and `ends_at` describe it; `expected_start_at` is
the scheduler's estimate for a pending job.

## Server-specific settings

### Prioritizing decode on an sglang route

If an sglang server was started with `--enable-priority-scheduling`, set
`priority_scheduling: true` on its route. The gateway then gives each request a
`priority`, so a huge prefill is scheduled behind interactive traffic:

```json
{
  "route": [
    {
      "kind": "sglang",
      "weight": 1.0,
      "base_url": "${LOCAL_DEPLOYMENT_URL}",
      "api_keys": [
        "${LOCAL_API_KEY}"
      ],
      "priority_scheduling": true
    },
    {
      "kind": "openrouter",
      "weight": 0.01,
      "base_url": "https://openrouter.ai/api/v1",
      "api_key": "${OPENROUTER_API_KEY}"
    }
  ]
}
```

The priority comes from the estimated *uncached* prefill: the prompt size minus
the prefix this endpoint probably has cached. The tiers are interactive 20,
large 15 and elephant 0, and a client's own `priority` field is dropped. Both
`/v1/chat/completions` and `/v1/messages` set it. Models on
`router: routewise` keep the upstream default, so the flag does nothing there.
Set it only for servers started with the flag: a remote provider that validates
its request body strictly may reject the field.

### Recording a prefix-cache miss on an sglang route

sglang with `--enable-cache-report` answers a prefix-cache miss with
`"prompt_tokens_details": null` instead of `{"cached_tokens": 0}`. The gateway
stores that null as `NULL` in `api_logs.cache_read_tokens`, as for a provider
that cannot report, so measured misses drop out of hit-rate calculations.
Declare on the route that the server reports, and the null is stored as `0`:

```json
{
  "route": [
    {
      "kind": "sglang",
      "weight": 1.0,
      "base_url": "${LOCAL_DEPLOYMENT_URL}",
      "api_keys": [
        "${LOCAL_API_KEY}"
      ],
      "null_cache_details_means_miss": true
    }
  ]
}
```

**Verify before setting it.** sglang without `--enable-cache-report`, and vLLM
without `--enable-prompt-tokens-details`, send the same null on every request,
hit or miss ([vllm-project/vllm#44377](https://github.com/vllm-project/vllm/issues/44377)).
There the flag would record every request as a miss. Send the same fresh, long
prompt twice:

```bash
curl -s "$BASE_URL/chat/completions" -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' -d '{"model":"'"$MODEL"'","messages":[{"role":"user","content":"<a long, freshly generated prompt>"}],"max_tokens":1}' | jq .usage
```

The route qualifies only if the first call returns `null` and the second
`{"cached_tokens": N}` with `N > 0`. The flag is allowed only on a route, and
only as a real boolean: a quoted `"false"` is a configuration error.

### Media URLs on a self-hosted route

vLLM and SGLang download any image, video or audio URL in a request from the
machine they run on. On a self-hosted route the gateway fetches those URLs
itself and sends the engine a `data:` URL, so a caller cannot make your server
read addresses on its own network. A route is self-hosted with
`deployment_type: self_hosted`, or under `auto` when its host is a loopback or
Docker-host address. Declare it for an engine on a LAN address, which `auto`
treats as a provider API.

The gateway fetches only `http` and `https` URLs on public addresses, up to
20 MiB per file and 32 MiB per request within 30 seconds; anything else gets a
`400` that names the message and part. It needs outbound internet access and
does not use an HTTP proxy. `data:` URLs pass through, and provider routes
forward URLs unchanged.

## Troubleshooting

**Model missing from `/v1/models`.** See
[Troubleshooting](adding-models.md#troubleshooting) for the startup log lines
to check.

**Gateway cannot reach the server.**

- From Docker, use `host.docker.internal`, not `localhost`.
- A server reached from a container must listen on `0.0.0.0`, not only
  `127.0.0.1`.
- Run `curl <base_url>/v1/models` from the same environment as the backend.

**Requests fail after registration.**

- `provider_model_id` must match the name the runtime serves.
- Drop parameters the runtime rejects from `supported_params`.
- Set `supports_tools` and `supports_structured_output` only if the runtime
  supports them.
