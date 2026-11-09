# Verification: route reasoning-effort map

Date: 2026-11-08. Design: [2026-11-08-route-reasoning-effort-map-design.md](../agents/specs/2026-11-08-route-reasoning-effort-map-design.md).
Scope: local only, against synthetic upstreams on localhost. No deployment was
contacted; this is not live deployment acceptance.

## Repository gates

On `b0dc31c5` (clean tree, Python 3.13, Node 22): `make lint`; `make test`
(12276 passed, 5 skipped); `make frontend-check` (2420 console tests);
`make docs-verify`; `make test-db-local` (966 passed, including the JSONB
order round trip).

## Running acceptance

A real gateway process (`uvicorn serving.servers.app:app`) on a fresh, migrated
local test database, with the models document imported into the database
route catalog, four fake OpenAI-compatible upstreams on localhost that record
every request body, and the console served by `next dev`. The test route is
configured like the intended deployment: no `reasoning_effort` in
`supported_params`, no `reasoning_efforts`, and
`reasoning_effort_map: {low: high, high: xhigh, max: max}`.

| Check | Result |
|---|---|
| Chat Completions, Responses, Messages; unary and stream; `low`/`high`/`max` | upstream bodies carried `high`/`xhigh`/`max` (18/18) |
| `xhigh`, `medium`, `none` on the mapped route, every surface and mode | 400 naming `low, high, max`, nothing sent upstream (18/18); breaker logged `client_error_skip_breaker` |
| No level (Auto), every surface and mode | no `reasoning_effort` in the upstream body (6/6) |
| Unmapped routes | Chat passed `medium` unchanged; Messages and an undeclared route dropped it, as before |
| Fallback (first route answers 500) | both attempts carried `high` for public `low`: translated once per attempt |
| `/v1/models` both shapes | `low, high, max` for mapped models; unknown and "none" kept distinct |
| Admin: set map, malformed map (422), connection-only edit, four-value save, explicit `null`, reset | 20/20; requests followed each change at once |
| Restart | console map restored in order; an override row without the field kept the models.yaml map |
| Console in a browser | rows saved and reloaded in order, "Offered to clients now: low, high, max", Restore defaults cleared it (5/5) |

Two defects were found by this run and fixed before the gates above: a JSONB
object reordered the stored map (`low, max, high`), and a capability save that
omitted the map dropped a console-set one. See the design record.

## Limits

- One gateway process. Other processes apply an admin change at their next
  boot (the existing capability-override reload model).
- The browser run showed React hydration warnings at page load on the login
  and dashboard pages, before any route editing; they are not from this change.
- The upstream is a fake; the real server's response to each value was taken
  from the supplied captures, not re-measured here.
