# Configuration

This page explains how a running gateway finds its configuration: which files it
reads, where those files live, and which layer wins when more than one names the
same file. It is the conceptual companion to
[Adding a New Model](adding-models.md) (the field-by-field model registry reference)
and [Routing](routing.md) (what the routing engine does with the result).

For distribution manifests, branding, UI modules and backend extensions, see
[Distribution customization](distribution-customization.md).

## What the gateway reads at startup

| Kind | Holds |
|---|---|
| `models` | The model registry: every model id the gateway serves and the upstream routes behind it |
| `routing` | Optional deployment-wide settings: health probing and a local/remote weight split |
| `alerts` | Alert rules and thresholds |
| Environment | Everything secret or host-specific: credentials, database connection, feature switches |

Only the model registry is required to serve traffic. Without it the gateway
starts, serves `/health`, and answers `GET /v1/models` with an empty list.
Without a routing file each route keeps the weight the registry gives it, and
without an alerts file the built-in thresholds apply.

The files are not the whole story once a database is configured. The admin
console writes providers, keys, routes, weights and per-model overrides to the
operational store, and the gateway re-applies that state on top of the loaded
registry at every start.
[Runtime configuration from the admin console](#runtime-configuration-from-the-admin-console)
covers that layer.

## Where configuration lives

`config/examples/` contains reference gateway configuration, including
`models.openrouter.yaml` and `routing.minimal.yaml`. A deployment keeps its
model registry, routing rules and alerts in its own configuration directory,
such as `distributions/<name>/config/`, and selects them through explicit
environment variables or an active distribution manifest.
[Distribution customization](distribution-customization.md#the-distribution-manifest)
explains the manifest and overlay layout.

A provider, key, route or whole model can also be added from the admin console
while the gateway runs. That state lives in Postgres and is described
[below](#runtime-configuration-from-the-admin-console).

## How a gateway finds its config

Precedence is environment, then manifest, then built-in default.

Each kind of file is looked up on its own, in this order:

1. **An explicit environment variable.** `MODELS_CONFIG_PATH`,
   `ROUTING_CONFIG_PATH`, `ALERTS_CONFIG_PATH`. (The older names `MODELS_CONFIG`
   and `ROUTING_CONFIG` are still accepted; the canonical `*_CONFIG_PATH` name
   wins when both are set.)
2. **The distribution manifest's `paths:` section** — only when
   `DISTRIBUTION_CONFIG_MODE=active`. By default a manifest is only checked,
   not applied; see
   [Activating a manifest](distribution-customization.md#activating-a-manifest).
3. **The built-in default**, which points at the reference examples:
   `config/examples/models.openrouter.yaml` and
   `config/examples/routing.minimal.yaml`. The `alerts` default is
   `config/alerts.yaml`, a path this repository does not ship — a
   missing alerts file means "use the built-in thresholds".

Two things follow:

- An environment variable **beats the manifest**. If you set `MODELS_CONFIG_PATH`
  in a deployment that also has a manifest, the manifest's `models:` path is
  ignored, and the gateway logs
  `explicit env override ... wins over manifest value ...`. Pick one mechanism.
- Paths that come from the environment are resolved relative to the **working
  directory** of the process. Relative paths inside a manifest are resolved
  against **the manifest file's own directory**.

A path that resolves from layer 1 or 2 but does not exist is a warning, not a
failure: the gateway logs `Models config not found: <path> (source=env)` and
continues with no models registered.

## What a fresh clone does with no configuration at all

Start the gateway in a clean checkout with no `MODELS_CONFIG_PATH`, no manifest
and no overlay, and the built-in default applies: it serves
`config/examples/models.openrouter.yaml`. That file registers three models — two
straight OpenRouter routes and one that puts a local OpenAI-compatible server
first with OpenRouter as automatic fallback — and every one of them needs the
same single credential.

```bash
export OPENROUTER_API_KEY=sk-or-...
uv run uvicorn serving.servers.app:app --no-proxy-headers --port 8080
```

Without that variable every model in the file is skipped — its route's only API
key expands to empty — and the gateway says so before serving anything:

```text
No models are available: every model in config/examples/models.openrouter.yaml was
skipped because its credential is unset. Set OPENROUTER_API_KEY and restart.
/v1/models will stay empty until then, and requests will report the model as not found.
```

This is the fastest way to a gateway that routes real traffic. Replace the file
with your own registry once you know what you want to serve.

## The model registry

[Adding a New Model](adding-models.md) is the field reference. Three things belong
here because they are properties of *configuration loading* rather than of any
one field:

**A route's `kind:` picks the adapter; the model's `provider:` is its default.**
When a route omits `kind:`, the model's top-level `provider:` is used; when a
model omits `route:` entirely, a single route is built from the top-level
`provider:`, `base_url` and `api_key`. `provider` is also the label written to
`api_logs.provider` and shown in metrics, which is why a route may override it
independently with `provider:`.

**These are the built-in kinds.** A deployment can add more with a
[backend extension](backend-extensions.md); those are checked first.

| Kind | Adapter |
|---|---|
| `openai_compat`, `staging`, `vllm`, `sglang`, `ollama`, `chutes`, `featherless`, `cliproxy`, `deepseek`, `zai`, `kimi`, `minimax` | `OpenAICompatAdapter` |
| `openrouter`, `openrouter[<slug>]` | `OpenRouterAdapter` |
| `claude` | `ClaudeAdapter` |
| `gemini` | `GeminiAdapter` |
| `anthropic` | `AnthropicAdapter` |

Local inference servers have no dedicated adapter: `vllm`, `sglang` and `ollama`
are OpenAI-compatible kinds that differ only in their provider label and usage
handling. Any other kind fails registry loading with
`ValueError: Unknown adapter kind: <kind>`.

**Environment interpolation in the registry is whole-value only.** In
`models.yaml`, only `base_url`, `api_key`, `api_keys`, `provider_model_id` and a
route's `embeddings_path` are expanded, and only when the entire value is
exactly `${VAR}`. There is no `${VAR:-default}` and no embedded substitution:

```yaml
base_url: ${LOCAL_BASE_URL}                  # expanded
base_url: ${LOCAL_BASE_URL:-http://x/v1}     # NOT expanded — treated as a var
                                             #   named "LOCAL_BASE_URL:-http://x/v1",
                                             #   resolves empty, model is skipped
base_url: http://${LOCAL_HOST}/v1            # NOT expanded — the literal string,
                                             #   including "${LOCAL_HOST}", is used
```

The routing and alerts files are more permissive (see below), so do not carry
habits from one file to the other.

## The routing file

`routing.yaml` is optional, and most deployments can leave it out. Read this
section before relying on it: on a gateway with a database, which includes the
standard Docker stack, its weight split has no effect, and its health probe
never changes routing. Where traffic goes is decided by the weights in the
model registry, any weight overrides set in the admin console, and the router
described in [Routing](routing.md).

Every key, with its default:

| Key | Default | Meaning |
|---|---|---|
| `default_router` | `fixed` | The router for models that do not name one with `router:`. The weight split below also runs only when this is `fixed`. |
| `timeout` | `2` | Seconds a health probe waits for a connection. |
| `health_check` | `0` | Seconds between health probes; `0` disables probing. |
| `local_deployment` | `[]` | Endpoints treated as local. |
| `remote_deployment` | `[]` | Endpoints treated as remote. |
| `logging` | `{}` | Free-form mapping. |

Each deployment entry needs both an `endpoint:` (which must start with `http://`
or `https://`) and a non-empty `models:` list; an entry with an empty `models:`
fails validation for the whole file, and the gateway logs
`RoutingManager failed to initialize` and carries on with the registry's own
weights. An entry whose `endpoint:` expands to empty is dropped individually,
with a warning naming the orphaned models, so one unset variable cannot
invalidate every other endpoint.

```yaml
default_router: fixed
timeout: 2
health_check: 30

local_deployment:
  - endpoint: ${LOCAL_BASE_URL:-http://localhost:8000}
    models: [<model-id>]

remote_deployment:
  - endpoint: https://api.your-provider.example/v1
    models: [<model-id>]
```

**The weight split applies only to a gateway without a database.** At startup,
the gateway sorts each model's routes into local and remote: a route joins a
group when its `base_url` matches that entry's `endpoint:` exactly *and* the
model is named in that entry's `models:`. It then gives each group half the
traffic (unless the deprecated `routing_parameter.local_fraction` sets another
share), spreads that evenly within the group, and scales a model's weights to
sum to 1.0. When one group is empty for a model, the other gets everything. A
model that matches nothing keeps the weights from its `route:` list. On a
gateway with a database, routing reads the registry's weights plus any admin
overrides and never sees this split.

Health probing covers `local_deployment` endpoints only — remote providers do not
serve the gateway's `/health` path and would be marked unhealthy for it. Each
probe is a `GET` to the endpoint's origin root plus `/health`.

**The probe only reports; it never changes routing.** An endpoint that fails
every probe keeps its weight and keeps receiving traffic. Each change of state
is logged at `WARNING`, and `GET /routing` shows the latest results under
`manager_status.endpoint_health`, next to `endpoint_health_enforced: false`.
The [circuit breaker](routing.md#endpoint-health-and-circuit-breaking) is what
takes a failing endpoint out of rotation.

Unlike the model registry, this file's expander handles `${VAR}`,
`${VAR:-default}`, and variables embedded in longer strings, at any depth.

`routing_strategy:` and `routing_parameter:` are the deprecated spellings of
`default_router:` and per-model `router_params:`. They still load, and log a
deprecation warning. Per-model router selection (`router:` / `router_params:` in
the model registry) overrides `default_router` and is documented in
[Routing](routing.md).

## Settings that currently have no effect

The gateway accepts these when it loads its configuration, so setting them
does not stop it from starting, but today they do nothing — or, in the case of
the routing file's weight split, nothing on a gateway with a database.

| Setting | Where | What happens |
|---|---|---|
| `local_deployment` / `remote_deployment` weight split | routing file | Applied only on a gateway without a database; see [The routing file](#the-routing-file) |
| `health_check` probe results | routing file | Logged and reported on `GET /routing`, never used for routing |
| `router_params.local_fraction` | model registry, `router: fixed` | Accepted and validated, not read |
| `features.routers` | distribution manifest | Published in `/site-config`, does not select or restrict a router |
| `paths.mcp` | distribution manifest | Accepted, not read by any component |
| `site.terms_document`, `site.privacy_document` | distribution manifest | Accepted, not loaded into the terms page; use a [Site UI module](site-ui-modules.md) |
| `deployment.target` | distribution manifest | A label only; does not select a host or build anything |

## Runtime configuration from the admin console

The files above are read once, at startup. Everything else an operator changes
about routing goes through the admin console — or the `/admin/*` endpoints
behind it — and is written to the operational store, so it takes effect without
a restart and survives one. This is the layer to use when a model, a provider or
a key has to exist *now*, on a running gateway, without editing the overlay and
redeploying.

It needs a database. `DB_ENABLED` defaults to `true` ([Database](database.md)
covers the connection); with `DB_ENABLED=false` there is no operational store,
every endpoint below answers `500 Database not configured`, and the console
tabs that call them have nothing to write to. Every endpoint requires an
administrator's JWT or `ADMIN_TOKEN` in the `Authorization: Bearer ...` header.

### Providers tab

| What | Console | Endpoint | Stored in |
|---|---|---|---|
| Add a custom OpenAI-compatible provider, with its first key | **Overview → Add provider** | `POST /admin/provider-definitions`; `POST /admin/provider-definitions/verify` probes the upstream first, without saving | `provider_definitions` |
| Edit or remove a custom provider | **Overview → Edit provider** | `PATCH` / `DELETE /admin/provider-definitions/{provider}` | `provider_definitions` |
| Add a provider API key | **Keys → Add a new key** | `POST /admin/provider-keys`; `POST /admin/provider-keys/verify` checks it first | `provider_api_keys` |
| Disable, re-enable or delete a key | **Keys** | `POST /admin/provider-keys/{key_id}/disable`, `.../enable`, `DELETE /admin/provider-keys/{key_id}`; for a key that came from the environment, `POST /admin/provider-keys/disable-env` and `.../enable-env` | `provider_api_keys`, `disabled_provider_env_keys` |
| Reserve a key for a tier | **Keys → Reserved for** | `POST /admin/provider-keys/{key_id}/min-role`, `.../min-role-env` — see [Reserving upstream keys for a tier](routing.md#reserving-upstream-keys-for-a-tier) | `provider_api_keys.min_role`, `provider_env_key_min_roles` |
| Take a provider out of rotation | **Availability → Enabled** | `PATCH /admin/providers/{provider}/disabled`; `GET /admin/providers/routable` lists what is in the routing table | `disabled_providers` |

The registry on the Overview tab lists every provider the gateway can route to,
but only *custom* providers are editable there. A provider that code or the
model registry already owns — the built-in adapter kinds, and any `provider:`
label a route declares — is read-only in this table, and a stored definition
that reuses one of those slugs is skipped at boot rather than allowed to shadow
it. Custom providers are `openai_compat` only; a protocol the generic adapter
cannot speak needs an adapter, which is a code change
([Writing a Provider Adapter](provider-adapters.md)).

A key added here joins the same pool as the keys the registry names through
`${VAR}` and rotates with them. An environment key has no row of its own, so
the console can disable it or reserve it for a tier but cannot delete it; unset
the variable and restart for that.

### Routing tab

| What | Console | Endpoint | Stored in |
|---|---|---|---|
| Create a model that is not in the registry | **Create model** | `POST /admin/routing/provider-route-models`; `POST /admin/routing/provider-route-model-verifications` tries the route without registering it | `provider_route_candidates`, plus a strategy and required-role marker in `site_settings` |
| Add a route to an existing model | **Add provider route** | `POST /admin/routing/provider-route-candidates/{model_id}`; `.../provider-route-candidate-verifications/{model_id}` to check first; `PATCH` / `DELETE /admin/routing/provider-route-candidates/{model_id}/{route_id}` | `provider_route_candidates` |
| Point a registry route somewhere else | edit the route's **Target** | `PUT /admin/routing/provider-routes/{model_id}/{route_id}`; `.../provider-route-verifications/{model_id}/{route_id}` to check first; `DELETE` removes the override and restores the YAML route | `provider_route_configs` |
| Change a route's weight | the weight field on each route | `PUT` / `DELETE /admin/routing/weights/{model_id}/{endpoint_id}`; `GET` shows the YAML, override and effective values | `provider_weight_overrides` |
| Switch a model between `fixed` and `routewise` | the strategy selector | `PATCH /admin/routing/provider-route-strategies/{model_id}` | `site_settings` |
| Reserve a route for requests left waiting in the outbound queue or for an engine's first token | **Queue offload** | `PUT` / `DELETE /admin/routing/offload-routes/{model_id}`; `GET /admin/routing/offload-routes` lists every model's, and whether routing applies it — see [Queue-wait offload](routing.md#queue-wait-offload) | `site_settings` |
| Tune RouteWise for one model | RouteWise settings | `/admin/routewise/model-settings` — see [RouteWise](routing.md#routewise) | `site_settings` |

`GET /admin/routing/provider-routes` (or `.../{model_id}`) returns every route
the gateway is serving, each tagged `source: yaml`, `override` or `runtime`,
and is the quickest way to see what the two layers add up to.

The provider selector on this tab offers every custom provider, plus each
built-in kind that the registry, the live route table or a configured
credential already names. Which route types a provider may be added as — `on_demand`,
`quota` or `concurrency` — is the deployment's contract with that vendor and
is declared with `PROVIDER_ROUTE_TYPES` (see
[Environment variables](installation.md#environment-variables)); an unlisted provider may
use any of the three.

A model created here is a **runtime model**: it exists only in the database,
carries an id, a first route, a pricing table, a router strategy and a
`required_role` (default `admin`, so a new model stays invisible to ordinary
users until you lower it), and is restored at every start. It does not carry
the catalog metadata a registry entry declares — context length, modalities,
supported parameters, aliases — and takes `ModelConfig`'s defaults for those:
`context_length` 8192, `max_output_length` 4096, text in and out, no tool
support. When a model needs any of that, put it in the registry.

### Settings tab

| What | Console | Endpoint | Stored in |
|---|---|---|---|
| Change who can see a model | **Model Visibility** | `PATCH /admin/models/{model_id}/visibility` sets or clears a `required_role` override; `GET /admin/models/visibility` lists baseline, override and effective values | `model_visibility_overrides` |
| Exempt a model from the per-user concurrency limit | **Model Concurrency Limit** | `PATCH /admin/models/{model_id}/concurrency` | `model_concurrency_exemptions` |

### How the two layers combine

The registry is loaded first and the stored state is applied on top of it, in
this order at boot; each admin change is also applied to the running router the
moment it is saved.

1. Custom provider definitions, skipping any slug the code or registry owns.
2. Stored provider keys, seeded into each provider's pool beside the
   environment keys.
3. Per-model router strategy overrides.
4. Runtime routes. A runtime model is restored only if its creation finished;
   a stored route whose model is no longer in the registry is skipped with a
   warning, so removing a model from YAML does not bring it back through a
   leftover row.
5. Route overrides, retargeting the registry routes they name.
6. Key tier reservations, re-read now that every provider is known; then
   weight overrides, disabled providers, offload routes and model visibility,
   loaded into resolvers the router consults at request time.

Two rules follow from that order. A stored change never edits the file it
overrides: delete the override and the registry route, weight or `router:`
value is back, and a runtime candidate sits beside the registry's routes rather
than replacing them. And the file still wins for anything the store has no row
for, so a registry edit plus restart is how catalog metadata, aliases and new
adapter kinds change.

## Environment variables

The gateway reads a `.env` file from its working directory and the process
environment, case-insensitively; the process environment wins. `.env.example`
in the repository root is the annotated list — copy it to `.env` and edit.
[Installation](installation.md#environment-variables) lists the variables you
are most likely to set. Secrets belong here and only here: not in the model
registry (reference them as `${VAR}`), not in the manifest.

## Running with your configuration

```bash
uv run uvicorn serving.servers.app:app --no-proxy-headers --port 8080
```

Then check what actually loaded:

```bash
curl -s localhost:8080/health           # includes routes_configured
curl -s localhost:8080/v1/models        # generated from the registered adapters
```

The startup log says what was loaded. `Registered N routes from <path>` names
the registry file that was used; `[distribution dark mode] ...` lines show what
a manifest would change once activated; `Skipping model '<id>' ... after env
expansion` names each model whose credentials were unset.

```{warning}
`GET /routing` requires no credentials and returns, for every published route,
the upstream `base_url`, its provider label and its weight — that is, your
complete upstream topology including any host and port embedded in a base URL.
Block `/routing` at your reverse proxy on any gateway reachable from the
internet unless you intend to publish this topology. `GET /admin/routing`
additionally reports unpublished routes and requires an administrator's JWT
or `ADMIN_TOKEN` in the `Authorization: Bearer ...` header. Treat both endpoints'
output as sensitive.
```
