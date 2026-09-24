# Configuration

This page explains how a running gateway finds its configuration: which files it
reads, where those files live, and which layer wins when more than one names the
same file. It is the conceptual companion to
[Adding a New Model](adding-models.md) (the field-by-field model registry reference)
and [Routing](routing.md) (what the routing engine does with the result).

For distribution manifests, branding, UI modules and backend extensions, see
[Distribution customization](distribution-customization.md).

## What the gateway reads at startup

| Kind | Holds | Read by |
|---|---|---|
| `models` | The model registry: every model id the gateway serves and the upstream routes behind it | `register_from_models_yaml` in `apps/backend/serving/servers/registry.py` |
| `routing` | Deployment-wide routing behaviour: local/remote split, health probing | `RoutingManager` in `apps/backend/routing/manager.py` |
| `alerts` | Alert rules and thresholds | `load_alert_config` in `apps/backend/serving/observability/alert_config.py` |
| Environment | Everything secret or host-specific: credentials, database connection, feature switches | `Settings` in `apps/backend/serving/config/settings.py` |

Only the model registry is load-bearing. Without it the gateway starts, serves
`/health`, and answers `GET /v1/models` with an empty list. Without a routing
file the registry's own per-route weights stand — which is also what a fresh
clone gets, because the built-in default `config/examples/routing.minimal.yaml`
declares empty endpoint pools rather than overriding anything. Without an alerts
file the built-in thresholds apply.

A fourth kind, `mcp`, is accepted by the resolver and by the distribution
manifest schema, but nothing at this revision reads it.

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

`resolve_config_path()` in `apps/backend/serving/config/distribution.py` resolves
each config kind independently, in this order:

1. **An explicit environment variable.** `MODELS_CONFIG_PATH`,
   `ROUTING_CONFIG_PATH`, `ALERTS_CONFIG_PATH`. (The older names `MODELS_CONFIG`
   and `ROUTING_CONFIG` are still accepted; the canonical `*_CONFIG_PATH` name
   wins when both are set.) `ALERTS_CONFIG_PATH` counts as an override only when
   you actually set it: it has a non-empty built-in default, so the resolver
   tracks whether you supplied the value rather than testing it against `""`.
2. **The distribution manifest's `paths:` section** — only when
   `DISTRIBUTION_CONFIG_MODE=active`; see the
   [manifest reference](distribution-customization.md#the-distribution-manifest).
3. **The built-in default**, which points at the reference examples:
   `config/examples/models.openrouter.yaml` and
   `config/examples/routing.minimal.yaml`. The `alerts` default is
   `config/alerts.yaml`, a path this repository deliberately does not ship — a
   missing alerts file means "use the built-in thresholds".

Two consequences worth internalising:

- An environment variable **beats the manifest**. If you set `MODELS_CONFIG_PATH`
  in a deployment that also has a manifest, the manifest's `models:` path becomes
  decorative. The gateway logs this rather than hiding it
  (`explicit env override ... wins over manifest value ...`). Pick one mechanism.
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
Each entry under `route:` names a `kind:`, and `kind` is what
`registry._make_adapter` dispatches on. When a route omits `kind:`, the model's
top-level `provider:` is used; when a model omits `route:` entirely, a single
route is synthesised from the top-level `provider:`, `base_url` and `api_key`.
`provider` is also the label written to `api_logs.provider` and shown in metrics,
which is why a route may override it independently with `provider:`.

**Adapter construction is centralized.** `_make_adapter`
(`apps/backend/serving/servers/registry.py`) checks registered extension factories
first, then dispatches these built-in kinds:

| Kind | Adapter |
|---|---|
| `openai_compat`, `staging`, `vllm`, `sglang`, `ollama`, `chutes`, `featherless`, `cliproxy`, `deepseek`, `zai`, `kimi`, `minimax` | `OpenAICompatAdapter` |
| `openrouter`, `openrouter[<slug>]` | `OpenRouterAdapter` |
| `claude` | `ClaudeAdapter` |
| `gemini` | `GeminiAdapter` |
| `anthropic` | `AnthropicAdapter` |

Local inference servers have no dedicated adapter: `vllm`, `sglang` and `ollama`
are OpenAI-compatible kinds that differ only in their provider label and usage
handling. A kind that is neither built-in nor explicitly registered by an
extension raises `ValueError: Unknown adapter kind: <kind>` during registry
loading. To re-derive the built-in list from the code:

```bash
sed -n '/def _make_adapter/,/Unknown adapter kind/p' apps/backend/serving/servers/registry.py
```

A `grep` for `if kind` misses most of it: the OpenAI-compat arm is a single
`if kind in (` followed by the names on their own lines, so none of them
appear in the output.

The neighbouring `RESERVED_PROVIDER_LABELS` set in the same module is a
different, larger list — the labels a route may not borrow as a custom
`provider:` — and includes registered extension kinds and names such as `openai`
and `router` that are *not* adapter kinds.

**Environment interpolation in the registry is whole-value only.** In
`models.yaml`, a value is expanded only when the entire string is exactly
`${VAR}`. There is no `${VAR:-default}` and no embedded substitution:

```yaml
base_url: ${LOCAL_BASE_URL}                  # expanded
base_url: ${LOCAL_BASE_URL:-http://x/v1}     # NOT expanded — treated as a var
                                             #   named "LOCAL_BASE_URL:-http://x/v1",
                                             #   resolves empty, model is skipped
base_url: http://${LOCAL_HOST}/v1            # NOT expanded — the literal string,
                                             #   including "${LOCAL_HOST}", is used
```

The routing file uses a different, more capable expander (see below). Do not
carry habits from one file to the other.

## The routing file

`routing.yaml` is optional. It configures the deployment-wide weight strategy and
the health probe loop. The schema is `RoutingConfig` in
`apps/backend/routing/config.py`; every key with its real default:

| Key | Default | Meaning |
|---|---|---|
| `default_router` | `fixed` | Deployment-wide weight strategy. Only `fixed` does anything: `RoutingManager.apply()` returns without touching weights for any other value. |
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

How it is applied: `RoutingManager` groups each model's registered adapters into
local and remote — an adapter joins a group when its `base_url` matches that
entry's `endpoint:` exactly *and* the model is named in that entry's `models:`.
It then splits traffic
between the two groups (`FixedRatioStrategy`, 50/50 unless the deprecated
`routing_parameter.local_fraction` block sets otherwise), spreads weight evenly
within each group, and renormalises so the weights of a model's routes sum to
1.0. When one group is empty for a given model, the whole weight goes to the
other. A model that matches nothing here keeps the weights from its `route:`
list.

Health probing covers `local_deployment` endpoints only — remote providers do not
serve the gateway's `/health` path and would be marked unhealthy for it. Each
probe is a `GET` to the endpoint's origin root plus `/health`.

**The probe is advisory: it does not change routing.** `RoutingManager.apply()`
is the only reader of its verdicts and it runs once, during bootstrap,
synchronously — before the prober task it just started has had a chance to run a
single probe. So grouping always sees every endpoint as healthy, and an endpoint
that fails every probe keeps its weight and keeps taking traffic. What the probe
does do is report: each transition (healthy → unhealthy and back) is logged at
`WARNING`, and `GET /routing` publishes the current verdicts under
`manager_status.endpoint_health`, alongside `endpoint_health_enforced: false` as
a standing reminder that the map is observation, not admission control. Removing
a wedged endpoint from rotation is the circuit breaker's job
([Routing](routing.md)).

Unlike the model registry, this file's expander handles `${VAR}`,
`${VAR:-default}`, and variables embedded in longer strings, at any depth.

`routing_strategy:` and `routing_parameter:` are the deprecated spellings of
`default_router:` and per-model `router_params:`. They still load, and log a
deprecation warning. Per-model router selection (`router:` / `router_params:` in
the model registry) overrides `default_router` and is documented in
[Routing](routing.md).

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
([Adding a New Provider](adding-models.md#adding-a-new-provider)).

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
| Tune RouteWise for one model | RouteWise settings | `/admin/routewise/model-settings` — see [RouteWise](routing.md#routewise) | `site_settings` |

`GET /admin/routing/provider-routes` (or `.../{model_id}`) returns every route
the gateway is serving, each tagged `source: yaml`, `override` or `runtime`,
and is the quickest way to see what the two layers add up to.

The provider selector on this tab is derived from the deployment, not from a
list in the code: a built-in vendor kind is offered once the registry, the live
route table or a configured credential names it, alongside every custom
provider. Which route types a provider may be added as — `on_demand`,
`quota` or `concurrency` — is the deployment's contract with that vendor and
is declared with `PROVIDER_ROUTE_TYPES` (see
[Environment variables](#environment-variables)); an unlisted provider may
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
4. Runtime route candidates. A runtime *model* is resurrected only when its
   commit marker is present; a candidate whose model is no longer in the
   registry is quarantined with a warning rather than registered, so removing
   a model from YAML does not bring it back through a leftover row.
5. Route overrides, retargeting the registry routes they name.
6. Key tier reservations, re-read now that every provider is known; then
   weight overrides, disabled providers and model visibility, loaded into
   resolvers the router consults at request time.

Two rules follow from that order. A stored change never edits the file it
overrides: delete the override and the registry route, weight or `router:`
value is back, and a runtime candidate sits beside the registry's routes rather
than replacing them. And the file still wins for anything the store has no row
for, so a registry edit plus restart is how catalog metadata, aliases and new
adapter kinds change.

## Environment variables

`Settings` (`apps/backend/serving/config/settings.py`) reads a `.env` file from
the working directory and the process environment, case-insensitively; the
process environment wins. `.env.example` in the repository root is the annotated
list — copy it to `.env` and edit. Secrets belong here and only here: not in the
model registry (reference them as `${VAR}`), not in the manifest.

## Running with your configuration

```bash
uv run uvicorn serving.servers.app:app --no-proxy-headers --port 8080
```

Then check what actually loaded:

```bash
curl -s localhost:8080/health           # includes routes_configured
curl -s localhost:8080/v1/models        # generated from the registered adapters
```

The startup log is the authority on resolution. `Registered N routes from <path>`
names the file that won; `[distribution dark mode] ...` lines show what a manifest
would have changed; `Skipping model '<id>' ... after env expansion` names each
model whose credentials were unset.

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
