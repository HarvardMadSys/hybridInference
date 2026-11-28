# Glossary

The terms these pages use, roughly in the order you meet them.

## Requests and models

Model id
: The name a client sends in the `model` field, such as `example-chat`: the
  gateway's public name for a model, independent of what any provider calls it.

Alias
: Another name for the same model id, declared in the model's `aliases`.

Model registry
: The `MODEL_CATALOG` setting: the database document that lists every model id
  and its routes. See [Configuration](configuration.md).

API key and provider key
: An API key is what a client presents to the gateway; a provider key is what
  the gateway presents to a provider. They are never the same credential.

## Routes and providers

Route
: One way of serving a model id: an entry in the model's `route` list naming an
  adapter kind, a base URL, a credential and a weight. Several routes let the
  gateway balance and fail over between them.

Endpoint
: The server a route sends requests to. "Endpoint" means the server, "route"
  the configuration entry.

`endpoint_id`
: The key for one endpoint of one model, `<model-id>:<location>`:
  `glm-4.6:local-12003` for local port 12003, `glm-4.6:zai-api` for a hosted
  API. Circuit breakers, latency profiles and weight overrides are keyed on it.
  The suffix comes from the host and port or the adapter kind, so it does not
  tell you who runs the server.

Provider
: The service behind a route: OpenRouter, DeepSeek, your own vLLM server. A
  route's `provider` is the label its traffic is reported under.

Kind
: A route's `kind`, which picks its adapter: `openai_compat`, `vllm`,
  `openrouter`, `anthropic` and so on.

Adapter
: The code that speaks one provider's API: it builds the request, holds the
  provider key and turns the response into the OpenAI shape. Self-hosted
  servers use the generic OpenAI-compatible adapter.

Upstream
: Whatever the gateway calls to produce an answer: the far end of a route.

## Choosing a route

Router
: The per-model logic, chosen with `router`, that picks a route for each
  request. `fixed` (the default) draws a route by weight and falls back to the
  others on failure; `routewise` trades latency against cost.

Weight
: A route's relative share of traffic under `fixed`. A route with weight `0`
  stays configured but gets no traffic, not even as a fallback.

Fallback
: Trying the model's remaining routes in order when a route fails.

Circuit breaker
: A per-endpoint switch that stops traffic to an endpoint after repeated
  failures and lets one test request through after a cooldown.

Offload route
: A route reserved for the requests a model's other routes cannot start in
  time. See [Queue-wait offload](routing.md#queue-wait-offload).

Routing file
: The optional `ROUTING_POLICY` setting: the default router, health-probe
  settings and a local/remote weight split. See
  [The routing file](configuration.md#the-routing-file).

## Accounts and sign-in

Single sign-on (SSO)
: Signing in to the console with an account an organization manages, instead
  of a password kept by the gateway. See [Single Sign-On](sso.md).

Identity provider
: The organization's system that signs its members in and tells the gateway,
  over OpenID Connect (OIDC), who they are.

Link
: The record that lets an identity at an identity provider sign in to one
  account. An account can hold links to several identity providers.

## Deployments

Distribution
: One deployment's code, assets and Compose settings, kept in
  `distributions/<name>/`. Its application settings (identity, branding, model
  catalog) live in the database. `distributions/example/` is the tutorial.

Overlay
: The `distributions/<name>/` directory itself, mounted over the neutral image
  instead of built into it.

Manifest
: The retired configuration file `distributions/<name>/distribution.yaml`. Its
  settings now live in Admin's `SITE_CONFIGURATION` document and startup no
  longer reads it; `python -m serving.config.manage import-legacy` imports an
  old one once.

Dry run and active
: The modes of the retired `DISTRIBUTION_CONFIG_MODE` setting: `active`
  applied a manifest, and the dry run (`dark`) only compared it.

Operational store
: The Postgres tables that hold accounts, API keys and everything changed in
  the admin console.

Setting
: A value the gateway reads by name, such as `SMTP_PASSWORD` or a `${VAR}` the
  model registry names. With a database, settings are stored there and edited
  on the admin console's Settings tab; the environment only supplies starting
  values. See
  [Settings stored in the database](configuration.md#settings-stored-in-the-database).

Live group
: Settings the gateway applies together without a restart once an operator
  activates the group: every backend process validates the set, switches to it
  and reports that it has. `alerts.runtime` is currently the only one. See
  [Settings that apply without a restart](configuration.md#settings-that-apply-without-a-restart).

First-run setup
: How a new deployment gets its first administrator: the backend logs a
  one-time setup code, and whoever enters it on the console's `/setup` page
  creates the account. See [First-run setup](installation.md#first-run-setup).

Runtime model
: A model created in the admin console instead of the model registry. It
  exists only in the operational store.

Console
: The Next.js web app: sign-up and sign-in, the user dashboard, the playground
  and the admin console. It also forwards API paths to the gateway; see
  [The public path table](public-path-table.md).

Site UI module
: A distribution's own React components for the public pages, compiled into
  its console image. See [Site UI Modules](site-ui-modules.md).

Backend extension
: A trusted Python module loaded at startup to add an adapter kind, a Cloud
  Agent access rule or a quota source. See
  [Backend Extensions](backend-extensions.md).
