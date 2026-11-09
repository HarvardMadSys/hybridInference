# Route reasoning-effort map: public levels, exact upstream values

Date: 2026-11-08. Status: implemented on `codex/fix/model-effort-capabilities`
(continues the route-derived capability listing of the same branch).
Verification: [docs/reviews/2026-11-08-route-reasoning-effort-map-verification.md](../../reviews/2026-11-08-route-reasoning-effort-map-verification.md).

## Problem

A client chooses a reasoning level by name; a server takes its own names. A
self-hosted reasoner can take `low/high/xhigh/max` while the levels an operator
wants to offer are three of them, under the names the model's official service
uses. Before this change a route could only pass the client's value through
unchanged (`reasoning_effort` in `supported_params`, optionally a
`reasoning_efforts` domain), and Messages dropped anything undeclared. There
was no way to offer level A and send value B, and nothing refused a level the
route did not take.

## Decision

Two vocabularies, kept apart:

- **public level** -- what a client sends (`reasoning_effort`,
  `reasoning.effort`, `output_config.effort`) and what `/v1/models` publishes;
- **upstream value** -- what the route's server receives as `reasoning_effort`.

A route may declare `reasoning_effort_map: {public: upstream, ...}`
(`serving/adapters/reasoning_effort.py`). It is a strict, self-contained
contract for that route only:

1. Its keys, in order, are the only public levels the route takes and
   publishes. Any other level is a 400 (`ReasoningEffortRefused`, an
   `aiohttp.ClientResponseError` like `RemoteMediaRefused`) before any I/O:
   the breaker treats it as a client error, and nothing is dropped or rounded.
2. Translation happens once, in `OpenAICompatAdapter`, from the public value
   the request still carries, after protocol normalization (Responses and
   Messages are already Chat parameters by then). The adapter reads the route's
   contract once per request, before its first await, so a concurrent admin
   edit cannot split validation from translation. `params` are never
   rewritten, so retries, fallback, hedging and reroutes each translate from
   the client's level with the serving route's own map.
3. The map alone makes the route forward the parameter; `supported_params` and
   `reasoning_efforts` are not consulted on a mapped route. Every reader of a
   route's effort domain (listings, Messages, agent catalog, batch admission,
   Auto) goes through `effort_contract` / `declared_public_levels`.
4. Messages refuses an undeclared level on a mapped route with a 400 before the
   endpoint is claimed, so a stream is refused with an HTTP status rather than
   in-band; on unmapped routes it keeps dropping undeclared values.
5. Route-level only (a model-level map is a load error), and only on an adapter
   with `forwards_reasoning_effort` (native Anthropic, Vertex Claude and Gemini
   refuse one).

No level vocabulary, tier or mapping is built in, and nothing is inferred from
a model name. A request that names no level sends none: the server's default
applies. Unmapped routes are unchanged; unknown (no declared domain) stays
distinct from "no levels" (parameter not forwarded).

## Admin

The map is one more, optional, field of the route capability override
(`RuntimeModelCapabilities.reasoning_effort_map`), on the existing PUT/DELETE
capability endpoints and route-edit bodies. Absent keeps the route's current
effort setting (its console map if one is set), so a connection-only edit or a
four-value save never clears it; a row stored before the field existed has
none and inherits the route's own (models.yaml or creation) map; `null` returns
to that map; a map sets it. Saving a map never touches the four or
`supported_params`. Stored rows keep the map as ordered `[public, upstream]`
pairs, because a JSONB object reorders keys (found by the running acceptance:
`low, high, max` came back as `low, max, high`); the object form is accepted
everywhere else. The baseline in
`route_metadata.capability_baseline` carries the map, so reset and target
rebuilds restore it. A runtime route never clones another route's map. Saves
are confirmed against the live adapter (409 otherwise). The console shows a
flat "Reasoning effort levels" row editor and the levels the live route
publishes. Application follows the existing capability-override reload model:
the handling process applies at once, other processes at their next boot.

## Generic example

```yaml
route:
  - kind: vllm
    base_url: ${EXAMPLE_REASONER_BASE_URL}
    reasoning_effort_map:
      low: high
      high: xhigh
      max: max
```

Clients are offered `low`, `high`, `max`; the server receives `high`, `xhigh`,
`max`. `xhigh`, `medium` or `none` from a client is a 400 on this route.

## Deferred

- No console control to *remove* a models.yaml map (edit the document).
- No cross-process propagation beyond the existing boot-time sync.
- Numeric efforts via `chat_template_kwargs` are out of scope by design.
