# Router Tutorial

Bring up an OpenAI-compatible gateway from a fresh clone and send it a request.
No provider account, no API key and no `.env` file are involved: the tutorial
runs against a fake upstream that ships with the repository, so every command
below works on a laptop with nothing else configured.

By the end you will have a gateway answering `/health`, `/v1/models` and
`/v1/chat/completions` — streaming and non-streaming — and you will know what a
distribution is made of.

CI runs these same commands whenever the example, the backend or this page
changes, so the walkthrough cannot rot unnoticed.

## What you need

- Docker Engine 24+ with Compose v2. Check with `docker compose version`.
- A running Docker daemon. On macOS that is Docker Desktop or Colima
  (`colima start`). `docker info` must succeed before you continue.
- GNU Make, and Python 3 on the host: `make smoke` runs the example's smoke
  client locally rather than in a container. It imports only the standard
  library, so no `pip install` is involved.
- Roughly 2 GB of disk for the gateway image.
- Port 18080 free. The example deliberately avoids 8080, which a gateway you
  already run would be publishing — but if something holds 18080 too, see
  [Port already in use](#port-already-in-use).

Node.js and a GPU are not required. After the image is built, nothing in this
tutorial reaches the network.

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
`distributions/example/config/models.yaml`; the remaining fields are
defaults the gateway fills in.

Edit that file and restart to change what the gateway advertises — no rebuild:

```bash
make restart s=backend DISTRIBUTION=example
```

The backend reads it through the same read-only `distributions/` mount every
deployment uses, so the file on disk is the file it serves.

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
estimate. They therefore differ from the counts the non-streaming call returned
for the same prompt, which the upstream reported itself.

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

## What a distribution is made of

A distribution is a directory under `distributions/`, and this example is one.
Everything it needs at runtime is inside it — manifest, config, Compose overlay,
its fake upstream and its smoke client — so copying the directory copies a
working shape:

```text
distributions/example/
├── EXAMPLE_OVERLAY          # marks this as a tutorial, not a deployment
├── distribution.yaml
├── config/
│   ├── models.yaml
│   └── routing.yaml
├── deploy/
│   ├── backend.env
│   └── docker-compose.yml
├── fixtures/
│   └── fake-openai-provider/
└── smoke.py
```

| Path | What it holds |
| --- | --- |
| `distribution.yaml` | Identity (id, display name), site URLs, feature flags, and which config files to load |
| `config/models.yaml` | The models you serve, the provider routes behind each one, and the `weight` on each route |
| `config/routing.yaml` | Router selection and health behaviour: `default_router`, timeouts, health-check interval, deployment lists |
| `deploy/backend.env` | Backend environment: published port, database, auth, upstream credentials |
| `fixtures/` | Fake dependencies this example invents for itself — here, the OpenAI-compatible upstream. Not part of the gateway |

Your own deployment is a sibling directory with the same shape and no
`fixtures/`, since it talks to real providers.

The example is not entirely self-contained, though: this page, the CI jobs that
run it, the export manifest that publishes it, and the tests that pin its model
id and sentinel all live outside the directory and name it. Removing the example
means removing those too.

One file separates the two kinds. `EXAMPLE_OVERLAY` marks this directory as a
teaching artifact, and that is why a bare `make up` — which otherwise discovers
the single overlay present and starts it — skips this one, and why
`make up DISTRIBUTION=example` gets a backend-only stack with no Postgres and no
accounts. A real overlay does not carry the file and gets the full stack. Always
name the one you mean: `make up DISTRIBUTION=freeinference` for a deployment,
`make up DISTRIBUTION=example` for this tutorial.

Nothing reads the file's contents; its presence is the declaration. That is
deliberate — a marker inside `deploy/backend.env` would be a dotenv, and Compose
parses those by its own rules, so an overlay could read as a teaching artifact
to one reader and a deployment to another.

Copying the example is where you start editing, not something to run unchanged.
The copy still carries this one's identity: `deploy/backend.env` points
`BACKEND_ENV_FILE` and `DISTRIBUTION_CONFIG_PATH` back at `distributions/example/`,
`deploy/docker-compose.yml` names the Compose project `hybridinference-example`
and builds the fake upstream, and `smoke.py` asserts this manifest's id, model id
and sentinel. See [Installation](installation.md) and
[Configuration](configuration.md) for what a real deployment needs.

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

To change the port permanently, edit `BACKEND_PORT` in
`distributions/example/deploy/backend.env`. Both Compose and the smoke
client read it from there, so those two cannot drift apart. Two more places
state the same port for display rather than for binding, and are worth keeping
consistent: `SITE_PUBLIC_BASE_URL` in that same file, and `site.public_base_url`
in `distribution.yaml`.

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
