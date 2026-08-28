# Routing through OpenRouter

[OpenRouter](https://openrouter.ai) is one of the providers HybridInference can
route to, and it is the one a fresh checkout uses by default. This page covers
the OpenRouter adapter specifically. For how routing works in general, see
[Architecture](architecture.md); for the model-registry syntax, see
[Adding models](adding-models.md).

## The default catalogue

With no environment variables and no deployment overlay, config resolution falls
through to `config/examples/models.openrouter.yaml`, which registers three
models against OpenRouter — two direct and one demonstrating a local-first
hybrid route with OpenRouter as the fallback leg. Supplying one API key is
enough to get a working gateway:

```bash
source .venv/bin/activate            # the env `make setup-dev` creates
export OPENROUTER_API_KEY=sk-or-...
uvicorn serving.servers.app:app --host 127.0.0.1 --port 8080
```

Run it from the repository root: the default paths are relative, and the boot
log confirms which registry was chosen with a line like
`Registered 3 routes from config/examples/models.openrouter.yaml`.

```bash
curl -s --noproxy '*' http://127.0.0.1:8080/routing
```

The catalogue is a starting point, not a fixture: OpenRouter's model slugs move,
and the pricing figures in that file are approximate and used only for the
gateway's own usage accounting. Copy it and edit freely.

## Route syntax

An OpenRouter route is an entry in a model's `route:` list. There are two forms
of `kind:`.

### `kind: openrouter`

OpenRouter picks the upstream provider itself, under its own default policy.

```yaml
- kind: openrouter
  weight: 1.0
  base_url: https://openrouter.ai/api/v1
  api_keys:
    - ${OPENROUTER_API_KEY}
  provider_model_id: meta-llama/llama-3.3-70b-instruct
```

`provider_model_id` is OpenRouter's own slug for the model. The `id:` your
clients ask for is the gateway's; the two are independent on purpose.

### `kind: openrouter[<slug>]`

Pins every request on that leg to one OpenRouter upstream, by sending
`provider: {order: [<slug>], allow_fallbacks: false}`.

```yaml
- kind: openrouter[deepinfra]
  weight: 1.0
  base_url: https://openrouter.ai/api/v1
  api_keys:
    - ${OPENROUTER_API_KEY}
  provider_model_id: meta-llama/llama-3.3-70b-instruct
```

The slug must match `[A-Za-z0-9_.-]+`, optionally with `/`-separated segments
(`deepinfra`, `deepinfra/turbo`). A malformed bracket form — empty pin,
whitespace, nested or unmatched brackets — raises at registration rather than
being silently ignored. OpenRouter's list of provider slugs is at
<https://openrouter.ai/docs/features/provider-routing>.

Two legs that share a `base_url` but pin different upstreams still get distinct
`endpoint_id`s, because the bracketed kind survives into the identifier. Their
circuit-breaker and availability state therefore stay isolated: one flaky
upstream does not take the other out. In `api_logs`, though, both forms record
`provider = "openrouter"`, so analytics sees a single OpenRouter cohort.

### Sort policy

An OpenRouter route created through the admin provider-routes API may carry an
`openrouter_sort` policy of `price`, `throughput`, or `latency`, which is sent
as `provider: {sort: <policy>}`. It applies only to *unpinned* routes — a route
with a bracket-form pin sends `order` instead, and the pin wins. The API
rejects the field with `422` on a non-OpenRouter route or an unrecognised value.

## What the adapter sends

`OpenRouterAdapter` (`apps/backend/serving/adapters/openrouter.py`) is a thin
subclass of the generic OpenAI-compatible adapter. On top of the normal request
it adds:

- **Attribution headers.** `X-Title` carries the site name and `HTTP-Referer`
  the site's public base URL, both resolved from the site identity
  (`SITE_NAME` / `SITE_PUBLIC_BASE_URL`, else the active distribution manifest,
  else the neutral default). OpenRouter attributes traffic to whoever these
  name, for leaderboard placement and free-tier limits. A header with no value
  is omitted rather than sent blank, so an undeclared `SITE_PUBLIC_BASE_URL`
  means no `HTTP-Referer` at all; `X-Title` always goes out, falling back to the
  literal `HybridInference` when no site name is set. Declare `SITE_NAME` if you
  want your own account credited.
- **`usage: {include: true}`** on every request, so OpenRouter returns its
  per-request `cost` field.
- **`stream_options: {include_usage: true}`** on streaming requests, so the
  final SSE chunk carries the usage block.
- **`provider: {...}`** for the pinned and sort cases described above.

## Cost accounting

Two numbers, deliberately kept apart:

| Column in `api_logs` | Meaning |
| --- | --- |
| `cost_usd` | What the caller is charged: tokens × the pricing declared for the model in your registry. Unaffected by OpenRouter |
| `upstream_cost_usd` | What OpenRouter reported it charged you for that request |

`upstream_cost_usd` is `NULL` for non-OpenRouter routes, and `NULL` for an
OpenRouter route when the response carried no cost figure.

## API keys

A route's `api_keys:` is a list. With a single entry — the usual case — the
adapter uses it directly. With several, the inherited key-pool logic rotates:
a key that hits a key-specific or transient failure (429, 401/402/403, 408/425,
5xx, or a timeout) is muted for five minutes and the request advances to the
next key. Request-scoped failures such as `400` and `422` fail identically on
every key, so they propagate immediately instead of burning the pool.

An `api_keys:` entry that expands from an unset environment variable is treated
as absent. A route marked `optional: true` is then skipped with a warning naming
the model and kind; any other route raises `MissingEnvBackedKeyError` and
startup fails. Either way a route is never registered pointing at an endpoint it
cannot authenticate to.

## Error handling

OpenRouter errors travel the same path as any other OpenAI-compatible
provider's. Two mechanisms handle them, and they are independent:

**Fallback.** When a request to an OpenRouter leg fails for any reason, the
router records the failure and tries the model's remaining legs in route order,
skipping any that are admin-disabled, modality-incompatible, or circuit-open.
The exception is a request the caller explicitly pinned with `X-Route-Pin`,
which never falls back. If every leg fails, the first error is what the client
sees. Failure status codes do not select between "fall back" and "propagate" —
that decision is only about whether another leg is available.

**The circuit breaker.** Failures accumulate per `endpoint_id`; after
`CIRCUIT_FAILURE_THRESHOLD` consecutive failures (default 3) the endpoint stops
receiving traffic for `CIRCUIT_COOLDOWN_SECONDS` (default 30) before a half-open
probe. Client errors are exempt: a 4xx other than 408, 429, 401, and 407 is the
caller's request being wrong, and letting it open the circuit would take the
endpoint away from everyone else. 408 and 429 mean OpenRouter is overloaded and
do count; 401 and 407 mean *your* `OPENROUTER_API_KEY` was rejected, which no
user can work around, so they count and additionally page.

## Troubleshooting

**Every OpenRouter request returns 401.** The gateway's key was rejected, not
the caller's. Check `OPENROUTER_API_KEY` in the environment the backend process
actually sees. A key that is unset entirely never gets that far — the route is
skipped or startup fails, as above — so a 401 means a key was present and
OpenRouter refused it.

**The model is missing from `/v1/models`.** Confirm the registry the gateway
loaded: the boot log emits `Registered N routes from <path>`, which is the file
the resolution chain chose. If it is not the file you edited, an explicit
`MODELS_CONFIG_PATH` or a distribution manifest is winning over it — see
[Configuration](configuration.md).

**Requests reach the wrong upstream.** Use `GET /routing` to see the effective
weight distribution per model. Note that this endpoint is unauthenticated and
discloses upstream base URLs; see the warning in
[Architecture](architecture.md#http-surface).
