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
is the annotated reference; the shape is:

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
get a warning telling you the mode defaulted to `dark`. Any unrecognised mode
value also degrades to `dark` with a warning, so a typo can only suppress a
planned activation, never cause one. Enable a manifest by running dark first,
reading the comparison lines, and only then setting `active`.

Failure behaviour differs by mode, on purpose. In dark mode a manifest that will
not load is logged and skipped. In active mode the manifest *is* where the paths
come from, so a manifest that will not load — a lost overlay mount, a YAML error
— refuses to start rather than quietly serving a different registry.

### Identity, and what the manifest must not contain

`site:` and `features:` are served as a public subset by `GET /site-config`, and
`distribution.display_name` / `site:` feed backend-rendered content (transactional
emails, attribution headers) through `get_site_identity()` in
`apps/backend/serving/config/site_identity.py`. Both are gated on
`DISTRIBUTION_CONFIG_MODE=active`; in any other mode `/site-config` returns a
neutral document and identity falls back to `SITE_NAME` / `SITE_PUBLIC_BASE_URL`
/ `SITE_DOCS_URL` / `SITE_SUPPORT_EMAIL` or to neutral defaults.

Manifests must not contain secrets. Credentials stay in the environment, and
`schema_version: 1` deliberately does not interpolate environment variables into
manifest values.

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

**The adapter kinds are defined in one place.** The dispatch in
`_make_adapter` (`apps/backend/serving/servers/registry.py`) is the source of
truth. At this revision it accepts:

| Kind | Adapter |
|---|---|
| `openai_compat`, `staging`, `vllm`, `sglang`, `ollama`, `chutes`, `featherless`, `cliproxy`, `deepseek`, `kimi`, `minimax` | `OpenAICompatAdapter` |
| `kimi_coding`, `zai` | `CodingIdentityAdapter` |
| `openrouter`, `openrouter[<slug>]` | `OpenRouterAdapter` |
| `claude` | `ClaudeAdapter` |
| `gemini` | `GeminiAdapter` |
| `anthropic` | `AnthropicAdapter` |

Local inference servers have no dedicated adapter: `vllm`, `sglang` and `ollama`
are OpenAI-compatible kinds that differ only in their provider label and usage
handling. Anything else raises `ValueError: Unknown adapter kind: <kind>` at
startup. To re-derive the list from the code rather than trusting this table:

```bash
sed -n '/def _make_adapter/,/Unknown adapter kind/p' apps/backend/serving/servers/registry.py
```

A `grep` for `if kind` misses most of it: the OpenAI-compat arm is a single
`if kind in (` followed by the eleven names on their own lines, so none of them
appear in the output.

The neighbouring `RESERVED_PROVIDER_LABELS` set in the same module is a
different, larger list — the labels a route may not borrow as a custom
`provider:` — and includes names such as `openai` and `router` that are *not*
adapter kinds.

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
Its alias `GET /admin/routing` carries no admin dependency either at this
revision, unlike the rest of `/admin/*`, and additionally reports routes that
`/routing` hides. Block both at your reverse proxy on any gateway reachable from
the internet, and treat their output as sensitive.
```
