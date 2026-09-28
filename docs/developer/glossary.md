# Glossary

The words these pages use for the parts of a gateway, in the order you meet
them. When a page uses one of these terms, it means exactly what is written
here.

## Requests and models

Model id
: The name a client sends in the `model` field, such as `example-chat`. It is
  the gateway's public name for a model and is independent of what any
  provider calls it.

Alias
: Another name that resolves to the same model id. Declared with `aliases:` on
  the model.

Model registry
: The YAML file, usually `models.yaml`, that lists every model id and the
  routes behind it. [Configuration](configuration.md) explains how the gateway
  finds it.

API key and provider key
: An API key is what a client presents to the gateway. A provider key is what
  the gateway presents to a provider. They are never the same credential.

## Routes and providers

Route
: One way of serving a model id: an entry in the model's `route:` list that
  names an adapter kind, a base URL, a credential and a weight. A model id can
  have several routes, and that is what lets the gateway balance and fail over
  between them.

Endpoint
: The server a route sends requests to. Pages say "endpoint" when they mean
  the server and "route" when they mean the configuration entry.

`endpoint_id`
: The gateway's key for one endpoint of one model, written
  `<model-id>:<location>` — `glm-4.6:local-12003` for a server on local port
  12003, `glm-4.6:zai-api` for a hosted API. Circuit breakers, latency
  profiles and weight overrides are all keyed on it. The suffix comes from the
  host and port or from the adapter kind, so it does not tell you who runs the
  server.

Provider
: The service behind a route: OpenRouter, DeepSeek, your own vLLM server. In
  configuration, a route's `provider:` is the label its traffic is reported
  under in logs and dashboards.

Kind
: A route's `kind:`, which picks the adapter that talks to the endpoint:
  `openai_compat`, `vllm`, `openrouter`, `anthropic` and so on.

Adapter
: The code that speaks one provider's API. It builds the request, holds the
  provider key and turns the response back into the OpenAI shape. Self-hosted
  servers use the generic OpenAI-compatible adapter.

Upstream
: Whatever the gateway calls to produce an answer — the far end of a route.

## Choosing a route

Router
: The per-model logic that picks a route for each request. `fixed` draws a
  route at random by weight and falls back to the others on failure; it is the
  default. `routewise` trades latency against cost. A model chooses one with
  `router:`.

Weight
: A route's relative share of traffic under the `fixed` router. A route with
  weight `0` stays configured but receives no traffic, not even as a fallback.

Fallback
: What the router does when a route fails: it tries the model's remaining
  routes in order until one answers.

Circuit breaker
: A per-endpoint switch that stops sending traffic to an endpoint after
  repeated failures and lets a single test request through after a cooldown.

Offload route
: A route set aside for the requests a model's other routes cannot start in
  time. See [Queue-wait offload](routing.md#queue-wait-offload).

Routing file
: The optional `routing.yaml`. It holds health-probe settings and a
  local/remote weight split; see [The routing file](configuration.md#the-routing-file)
  for what it does and does not change.

## Deployments

Distribution
: One deployment's own files — its manifest, model registry, routing file,
  branding and Compose settings — kept together in `distributions/<name>/`
  instead of in the source. `distributions/example/` is the one this
  repository ships.

Overlay
: The `distributions/<name>/` directory itself. It is called an overlay
  because it is mounted over the neutral image rather than built into it.

Manifest
: `distribution.yaml`, the file in a distribution that names it and says where
  its configuration files are.

Dry run and active
: The two modes of `DISTRIBUTION_CONFIG_MODE`. In a dry run — the default,
  written `dark` in the setting — the gateway reads and checks the manifest
  and logs what it would change, but uses nothing from it. `active` applies
  it.

Operational store
: The Postgres tables that hold accounts, API keys and everything changed from
  the admin console.

Runtime model
: A model created from the admin console instead of the model registry. It
  exists only in the operational store.

Console
: The Next.js web app: sign-up and sign-in, the user dashboard, the
  playground and the admin console. It also forwards API paths to the
  gateway; see [The public path table](public-path-table.md).

Site UI module
: A distribution's own React components for the public pages, compiled into
  its console image. See [Site UI Modules](site-ui-modules.md).

Backend extension
: A trusted Python module a deployment loads at startup to add an adapter, an
  agent-access rule or a quota source. See
  [Backend Extensions](backend-extensions.md).
