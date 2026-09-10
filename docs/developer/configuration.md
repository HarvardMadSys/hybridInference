# Configuration

This page explains how a running gateway finds its configuration: which files it
reads, where those files live, and which layer wins when more than one names the
same file. It is the conceptual companion to
[Adding a New Model](adding-models.md) (the field-by-field model registry reference)
and [Routing](routing.md) (what the routing engine does with the result).

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

There is no `config/models.yaml` or `config/routing.yaml` in this repository.
`config/` holds one directory, `config/examples/`, containing reference files
that are meant to be copied or pointed at:

```text
config/examples/
├── models.openrouter.yaml     # neutral default catalog (llama-3.3-70b, ...)
├── routing.minimal.yaml       # companion routing file, names no hosts
└── distribution.example.yaml  # annotated manifest reference
```

A real deployment's configuration lives in a **distribution overlay**: one
directory under `distributions/` holding that deployment's manifest, config
files, Compose/env inputs, and branding.

```text
distributions/<name>/
├── distribution.yaml       # the manifest: identity + where the config files are
├── config/
│   ├── models.yaml
│   └── routing.yaml
└── deploy/
    ├── backend.env         # any *.env here; Compose reads them all
    └── docker-compose.yml  # optional overlay on deploy/docker/docker-compose.yml
```

The split exists so the source tree and the container image stay neutral. Model
IDs, upstream base URLs, ports, site identity and branding are properties of one
deployment, not of the project; keeping them in an overlay means the image can be
built once and a deployment's overlay mounted into it (`deploy/docker/docker-compose.yml`
mounts the overlay read-only rather than baking it into the image). It also means
a clone of this repository ships nobody's hosts.

Not every change needs a file. A provider, a key, a route or a whole model can
also be added from the admin console while the gateway runs; that state lives
in Postgres rather than in the overlay, and is described
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
   `DISTRIBUTION_CONFIG_MODE=active`; see below.
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

## The distribution manifest

A manifest is one versioned YAML document that names a deployment and tells the
gateway where its config files are. `config/examples/distribution.example.yaml`
is the annotated reference. Both examples below and in that file close public
signup when activated; change `public_signup` to `true` or null if this
deployment should accept new accounts through the public signup API:

```yaml
schema_version: 1

distribution:
  id: example
  display_name: Example Router
  release: "1.0.0"

site:
  public_base_url: https://your-gateway.example
  support_email: support@your-gateway.example

features:
  routers: [fixed]
  public_signup: false
  rag: false

paths:
  models: config/models.yaml      # relative to this file's directory
  routing: config/routing.yaml

deployment:
  target: local
```

Point the gateway at it with two environment variables:

```bash
export DISTRIBUTION_CONFIG_PATH=distributions/<name>/distribution.yaml
export DISTRIBUTION_CONFIG_MODE=active
```

With an active manifest, `features.public_signup: false` disables both the
signup UI and `POST /auth/signup` (HTTP 403, without creating an account).
Neither `SIGNUP_ENABLED=true` nor a runtime `signup_enabled=true` override can
reopen it. To enable public signup, change the manifest to `true` or leave the
field unset/null and restart the backend, then ensure the effective
`signup_enabled` setting is enabled.

The admin API rejects attempts to set `signup_enabled=true` while the active
manifest disables signup (HTTP 400, without saving the setting). Manifest root
and `features` fields reject unknown keys, including `public-signup`,
`publicSignup`, or a `public_signup` field at the manifest root.

When the manifest allows signup, the runtime `signup_enabled` setting takes
precedence over `SIGNUP_ENABLED` (default `true`). The same rule applies with
no manifest or in dark mode; dark-mode feature values have no effect.
`GET /site-config` reports the resulting boolean in `features.public_signup`,
so the console follows the backend policy. Email verification, domain-based
approval, and quotas remain separate checks; allowing registration does not
bypass them.

If a runtime signup-setting read fails or exceeds one second, the gateway
temporarily disables public signup. A valid `/site-config` response still
returns HTTP 200 with its identity and branding intact and
`public_signup: false`; registration returns HTTP 403 without creating an
account. It does not fall back to an environment value that could reopen
registration.

Because `/site-config` is unauthenticated and read while rendering every
console page, the gateway does not repeat a failing read for every request.
Callers that arrive together while the setting's cache is cold share one
store round-trip, and a failed read is reused for five seconds, so an outage
costs one timeout per window rather than one per request. Recovery needs no
restart: the window expires on its own, and writing `signup_enabled` through
the administrator API clears it at once.

### Dark mode is the default, and that is deliberate

`DISTRIBUTION_CONFIG_MODE` defaults to `dark`. In dark mode the manifest is
loaded and validated, and the resolver logs what it *would* change, but current
resolution stays effective:

```text
[distribution dark mode] models config stays config/examples/models.openrouter.yaml
(source=default, sha256=51fd6cde50fd); manifest would use
/srv/app/distributions/example/config/models.yaml (sha256=6117799cd0bf) — DIFFERENT
```

Setting only `DISTRIBUTION_CONFIG_PATH` therefore cannot change behaviour; you
get a warning telling you the mode defaulted to `dark`. Config-path resolution
treats an unrecognised mode as `dark` with a warning. Signup, `/site-config`, and
the RAG feature policy are stricter: an explicitly empty or unknown mode, or
`active` without a manifest path, closes signup (HTTP 403), makes `/site-config`
report an unavailable configuration (HTTP 503), and makes authenticated
`/v1/rag/status` and `/v1/rag/chat` requests return HTTP 503. Only an unset mode
defaults to `dark`, including in Compose. Enable a manifest by running dark
first, reading the comparison lines, and only then setting `active`.

Mode warnings are logged once per distinct value per process. When diagnosing
RAG configuration errors, check the startup logs and the backend's effective
`DISTRIBUTION_CONFIG_MODE` and `DISTRIBUTION_CONFIG_PATH`, rather than expecting
the warning to repeat on every request. Correct the values and restart the
backend. See [Docs RAG Assistant](rag-chat.md) for the feature restriction.

Failure behaviour differs by mode, on purpose. In dark mode a manifest that will
not load is logged and skipped. In active mode the manifest *is* where the paths
come from, so a manifest that will not load — a lost overlay mount, a YAML error
— refuses to start rather than quietly serving a different registry.

The manifest root and `features` are closed schemas: unknown keys are rejected,
including misspelled feature names or feature flags placed at the root. This
changes the earlier behavior that silently ignored these keys. Before upgrading
an active deployment, validate its actual overlay with the new backend code;
the repository's example manifests do not validate privately maintained
overlays. Correct misspellings and keep operator-specific metadata in a separate
file. With `DISTRIBUTION_CONFIG_PATH` set to the deployed manifest, run:

```bash
uv run python - <<'PY'
import os
from pathlib import Path
from serving.config.distribution import load_distribution_config

load_distribution_config(Path(os.environ["DISTRIBUTION_CONFIG_PATH"]))
print("Manifest valid.")
PY
```

This is not exhaustive typo detection: `paths`, `site`, `distribution`, and
`deployment` still ignore unknown section keys for compatibility. In particular,
check `paths.models` and the resolved model-registry log before activating an
overlay; an unknown path key is treated as an omitted path and can select a
legacy default. Strict validation of those sections is a separate compatibility
change.

If an invalid active manifest reaches the HTTP handlers, signup remains closed
and `/site-config` returns HTTP 503 without exposing file paths or parser
errors. It cannot return a valid identity until the manifest is repaired.

### Identity, and what the manifest must not contain

`site:` and `features:` are served as a public subset by `GET /site-config`, and
`distribution.display_name` / `site:` feed backend-rendered content (transactional
emails, attribution headers) through `get_site_identity()` in
`apps/backend/serving/config/site_identity.py`. Both are gated on
`DISTRIBUTION_CONFIG_MODE=active`; in dark mode `/site-config` returns a
neutral document and identity falls back to `SITE_NAME` / `SITE_PUBLIC_BASE_URL`
/ `SITE_DOCS_URL` / `SITE_SUPPORT_EMAIL` or to neutral defaults.

Manifests must not contain secrets. Credentials stay in the environment, and
`schema_version: 1` deliberately does not interpolate environment variables into
manifest values.

## Backend extensions

`BACKEND_EXTENSIONS` is an optional comma-delimited list of trusted, local Python
module names. It is empty by default. Each module must expose a synchronous
`register()` function that takes no arguments. The gateway imports and registers
each module once per process, after loading dotenv and before constructing
runtime routes or consuming their registries. An import or registration failure
aborts startup; the gateway does not silently use a different adapter.

Extensions register factories through
`serving.servers.registry.register_adapter_factory(kind, factory, *, override=False)`.
A factory receives a configuration dictionary and returns an adapter, before
built-in provider defaults are applied. Registering an existing extension kind
is an error. Replacing a built-in kind requires `override=True` and emits a log
entry. Registered kinds also become reserved provider labels. The startup
`register()` function may populate the existing runtime-setting and provider
metadata dictionaries before their consumers run; it must mutate those shared
dictionaries rather than replace them.

The deployment must make these modules importable, for example through its
read-only overlay mount. This executes trusted server code, not user-supplied
configuration: do not derive module names from requests or allow an admin form
to choose them. The loader does not fetch remote modules or load code per
request. Import all extension code during startup, avoid later lazy imports or
live code reloads, and restart the backend when changing the mounted extension.
See [Deployment-local adapters](adding-models.md#deployment-local-adapters) for
a minimal factory example.

### Quota reporting

The gateway keeps quota reporting and quota-aware routing independent of the
service that measures usage. The public framework supplies the Providers
tab's result rows, disabled-key cards, key discovery and `gather_all`, plus
RouteWise's quota snapshots. No quota source is enabled by default. A trusted
backend extension connects an authorized usage API or the operator's own
metering service; the framework does not require a particular supplier,
website login or cookie.

An extension's `register()` calls
`serving.admin.provider_quotas.register_quota_fetcher(provider, display_name, fetch)`;
`fetch` is awaited as `fetch(operational_store, services)` and returns one
`ProviderQuotaResult` per configured key, converting its own failures into
results rather than raising. The admin endpoint supplies the store and services;
RouteWise currently calls it with `(None, None)`, so the fetcher must also work
without those objects. Registration runs at startup, not when the module is
merely imported. A fetcher queries usage; it does not send user inference
requests or change the route's inference protocol.

Each result contains `ProviderQuotaUsage` rows with `label`, `used`, `limit`,
`unit` and an optional timezone-aware `reset_at`. The deployment's provider
identifier links the source to its routes; it is not a hard-coded vendor
enum. `quota_source.provider`, `usage_label` and `unit` must exactly match a
returned result and usage row. Report the account/window shared by that quota
pool, and keep independent accounts in distinct sources. Do not sum unrelated
keys or present a failed query as zero usage.

The UI can display percentages, currencies and other units, but RouteWise's
quota admission consumes one **request** at a time and requires a compatible
count-based source. A percentage alone is not a request allowance. The source
owns window boundaries and reset times; `quota.limit` is a cross-check against
its reported limit, not a replacement measurement. A source with no successful
snapshot stays unready. A failed refresh does not manufacture a fresh balance;
an existing pool retains its last successful snapshot and local increments.

#### Run a local quota source

The complete [example extension](../../distributions/example/quota_extension.py)
reads `/usage` from the bundled fake provider. Its in-memory daily counter is
only a teaching fixture, resets on process restart or at UTC midnight, and is
not production accounting. It uses no real account or credential and is loaded
only when explicitly selected through `BACKEND_EXTENSIONS`.

After the developer setup, run these commands from the repository root in
three terminals. First, start a simulated quota provider:

```bash
uv run python distributions/example/fixtures/fake-openai-provider/server.py \
  --port 18353 --response-text ROUTED_TO_QUOTA --quota-limit 100
```

Then start a slower, priced fallback (its prices in the example are fictional):

```bash
uv run python distributions/example/fixtures/fake-openai-provider/server.py \
  --port 18352 --response-text ROUTED_TO_FALLBACK --ttft-delay-ms 400
```

Finally start the gateway with the opt-in
[quota registry](../../config/examples/models.routewise.quota.yaml). Name the
demo token once so the admin call below can reuse it:

```bash
export DEMO_ADMIN_TOKEN=local-quota-demo-only

PYTHONPATH=.:apps/backend \
  PYTHON_DOTENV_DISABLED=1 \
  BACKEND_EXTENSIONS=distributions.example.quota_extension \
  EXAMPLE_QUOTA_BASE_URL=http://127.0.0.1:18353 \
  MODELS_CONFIG_PATH=config/examples/models.routewise.quota.yaml \
  ROUTING_CONFIG_PATH=config/examples/routing.minimal.yaml \
  DB_ENABLED=false USER_AUTH_ENABLED=false ADMIN_TOKEN="${DEMO_ADMIN_TOKEN}" \
  JWT_SECRET_KEY=local-quota-demo-signing-secret-not-for-production \
  uv run uvicorn serving.servers.app:app --host 127.0.0.1 --port 18080
```

These settings disable dotenv loading and user authentication and use a public
demo-only admin token and JWT signing secret: keep the listener on loopback and
never use them for a real deployment. The admin endpoint needs the signing
secret even when authenticating with the demo token. Read the same result the
console uses:

```bash
curl http://127.0.0.1:18080/admin/provider-quotas \
  -H "Authorization: Bearer ${DEMO_ADMIN_TOKEN}"
```

Send several requests to calibrate the cost envelope and allow the first
five-second probe/snapshot cycle to finish:

```bash
curl http://127.0.0.1:18080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"quota-demo","messages":[{"role":"user","content":"hi"}]}'
```

The response identifies the selected fixture. Initial requests use the fallback
until the quota route has both a usage snapshot and cost-envelope evidence.
Accepted chat requests, including active probes, consume the mock quota;
health, model discovery and `/usage` reads do not. To simulate exhaustion,
restart only the quota fixture with `--quota-limit 100 --quota-used 100`; after
the next snapshot refresh, requests continue through the fallback. Stop each
local process with Ctrl+C when finished.

To see the card in the authenticated Web/Admin Console, configure this same
extension and registry on a local full-stack demo backend, then open
**Providers → Quotas**. Use a quota endpoint reachable from that backend:
inside Compose, `127.0.0.1` denotes the backend container, not the host. The
[Router Tutorial](router-tutorial.md) covers the full-stack demo and its
authentication. With no returned results the Quotas tab stays visible and
links back here; Overview, Keys, Availability and Performance are independent.

For your own integration, copy the extension and replace its local HTTP query
and mapping with your authorized data source. Preserve the result contract, bound network
requests with a timeout, handle failures explicitly and keep credentials out
of responses and logs. Supplier-specific authorization and service terms still
apply; using an extension is a software boundary, not an exemption from them.

## The example distribution

`distributions/example/` is a complete, runnable overlay kept in the repository
as a teaching artifact: a manifest, a one-model registry pointing at a bundled
fake provider that needs no credential, Compose overlays, and smoke scripts.
Walk through it with the [Quickstart](router-tutorial.md):

```bash
make up DISTRIBUTION=example
make smoke DISTRIBUTION=example
```

It carries a marker file, `EXAMPLE_OVERLAY`, whose presence keeps it out of
automatic discovery. The Makefile picks a distribution like this:

- Overlays are discovered from `distributions/*/deploy/*.env`, minus any
  directory containing `EXAMPLE_OVERLAY`.
- Exactly one candidate: it is selected automatically, and `make` prints which
  identity it is compiling in.
- Several candidates: `make` fails and asks you to name one.
- None (the state of a fresh clone of this repository): no overlay is selected,
  and the stack is neutral.

`DISTRIBUTION=<name>` selects one by name — including the example, which is only
ever selected explicitly. `DISTRIBUTION=none` opts out.

## What a fresh clone does with no configuration at all

Start the gateway in a clean checkout with no `MODELS_CONFIG_PATH`, no manifest
and no overlay, and the built-in default applies: it serves
`config/examples/models.openrouter.yaml`. That file registers three models — two
straight OpenRouter routes and one that puts a local OpenAI-compatible server
first with OpenRouter as automatic fallback — and every one of them needs the
same single credential.

```bash
export OPENROUTER_API_KEY=sk-or-...
uv run uvicorn serving.servers.app:app --port 8080
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
probe is a `GET` to the endpoint's origin root plus `/health`. An endpoint that
fails is excluded from grouping, so its weight goes to the surviving routes; it
returns automatically when a probe succeeds.

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
uv run uvicorn serving.servers.app:app --port 8080
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
