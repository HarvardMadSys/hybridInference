# Configuration

This page explains how a running gateway finds its configuration: which files it
reads, where those files live, and which layer wins when more than one names the
same file. It is the conceptual companion to
[Adding a New Model](adding-models.md) (the field-by-field model registry reference)
and [Routing](routing.md) (what the routing engine does with the result).

For distribution manifests, branding, UI modules and backend extensions, see
[Distribution customization](distribution-customization.md).

## Email and SMS delivery providers

`EMAIL_PROVIDER` defaults to `smtp`; `SMS_PROVIDER` defaults to `none`. Every other provider is an optional module that a deployment loads explicitly. A gateway that does not load a provider neither offers it nor lists its settings in **Admin > Configuration**. Without a provider beyond the built-in one, it does not list `EMAIL_PROVIDER` or `SMS_PROVIDER` either, which would have only one value to choose, and without an SMS provider it lists none of the phone settings. Selecting an unavailable provider is a configuration error; delivery never silently falls back to another provider.

Each distribution can add the providers its own inference hub needs, such as a regional email or SMS service. A trusted module selected through `BACKEND_EXTENSIONS` registers its email or SMS transport and configuration entries before database-backed settings load. The shared interfaces are `register_email_provider` and `register_sms_provider` in `serving.utils.delivery`, and `register_config_entries` in `serving.config.app_config_registry`. Extension settings keep the normal database → environment → registered default precedence, validation, secret masking and worker refresh. The extension reads them through `config_value()` instead of adding vendor fields to shared `Settings`.

### Tencent Cloud SES and SMS

The gateway ships one such module, `serving.integrations.tencent_cloud`, for Tencent Cloud transactional email and SMS. It is not loaded by default. Add it to `BACKEND_EXTENSIONS` to offer `EMAIL_PROVIDER=tencent_ses` and `SMS_PROVIDER=tencent`. A failed SES request or missing SES template does not fall back to SMTP.

Configure `TENCENT_CLOUD_SECRET_ID`, `TENCENT_CLOUD_SECRET_KEY`, optional `TENCENT_CLOUD_TOKEN`, `TENCENT_SES_REGION`, `TENCENT_SES_FROM_EMAIL` and optional `TENCENT_SES_FROM_NAME`. The sender must be approved in the selected region. Secret values remain server-side and are redacted in the configuration API. Keep production credentials and sender identities in deployment configuration.

Set positive IDs for `TENCENT_SES_TEMPLATE_VERIFICATION` and `TENCENT_SES_TEMPLATE_PASSWORD_RESET` before enabling SES account mail. Configure `TENCENT_SES_TEMPLATE_APPROVAL`, `TENCENT_SES_TEMPLATE_REJECTION`, `TENCENT_SES_TEMPLATE_ADMIN_SIGNUP` and `TENCENT_SES_TEMPLATE_NOTICE` for the notifications the deployment uses. Unset IDs disable those templates. Configuration health reports missing SES account-mail fields when public signup requires email verification, instead of asking for SMTP credentials.

Reference HTML lives in `apps/backend/serving/integrations/tencent_cloud/ses-templates/`. Before submitting templates for provider approval, replace the example domain with the deployment console domain and choose the desired language and wording. Verification and reset templates contain a fixed HTTPS origin and path; only the encoded `token` query value varies. Other templates use `site_name` and, where applicable, `reason`, `identifier` or `body`. Keep template links consistent with `FRONTEND_URL`; the gateway cannot inspect approved template contents.

SES receives approved templates with escaped scalar values, never arbitrary HTML variables. Serialized template data is limited to 800 UTF-8 bytes; oversized notices and HTML-only notices fail rather than being truncated. Successful submission means provider acceptance, not inbox delivery. Calls are not automatically retried because a timeout can follow acceptance. Deployments that require unrestricted HTML mail or long notices can continue to use SMTP.

The SMS provider requires `TENCENT_CLOUD_SECRET_ID`, `TENCENT_CLOUD_SECRET_KEY`, `SMS_REGION`, `SMS_SDK_APP_ID`, `SMS_SIGN_NAME` and `SMS_TEMPLATE_ID`. Temporary credentials also require `TENCENT_CLOUD_TOKEN`. Its approved template takes the code and its lifetime in minutes, in that order. It currently sends to mainland China mobile numbers only: set `SMS_DEFAULT_COUNTRY_CODE` to `86` if local input is desired and `SMS_ALLOWED_PREFIXES` to `+86`. Keep real credentials, approved signatures and template IDs in deployment configuration, outside source control.

A deployment that used the Tencent adapter before it became optional adds the module to `BACKEND_EXTENSIONS` when it upgrades; setting names and stored values are unchanged. On a gateway that does not load a provider, a stored row it once used lists as an ordinary custom setting that no code reads, keeping its secret flag, and an administrator can delete it.

## What the gateway reads at startup

| Kind | Holds |
|---|---|
| `models` | The model registry: every model id the gateway serves and the upstream routes behind it |
| `routing` | Optional deployment-wide settings: health probing and a local/remote weight split |
| `alerts` | Alert rules and thresholds |
| Settings | Credentials, SMTP, proxy trust, logging, routing tuning and every other application setting, stored in the database and edited in the admin console |
| Environment | The database connection, the paths to the files above, and how the containers are wired |

Only the model registry is required to serve traffic. Without it the gateway
starts, serves `/health`, and answers `GET /v1/models` with an empty list.
Without a routing file each route keeps the weight the registry gives it, and
without an alerts file the built-in thresholds apply.

The built-in `rules.request_log_queue_saturation` rule pages when the bounded
request-log queue drops `api_logs` report rows. It is on by default but still
only runs when alerting is otherwise enabled (`ALERTS_ENABLED` and a configured
channel). Tune or disable it in `alerts.yaml`:

```yaml
rules:
  request_log_queue_saturation:
    enabled: true       # default
    window_sec: 600     # default
    threshold_count: 1  # warning events in the window; default = first loss
    cooldown_sec: 1800  # default
```

`threshold_count` counts the producer's loss warnings, not dropped rows. The
producer warns on the first drop, every 100th drop, and at least once per
60 seconds while drops continue, so a second small burst after a quiet period
is still observable. The rule uses the alert engine's existing cooldown, so a
sustained loss episode does not page once per warning.

The files are not the whole story once a database is configured. The admin
console writes providers, keys, routes, weights and per-model overrides to the
operational store, and the gateway re-applies that state on top of the loaded
registry at every start.
[Runtime configuration from the admin console](#runtime-configuration-from-the-admin-console)
covers that layer, and
[Settings stored in the database](#settings-stored-in-the-database) covers the
settings.

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
skipped because its credential is unset. Set OPENROUTER_API_KEY (Admin > Configuration)
and restart. /v1/models will stay empty until then, and requests will report the
model as not found.
```

With a database, set it on the admin console's **Configuration** tab instead,
which lists it as missing, and restart the backend.

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

**`${VAR}` interpolation in the registry is whole-value only.** In
`models.yaml`, only `base_url`, `api_key`, `api_keys`, `provider_model_id` and a
route's `embeddings_path` are expanded, and only when the entire value is
exactly `${VAR}`. The value is the [setting](#settings-stored-in-the-database)
of that name: its database value, else the environment variable, else the
default the gateway registers for that setting, so `${CHUTES_BASE_URL}` loads
as `https://llm.chutes.ai/v1` when nothing sets it. A database or environment
value wins even when it is empty. There is no `${VAR:-default}` and no
embedded substitution:

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
`${VAR:-default}`, and variables embedded in longer strings, at any depth. The
alerts file expands the same way. A variable neither the database nor the
environment sets takes the inline `:-default`, and without one the default the
gateway registers for that setting.

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

Each row is a provider that at least one route goes through. A slug the model
registry only mentions is reserved the same way but gets no row of its own: a
model's own `provider:`, which names who makes the model while its routes say
who serves it, and the `kind:` of a route whose `route_metadata` names a
different `key_provider`, a route listed under that `key_provider` instead.

A key added here joins the same pool as the keys the registry names through
`${VAR}` and rotates with them. A key the registry names — an *environment
key* on this tab, whose value comes from the
[settings](#settings-stored-in-the-database) — has no row in this table, so
this tab can disable it or reserve it for a tier but cannot delete it. To
remove it, clear the variable on the **Configuration** tab and restart the
backend.

### Routing tab

| What | Console | Endpoint | Stored in |
|---|---|---|---|
| Create a model that is not in the registry | **Create model** | `POST /admin/routing/provider-route-models`; `POST /admin/routing/provider-route-model-verifications` tries the route without registering it | `provider_route_candidates`, plus a strategy and required-role marker in `site_settings` |
| Add a route to an existing model | **Add provider route** | `POST /admin/routing/provider-route-candidates/{model_id}`; `.../provider-route-candidate-verifications/{model_id}` to check first; `PATCH` / `DELETE /admin/routing/provider-route-candidates/{model_id}/{route_id}` edits it in place or deletes it | `provider_route_candidates` |
| Point a registry route somewhere else | **Edit** | `PUT /admin/routing/provider-routes/{model_id}/{route_id}`; `.../provider-route-verifications/{model_id}/{route_id}` to check first (it checks a runtime route's edit too); `DELETE` removes the override and restores the YAML route (**Reset config**) | `provider_route_configs` |
| Change what a route accepts: context window, max output, image input, tool calling | **Capabilities** on each route | `PUT /admin/routing/provider-route-capabilities/{model_id}/{route_id}`; `DELETE` returns the route to the values it has from the registry or its creation | `provider_route_capabilities` |
| Disable or enable a route of either source | **Disable** / **Enable** | `PATCH /admin/routing/provider-route-states/{model_id}/{route_id}` with `{"disabled": true}`; the route keeps its configuration and weights, and nothing (selection, retry, fallback, offload) picks it until it is enabled | `provider_route_states` |
| Delete a registry route from this deployment | **Delete**, then **Restore** | `POST /admin/routing/provider-route-removals/{model_id}/{route_id}`; `DELETE` on the same path restores it with its edits and weights. models.yaml is not changed | `provider_route_states` |
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
is declared with the `PROVIDER_ROUTE_TYPES` setting (see
[Environment variables](installation.md#environment-variables)); an unlisted provider may
use any of the three.

A model created here is a **runtime model**: it exists only in the database,
carries an id, a first route, a pricing table, a router strategy and a
`required_role` (default `admin`, so a new model stays invisible to ordinary
users until you lower it), and is restored at every start. It also declares
what it accepts, as `capabilities` in the request: `context_length`,
`max_output_length`, `input_modalities` (`text`, optionally `image`, `audio`
and `video`) and `supports_tools`. All four are required, because each changes
what a request does: a model without tool support has tool definitions
dropped, `max_tokens` is clamped to `max_output_length`, an image reaches only
a route that accepts images, and Auto and the context-overflow fallback read
`context_length`. The two lengths are whole numbers of tokens, at most
10,000,000. A route added to any model, runtime or registry, declares its own
four the same way; the console starts them from the model's first route, and the
route stores what was submitted. Each route stores and restores its own four,
since routes of one model can differ. A runtime model created before they could
be declared keeps the defaults it was given: `context_length` 8192,
`max_output_length` 4096, text only, no tool support. Supported parameters such
as `reasoning_effort`, structured output and aliases still take `ModelConfig`'s
defaults; when a model needs any of those, put it in the registry.

Any route's four can be changed after the fact with **Capabilities**, registry
and runtime routes alike, with the same validation. The console's form has a
field for image input only: it keeps a route's `audio` and `video` input as they
are, and an added route starts with the first route's. The values the route has
from the registry or from its creation stay as its baseline: the change is
stored as an override, the next request through the route follows it, a restart
restores it, and retargeting the route keeps it. Deleting a registry route
keeps it too, so **Restore** brings the route back with it. Resetting deletes
the override and returns the route to its baseline. Route listings report `capabilities` (as
served), `capabilities_baseline` and `capabilities_overridden`. A change applies
in the gateway process that handles the admin request; a deployment running
several gateway processes sees it in the others after their next restart.

### Settings tab

| What | Console | Endpoint | Stored in |
|---|---|---|---|
| Change who can see a model | **Model Visibility** | `PATCH /admin/models/{model_id}/visibility` sets or clears a `required_role` override; `GET /admin/models/visibility` lists baseline, override and effective values | `model_visibility_overrides` |
| Exempt a model from the per-user concurrency limit | **Model Concurrency Limit** | `PATCH /admin/models/{model_id}/concurrency` | `model_concurrency_exemptions` |
| Cap how many batch requests a model runs at once | **Batch Concurrency** | `PATCH /admin/batch/models/{model_id}` sets or clears a model's cap; `GET /admin/batch/models` lists the caps with each model's running and queued batch requests. The other batch settings are in [Running batch inference](batches.md#running-batch-inference) | `batch_model_limits` |

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
6. Route capability overrides, last of the route state, so they reach every
   route the steps above installed.
7. Key tier reservations, re-read now that every provider is known; then
   weight overrides, disabled providers, offload routes and model visibility,
   loaded into resolvers the router consults at request time.

Two rules follow from that order. A stored change never edits the file it
overrides: delete the override and the registry route, weight or `router:`
value is back, and a runtime candidate sits beside the registry's routes rather
than replacing them. And the file still wins for anything the store has no row
for, so a registry edit plus restart is how catalog metadata, aliases and new
adapter kinds change.

## Settings stored in the database

Everything the gateway reads that is not one of the files above, the database
connection or container wiring is a *setting*: provider keys, SMTP, Slack
webhooks, proxy trust, browser origins, logging, routing and timeout tuning,
the cloud-agent contract, and the secrets that sign sessions and protect API
keys. A gateway with a database keeps them in its `app_config` table, and an
administrator edits them on the admin console's **Settings** tab. A
gateway with `DB_ENABLED=false` has no stored settings and reads the
environment and the built-in defaults only.

### Where a value comes from

Each setting resolves in this order:

1. its row in the database — even an empty one, which is how a setting is
   cleared;
2. the environment variable of the same name;
3. the built-in default.

The environment is a starting point. At every start, a setting that has a
non-empty value in the environment and no row in the database is copied into
the database, and from then on the row wins. That is how an existing `.env`
carries over on an upgrade, and why editing the variable afterwards changes
nothing: the tab marks it **Environment ignored**. Change the value on the tab
instead, or **Reset** it, so that the environment, and then the default,
applies again. A reset lasts: the database remembers it, so the next start does
not copy the variable in again, and later edits to it take effect. The
switches on the **Settings** tab —
`USER_AUTH_ENABLED`, `SIGNUP_ENABLED` and the others — are copied from the
environment the same way.

An environment value that is not valid on its own — a CIDR range that does not
parse, a number that is not one — is not copied in. The backend then starts
only if the database holds a valid value for that setting, which applies, with
a warning in the log; otherwise it stops and names the variable. Fix or remove
the variable, or store a valid value with `python -m serving.config.manage set`
(see *Without the console* below), which works while the environment is
invalid. The database connection settings (`DB_HOST`, `DB_PORT` and the
others) are the exception: they are needed to read the database, so an invalid
one always stops the backend.

Settings that depend on each other — `TRUST_X_REAL_IP` needs
`TRUST_PROXY_HEADERS` — are judged together, and a stored value wins there
too. A variable whose partner is already stored and satisfies the rule is
copied in like any other, so it survives the variable's removal. One that
breaks the rule with a stored value is set aside instead: it counts as unset,
its default applies, the next start does not copy it in, and the log says so
with a warning. With `TRUST_X_REAL_IP=1` in the environment and
`TRUST_PROXY_HEADERS` stored as off, for example, the gateway runs with
`TRUST_X_REAL_IP` off; to turn both on, change them together on the
Settings tab or with `python -m serving.config.manage set`. Only a rule
the environment breaks on its own, with no stored value involved, stops the
backend, which names the rule.

On the first start against an empty database the backend also generates the
two secrets it cannot run without, `JWT_SECRET_KEY` and `API_KEY_SECRET`,
unless the environment supplies them. `API_KEY_SECRET` and
`ERASURE_FENCE_SECRET` cannot be changed once set, because records depend on
them; see [Database](database.md#secrets-for-deleting-accounts). The backend
refuses to start rather than generate `API_KEY_SECRET` for a database that
already holds API keys, since a new secret would invalidate every one of them.

### Analytics sections

In **Admin → Settings → Common → Analytics**, toggle **Token Usage**, **Users
and Requests**, **Performance Metrics** and **Growth Rate** independently, then
select **Save changes**. These database-backed settings apply to
**Analytics → Overview** for all administrators. Token Usage, Users and Requests
and Performance Metrics default to on; Growth Rate defaults to off. Reopen
Analytics after saving; no backend restart is needed. A disabled section is
hidden and does not request its data. Data collection continues, so re-enabling
a section can show existing history.

The runtime keys are `analytics_token_usage_enabled`,
`analytics_users_and_requests_enabled`, `analytics_performance_metrics_enabled`
and `analytics_growth_rate_enabled`. The overview's Hour / Day / Week / Month
control applies to Token Usage and Users and Requests. Performance Metrics keeps
its existing windows, and Growth Rate has its own 30, 60 or 90-day range.

### On the Settings tab

The tab opens on **Common**, with the switches and limits changed most often:
sign-up, per-user concurrency and the daily quotas among them. The rest is
grouped by category, and each category is listed on the sub-tab of its topic:
**Access** holds the sign-up settings and the signup allowlist, for instance.
**Setup**, near the end, holds the categories usually set once, when the
gateway is deployed: its addresses and name, email, network, privacy and
integrations. Within any category, the settings usually set once come last,
under **Usually set once**. The tab describes each setting and shows where its
value comes from. A search, or **Missing only**, looks through every sub-tab at
once. Besides every setting the gateway reads itself, it lists:

- every `${VAR}` the active model registry and routing file name, under
  **Providers**, with the models that use it, and those of the alert rules
  under **Alerts**;
- numbered extra keys for a provider's key pool, `MINIMAX_API_KEY1` to
  `MINIMAX_API_KEY20` beside `MINIMAX_API_KEY` for instance, once one is set.
  Every one that has a value joins the pool: a blank one in the middle does
  not end the list, and a key entered twice counts once;
- variables added with **Add variable**, for a `${VAR}` reference the gateway
  cannot see yet, such as one in a registry you are about to switch to.

It leaves out the settings that offer nothing to choose: one with a single
accepted value, such as `EMAIL_PROVIDER` while SMTP is the only transport, and
the phone settings while no SMS provider is registered, since they do nothing
without one. Such a setting still appears while it needs attention — required
and empty, stored with a value that cannot apply, or waiting for a restart —
and `python -m serving.config.manage list` always shows it.

Secrets are write-only: the tab shows whether one is set, never its value, and
the admin audit log records only that it changed. Besides the settings the
gateway knows to be secret, every `${VAR}` a configuration file uses in a
credential field — `api_key`, `api_keys`, `token`, an `Authorization` header —
is secret, whatever it is called. Any other variable is secret when its name
contains `KEY`, `TOKEN`, `SECRET`, `PASSWORD`, `PASS`, `PWD`, `WEBHOOK`,
`PRIVATE`, `AUTH`, `CREDENTIAL`, `BEARER`, `CERT`, `DSN` or `SA_JSON`, and a
variable added under such a name always is. An administrator can also mark a
discovered, numbered or added variable secret with **Mark as secret**. That
works one way only: a secret is never shown again, not even after a reset.

A save is checked as a
whole, against everything it leaves in force, so settings that depend on each
other — `TRUST_CLOUDFLARE_HEADERS` needs `TRUST_PROXY_HEADERS` — can change
together. A save or a **Reset** that would leave another setting unable to
apply is refused and names it: turning `TRUST_PROXY_HEADERS` off while
`TRUST_X_REAL_IP` is on, for example, or clearing the Cloudflare networks while
`TRUST_CLOUDFLARE_HEADERS` is on. Change both in one save. A **Reset** is also
refused when the environment value it would fall back to is not valid. A
setting that was already invalid never blocks an unrelated change. The names that stay in the
environment, such as `DB_PASSWORD` or `MODELS_CONFIG_PATH`, cannot be set here.
Behind the tab are `GET` and `PATCH /admin/config` and
`DELETE /admin/config/{key}`.

### When a change applies

Most settings apply as soon as they are saved: at once in the backend process
that served the save, and within ten seconds in every other one. A setting
marked **Restart required** is read only at startup, and the process keeps the
value it started with until it restarts. Provider credentials and every
variable the registry names are among them, as are the circuit-breaker and
outbound-concurrency tuning, `ALERTS_ENABLED` and `DB_STORE_FULL_CONTENT`. The
alert channels and the failed-request alert thresholds are too, until an
operator activates their [live group](#settings-that-apply-without-a-restart).

While such a change is waiting, the tab shows a **Restart pending** notice with
a **Restart backend** button. The button (`POST /admin/system/restart`) makes
the backend exit so that its supervisor starts it again: Docker, through the
bundled Compose file's `restart: unless-stopped`, or systemd, through the
bundled units' `Restart=on-failure`. Nothing inside the process can see that
policy, so the deployment declares it: the bundled Compose file and units set
`SUPERVISOR_RESTARTS_BACKEND=1` next to it, and a deployment that starts the
backend some other way sets it only if its supervisor restarts the process
when it exits. Without it, and whenever several worker processes serve the
backend (`WEB_CONCURRENCY` or `UVICORN_WORKERS` above 1, or
`uvicorn --workers`, where the button would restart only one of them), the tab
shows a command to run yourself instead, such as
`docker restart hybridinference-backend`. A restart cuts off requests in
flight, streams included. The console knows the restart is over once `/health`
names a different process in its `instance` field, however quickly the backend
came back.

### Settings that apply without a restart

A live group is a set of settings the backend applies together, without a
restart, once an operator has activated it. The first one, `alerts.runtime`,
holds the alert channel and the failed-request alert:

| Settings | What they choose |
|---|---|
| `SLACK_ALERTS_WEBHOOK_URL`, `SLACK_WEBHOOK_URL`, `WECOM_ALERTS_WEBHOOK_URL` | The one channel alerts go to |
| `FAILED_REQUEST_ALERT_THRESHOLD`, `FAILED_REQUEST_ALERT_WINDOW_MINUTES`, `FAILED_REQUEST_ALERT_COOLDOWN_MINUTES`, `FAILED_REQUEST_ALERT_RATE`, `FAILED_REQUEST_ALERT_RATE_MIN_COUNT` | When the failed-request alert pages |

Once the group is active, a save reaches every backend process within about
ten seconds. Each process checks the whole group first: a pair of channels
that conflict, or a value out of range, keeps the values it already uses and
reports the group as invalid, while the saved values stay for you to correct.
To move from Slack to WeCom, clear the Slack webhooks and fill in the WeCom
one in the same save. Clearing every webhook pauses alert delivery and the
failed-request alert on purpose; it is not a failure. The failed-request alert
runs while `SLACK_WEBHOOK_URL` or `WECOM_ALERTS_WEBHOOK_URL` is set, as before;
`SLACK_ALERTS_WEBHOOK_URL` alone does not start it.

The Alerts category shows where the saved configuration stands. **Applied in
each backend process** names the saved version — for example
`app_config / epoch 3f1c… / revision 42` — and, for every process that has
registered, whether it has applied that revision, is still applying it, failed
or has not reported. A process whose heartbeat stopped more than a minute ago
counts as unknown, never as applied. A backend running an older build does not
register at all, so the panel cannot prove that every process is covered:
compare its list with your deployment's own list of processes. **Applied**
means the process uses the saved channel and thresholds; it says nothing about
whether a message reached the channel, which **Send test** checks. **Send
test** uses the saved webhook (or the address you typed), not necessarily the
one a process is using.

After a channel change, alerts already under way finish on the old channel,
and the recovery message of an incident opened earlier goes to the new one.
The failed-request alert keeps its cooldown and its open incident across the
change. It still checks once a minute, so a new threshold takes effect at the
next check after the change has applied.

The console saves conditionally: if someone else saved the configuration after
you started editing a category, your save is refused, nothing is stored, and
the tab reloads the latest values and keeps your unsaved changes for you to
review and save again. A reload, or a save in another category, never moves
unsaved changes onto values you have not seen.

`ALERTS_ENABLED`, the alert rules file and the alert engine still apply only
after a restart. Other settings join live groups later, one group at a time.

#### Activating a live group

A group stays inactive until an operator activates it, and a backend upgrade
never activates one by itself. Until then its settings behave as **Restart
required**. Activate it once every backend process runs a build that supports
it:

1. Deploy the build everywhere and check that each process registered:
   `python -m serving.config.manage live status`.
2. Preview what activation applies. It shows each setting's saved value (a
   secret only as set or not set) and the processes whose value in use differs
   — values saved while a restart was pending apply as soon as you activate,
   which can change where alerts go:
   `python -m serving.config.manage live preview alerts.runtime`.
3. Activate with the version the preview printed, and the number of backend
   processes your deployment runs:
   `python -m serving.config.manage live activate alerts.runtime --expect <epoch>:<revision> --expect-instances <N>`.
   It refuses when the configuration changed after the preview, when a running
   process does not support the group, or when the count differs.
4. Check `live status`, or the Alerts category, until every process has
   applied the latest revision.

If a process cannot apply the group when it is activated — a saved value is
invalid, say — the group stays active and the process keeps the values it was
using. Correct the setting and save it again: it applies without a restart.

`live deactivate alerts.runtime --expect <epoch>:<revision>` turns the group
off. A running process then keeps the values it last applied until it
restarts, instead of returning to the ones it started with, and a process that
starts afterwards uses the saved values. Until every process has restarted the
two can differ, and the status shows it. Going back to a build that does not
know the group is a deployment, not a configuration change.

A configuration backup does not include this status. Restoring a backup
applies its `app_config` rows to live groups like any other save; the other
tables in the backup are written separately, so a failure there can leave them
partly restored. After restoring a whole database, run
`python -m serving.config.manage live reset-epoch --expect <epoch>:<revision>`:
it starts a new version history and makes every process register again.

### Without the console

A setting can break the console that edits it — a value that stops sign-in
from working, say. `python -m serving.config.manage` changes stored settings
from the command line instead, with the console's own validation:

```bash
docker exec -it hybridinference-backend python -m serving.config.manage list
docker exec -it hybridinference-backend python -m serving.config.manage reset JWT_ALGORITHM
```

`list` shows every setting, `get KEY` one of them, `set KEY VALUE` stores a
value — add `--secret` to mark a discovered, numbered or added variable
secret, as **Mark as secret** does — and `reset KEY` resets it as the tab's
**Reset** does, so that the environment or the default applies again. It never
prints a secret's value. A running backend picks a change up within about ten
seconds, or at its next restart for a setting marked **Restart required**.

The tool talks to the database directly with the `DB_*` connection settings
alone, so it needs no sign-in and still runs when another setting in the
environment is invalid. Like the backend, it first reads `.env` from the
directory it runs in, without replacing a variable already set, so a value
kept only there is the one it shows and the one a reset falls back to. When
the backend container is not running, run it in the repository root of a
source checkout instead (`uv run python -m serving.config.manage ...`), where
`.env` reaches Postgres on `127.0.0.1:5432`. The command that resets an
administrator's password works the same way; see
[The first admin account](deployment.md#the-first-admin-account).

### Missing settings

Some settings are required: the two generated secrets, every variable a model
route needs to load (unless the route is `optional: true`), and `SMTP_USER`
and `SMTP_PASSWORD` while public signup requires email verification. When one
of them has no value, the console shows a banner to signed-in users.
Administrators get a link to the Settings tab, where **Missing only**
lists what to fill in; everyone else is told that some features may not work
and to contact the administrator. A model whose credential is missing is not
loaded, and a request for it is answered with a `404`. When an administrator
sends it with their own API key, the `404` says the deployment's configuration
is incomplete; anyone else gets the same "not found" as for a model the
deployment does not define, so the difference reveals nothing beyond what
they may use.

A deployment that leaves a model's credential unset on purpose — a route it
does not use yet, or a canary it enables elsewhere — should mark that route
`optional: true` in the model registry. The route is then skipped quietly,
and its variable is not reported as missing.

```{warning}
Settings are stored in plaintext. Anyone who can read the database — a
`pg_dump`, a backup, a `psql` or pgAdmin session — can read every provider
key, decrypt users' stored API keys and sign administrator tokens. Protect
database access and backups as you would the credentials themselves.
```

## Environment variables

The environment keeps what the backend needs before it can reach its database
and what describes the containers rather than the application:

- the database connection: `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`,
  `DB_PASSWORD`, `DB_ENABLED`;
- the configuration files: `MODELS_CONFIG_PATH`, `ROUTING_CONFIG_PATH`,
  `ALERTS_CONFIG_PATH`, `DISTRIBUTION_CONFIG_PATH`,
  `DISTRIBUTION_CONFIG_MODE`;
- the backend process: `LOG_FILE`, `BACKEND_EXTENSIONS`, `GEOIP_COUNTRY_DB`,
  `GEOIP_COUNTRY_PROVIDER`, `WEB_CONCURRENCY`, `SUPERVISOR_RESTARTS_BACKEND`,
  and `REFRESH_TOKEN_COOKIE_NAME`, which the console reads too;
- Compose and the console: host ports and bind addresses, the console's build
  values and its runtime ones such as `AGENT_WEB_INTERNAL_URL`.

The gateway reads a `.env` file from its working directory and the process
environment; the process environment wins. `.env.example` in the repository
root is the annotated list — copy it to `.env` and edit.
[Installation](installation.md#environment-variables) lists the settings you
are most likely to set. A value in the environment for any other setting is
imported into the database at startup, as described above. Never write a
secret into the model registry or the manifest: reference it there as
`${VAR}`.

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

## Optional phone authentication

Phone signup, login and binding are disabled by default. Install a distribution-owned SMS provider, configure its delivery settings, then enable `SMS_AUTH_ENABLED` and select that registered provider through `SMS_PROVIDER` in **Admin > Configuration**, where the phone settings appear once a provider is registered. The gateway advertises `features.phone_auth` through `/site-config`; the shared forms show the phone option only when it is available. Existing email and first-run administrator logins continue to work.

Account identifiers use international format, such as `+12025550123`. `SMS_DEFAULT_COUNTRY_CODE` optionally supplies a calling code for local input; leave it empty to require international input. `SMS_ALLOWED_PREFIXES` optionally restricts destinations using comma-separated prefixes. The distribution chooses a number policy supported by its delivery provider; account storage and verification-code policy remain shared.

Codes expire after 300 seconds by default, allow at most five guesses, and can be consumed once. PostgreSQL serializes send and verification quotas across workers; the `SMS_OTP_*` settings control their bounds. Turnstile and trusted-proxy client IP handling remain shared with signup. Phone registration follows public-signup and approval policy, never automatically merges with an email account, and creates no password. Like email signup, it is refused with `503` while first-run setup is pending. Binding requires an authenticated session and a separate code for that account.

Startup adds phone identity columns and durable code tables and makes password hashes nullable. When a long-running `pg_dump` holds the users table, the columns land later, from the background, and phone sign-in answers `503` until they do; see [Database](database.md#how-the-schema-is-created). Upgrade every gateway worker before enabling phone signup. Back up the database first; once phone-only accounts exist, an older binary that assumes every account has a password is not a supported rollback. Disabling SMS hides phone login but does not remove accounts, so arrange another verified sign-in method before disabling it for phone-only users. Site UI v1 message overrides remain optional; distributions can translate the added phone message slots without owning authentication logic.
