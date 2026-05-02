# OpenRouter as Upstream Backend

The gateway can route requests through [OpenRouter](https://openrouter.ai) as an
upstream provider, either as the primary route for a model or as a fallback leg
on an existing model.

## Configuration

Set `OPENROUTER_API_KEY` in `.env` (single account-level key) and add a route
entry in `config/models.yaml` with one of two `kind` forms.

### Bare `kind: openrouter`

Lets OpenRouter pick the upstream provider freely (lowest cost / best
availability per their default policy).

```yaml
- kind: openrouter
  weight: 1.0
  base_url: https://openrouter.ai/api/v1
  api_key: ${OPENROUTER_API_KEY}
  provider_model_id: meta-llama/llama-3.3-70b-instruct
```

### Bracket form `kind: openrouter[<provider_slug>]`

Pins the request to one specific OpenRouter upstream via
`provider.order=[<slug>]` with `allow_fallbacks=false`.

```yaml
- kind: openrouter[deepinfra]
  weight: 1.0
  base_url: https://openrouter.ai/api/v1
  api_key: ${OPENROUTER_API_KEY}
  provider_model_id: meta-llama/llama-3.3-70b-instruct
```

The slug must match `[A-Za-z0-9_.-]+`. See
<https://openrouter.ai/docs/features/provider-routing> for the full list of
provider slugs.

When two route legs share a `base_url` but use different pinned providers,
they get distinct `endpoint_id`s — circuit-breaker stats stay isolated per
upstream.

## What the adapter sends

On every request, `OpenRouterAdapter` injects:

- Headers: `HTTP-Referer: https://freeinference.org`, `X-Title: FreeInference`.
- Body: `usage: {include: true}` so OpenRouter returns per-request `cost`.
- For streaming requests: `stream_options: {include_usage: true}`.
- For bracket-form routes: `provider: {order: [<slug>], allow_fallbacks: false}`.

## Cost logging

End-user billing is unchanged: `api_logs.cost_usd` continues to hold
`tokens × model-level pricing`.

The OpenRouter-reported `cost` is logged separately to a new
`api_logs.upstream_cost_usd` column. It is `NULL` for non-OpenRouter routes
and `NULL` for OpenRouter routes when the upstream provider failed to report
cost (rare).

## Error handling

OpenRouter responses are dispatched through the standard OpenAI-compatible
error path:

| HTTP | Behavior |
|------|----------|
| 400 / 403 | Propagated to the client; no fallback (deterministic failure). |
| 401 / 402 | Logged at error level; routed to the next leg in the route's fallback chain. |
| 408 / 429 / 502 / 503 / 524 | Logged at warning level; routed to the next leg. |

There is no special multi-key rotation for OpenRouter — a single account-level
`OPENROUTER_API_KEY` is used.

## Specs and design history

- Design spec: `docs/superpowers/specs/2026-05-02-openrouter-upstream-design.md`
- Implementation plan: `docs/superpowers/plans/2026-05-02-openrouter-upstream.md`
