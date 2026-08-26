# Router Tutorial

Bring up an OpenRouter-compatible gateway from a fresh clone and send it a
request. No provider account, no API key and no `.env` file are involved: the
tutorial runs against a fake upstream that ships with the repository, so every
command below works on a laptop with nothing else configured.

By the end you will have a gateway answering `/health`, `/v1/models` and
`/v1/chat/completions` — streaming and non-streaming — and you will know how
to turn the example into a distribution of your own.

Every command here is the same command CI runs on each change, so a tutorial
that stops working turns the build red.

## What you need

- Docker Engine 24+ with Compose v2. Check with `docker compose version`.
- A running Docker daemon. On macOS that is Docker Desktop or Colima
  (`colima start`). `docker info` must succeed before you continue.
- Roughly 2 GB of disk for the gateway image.
- Port 18080 free. The example deliberately avoids 8080, which a gateway you
  already run would be publishing — but if something holds 18080 too, see
  [Port already in use](#port-already-in-use).

Python, Node.js and a GPU are not required. After the image is built nothing
in this tutorial reaches the network.

## Start the gateway

```bash
git clone https://github.com/HarvardMadSys/hybridInference.git
cd hybridInference

make up DISTRIBUTION=example
```

Two containers start: `example-provider`, a deterministic OpenAI-compatible
upstream, and `backend`, the gateway itself. Postgres stays stopped and no
accounts exist, because the example sets `DB_ENABLED=false` and
`USER_AUTH_ENABLED=false`. That is what lets the first request work without a
signup step; a real deployment drops both.

The example uses its own Compose project and container names, so it cannot
disturb another HybridInference stack on the same machine.

## Verify it

```bash
make smoke DISTRIBUTION=example
```

```text
EXAMPLE_SMOKE_OK
```

The smoke client waits for startup, then checks `/health`, `/v1/models` and one
routed completion — the three calls the next section walks through by hand — plus
`/site-config`, which confirms the example's manifest is the active one rather
than some other distribution answering on the same port.

## Send the first request yourself

### Is the gateway up

```bash
curl -s localhost:18080/health
```

```json
{
    "status": "healthy",
    "routes_configured": 1,
    "database_configured": false,
    "database_connected": false
}
```

`routes_configured: 1` is the single model in the example's registry. The two
database fields are `false` by design here.

### What does it serve

```bash
curl -s localhost:18080/v1/models
```

```json
{
    "object": "list",
    "data": [
        {
            "id": "example-chat",
            "name": "Runnable Example Chat",
            "object": "model",
            "created": 1787715553,
            "owned_by": "openai_compat",
            "input_modalities": ["text"],
            "output_modalities": ["text"],
            "quantization": "bf16",
            "context_length": 4096,
            "max_output_length": 1024,
            "pricing": {
                "prompt": "0",
                "completion": "0",
                "image": "0",
                "request": "0"
            },
            "supported_sampling_parameters": [
                "max_tokens",
                "stop",
                "temperature",
                "top_p"
            ],
            "supported_features": [],
            "on_demand": false,
            "openrouter": null
        }
    ]
}
```

The id, name, limits, pricing and sampling parameters all come from
`examples/distributions/example/config/models.yaml`; the remaining fields are
defaults the gateway fills in. Editing that file and restarting changes what the
gateway advertises.

### Ask for a completion

```bash
curl -s localhost:18080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model": "example-chat", "messages": [{"role": "user", "content": "Say hello."}]}'
```

```json
{
    "id": "chatcmpl-cba6d4a4829145f9b902dc10",
    "object": "chat.completion",
    "created": 1787715582,
    "model": "example-chat",
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "RUNNABLE_EXAMPLE_OK"
            },
            "finish_reason": "stop"
        }
    ],
    "usage": {
        "prompt_tokens": 4,
        "completion_tokens": 4,
        "total_tokens": 8
    }
}
```

`RUNNABLE_EXAMPLE_OK` is the fake upstream's fixed reply, so seeing it means
the request really traversed the gateway's routing rather than short-circuiting
somewhere earlier.

Two details are worth noticing. The gateway issues its own `id` instead of
forwarding the upstream's, and it reports `model` as `example-chat` — the id it
serves — rather than `example-upstream`, the id it asked the upstream for. That
indirection is the point of a router: clients address models you define, and
you decide which provider answers.

## Stream the same request

Add `"stream": true` and read the Server-Sent Events:

```bash
curl -sN localhost:18080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model": "example-chat", "messages": [{"role": "user", "content": "Say hello."}], "stream": true}'
```

```text
data: {"id": "chatcmpl-074e4fd82ae448e7b60f5b70", "object": "chat.completion.chunk", "created": 1787715563, "model": "example-chat", "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": null}]}

data: {"id": "chatcmpl-074e4fd82ae448e7b60f5b70", "object": "chat.completion.chunk", "created": 1787715563, "model": "example-chat", "choices": [{"index": 0, "delta": {"content": "RUNNABLE_EXAMPLE_OK"}, "finish_reason": null}]}

data: {"id": "chatcmpl-074e4fd82ae448e7b60f5b70", "object": "chat.completion.chunk", "created": 1787715570, "model": "example-chat", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 11, "completion_tokens": 6, "total_tokens": 17}}

data: [DONE]
```

Every frame repeats one `id`, which is how clients group a completion's chunks,
and the stream ends with the literal `[DONE]` sentinel rather than a JSON
payload.

The bundled upstream does not report usage on streamed responses, which is the
OpenAI default, so the token counts in the final chunk are the gateway's own
estimate. They therefore differ from the exact counts the non-streaming call
returned for the same prompt.

Any OpenAI client library works against this endpoint; point its base URL at
`http://localhost:18080/v1` and give it any non-empty API key, since
authentication is disabled in the example.

## Point it at a real provider

The example's model registry reads its upstream from three environment
variables, so the same configuration can address a real OpenAI-compatible API:

```bash
EXAMPLE_UPSTREAM_BASE_URL=https://openrouter.ai/api/v1 \
EXAMPLE_UPSTREAM_API_KEY="$OPENROUTER_API_KEY" \
EXAMPLE_UPSTREAM_MODEL='<provider-model-id>' \
make up DISTRIBUTION=example
```

Clients keep asking for `example-chat`; only the route behind it changed.

`make smoke` expects the fake upstream's sentinel and will fail against a real
provider — that is intended, since a deterministic check cannot assert a real
model's words. Call `/v1/chat/completions` directly instead.

## Make it your own

The example is a directory, so copying it is how you start a real deployment:

```bash
cp -r examples/distributions/example distributions/myrouter
```

Inside it, four things decide what your gateway is:

| Path | What it controls |
| --- | --- |
| `distribution.yaml` | Identity (id, display name), site URLs, feature flags, and which config files to load |
| `config/models.yaml` | The models you serve and the provider routes behind each one |
| `config/routing.yaml` | Weights across those routes |
| `deploy/backend.env` | Backend environment: ports, database, auth, upstream credentials |

Edit the identity in `distribution.yaml`, replace the model registry with your
own providers, then start it the same way:

```bash
make up DISTRIBUTION=myrouter
make smoke DISTRIBUTION=myrouter
```

Overlays under `distributions/` are what `make up` discovers automatically when
you give it no `DISTRIBUTION` argument; the tutorial's copy lives under
`examples/distributions/` precisely so that adding it can never change which
deployment a bare `make up` selects.

## Stop it

```bash
make down DISTRIBUTION=example
```

This removes only the example's own containers and network.

## Troubleshooting

### Port already in use

```text
Bind for 127.0.0.1:18080 failed: port is already allocated
```

Find the holder with `lsof -nP -iTCP:18080 -sTCP:LISTEN`, or move the example
aside. Pass the port to both commands, since one publishes it and the other
connects to it:

```bash
BACKEND_PORT=28080 make up DISTRIBUTION=example
BACKEND_PORT=28080 make smoke DISTRIBUTION=example
```

Then read `localhost:28080` wherever this page says `localhost:18080`.

Changing the port permanently is an edit to one line — `BACKEND_PORT` in
`examples/distributions/example/deploy/backend.env`. Both Compose and the smoke
client read it from there, so neither can drift from the other.

### Cannot connect to the Docker daemon

Start it first: Docker Desktop, or `colima start` on macOS. `docker info` should
print a server section before you run `make up`.

### `make up` chose a different distribution

Running `make up` without `DISTRIBUTION` makes it discover the overlays under
`distributions/`, and it prints which one it picked. Always pass
`DISTRIBUTION=example` while following this page.

## What this example deliberately leaves out

It stops at the first routed completion. Frontend packaging, visual branding,
user accounts, API keys, quotas and request history are all real parts of the
platform, and all excluded here so the tutorial stays deterministic and fast
enough to run in CI on every change.

For those, continue with [Installation](installation.md) for a full stack,
[Configuration](configuration.md) for the settings this page skipped,
[Adding Models](adding-models.md) for the model registry in depth, and
[Routing](routing.md) for weights and failover.
