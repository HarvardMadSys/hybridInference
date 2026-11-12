# The Public Path Table

HybridInference ships two long-running HTTP services: the FastAPI gateway
(`apps/backend`) and a Next.js console (`apps/frontend`). Only one of them needs
to be reachable from the internet.

**`deploy/public-paths.json` is the public path table.** It lists every public
path that is not one of the console's own pages, and what serves it. The
console is its default implementation: it is the process that terminates
public traffic, and it forwards the gateway's paths through App Router route
handlers, apart from a few that only answer `GET`, which are static rewrites
in the `rewrites()` configuration in `apps/frontend/next.config.js`; the cloud
agent and pgAdmin have route handlers of their own. Anything not named by
either mechanism is served by the console's own pages.

A deployment's entry — its CDN, tunnel or load balancer — may instead send the
paths the manifest marks `entry_split`, today `/v1` and `/anthropic`, straight
to the gateway, so that a console release or restart no longer interrupts
inference requests. That is the deployment's choice and is made in its own
entry; this repository ships no entry configuration. The console keeps
forwarding those prefixes either way, and removing the entry's rules sends
the traffic back through it. The conditions such an entry has to meet are in
[Splitting inference traffic at the entry](deployment.md#splitting-inference-traffic-at-the-entry).

So exposing the console's port exposes every gateway path in the table below.
Outside the split prefixes, publishing a new path means adding it to the
manifest and editing `next.config.js` or adding a route handler — not changing
the reverse proxy or tunnel in front. Under a split prefix, every gateway
route is already public on both topologies, since the console relays the
whole prefix too: a new route there needs no entry change either. Only adding
or moving a split prefix changes an entry, and the
[probe](deployment.md#checking-an-entry-split-with-the-probe) has to be run
again when it does. A path missing from both the handlers and the rewrites gets
the console's HTML 404 page, which an API client will read as "the gateway is
down" rather than "this path is not forwarded".

```text
client ──▶ (your edge: CDN / tunnel / reverse proxy)
              │   └── optional: /v1, /anthropic ──────────▶ FastAPI gateway
              ▼
        Next.js console ──┬──▶ FastAPI gateway   (route handlers; rewrites for a few GETs)
                          ├──▶ cloud agent       (runtime route handler)
                          ├──▶ pgAdmin           (runtime route handler, admin-gated)
                          └──▶ its own pages     (everything else)
```

## The manifest

Each entry of `deploy/public-paths.json` has four fields:

| Field | Meaning |
|---|---|
| `path` | The public path. A segment written `:name` stands for any one segment: `/internal/users/:userId/status` |
| `match` | `prefix`: the path itself or anything below it, so `/v1` covers `/v1` and `/v1/...` but not `/v10`. `exact`: the path alone. Either is judged after dot segments are resolved, as the console's `isUnder()` judges it |
| `owner` | What serves it: `gateway`, `console` (a route handler of the console's own, such as `/healthz` and `/site-assets`), `cloud-agent` or `pgadmin` |
| `entry_split` | `true` when a deployment's entry may send the path straight to the gateway. Only `/v1` and `/anthropic` are |

A gateway path that is not in the manifest must not be reachable from the
internet — FastAPI's `/docs`, `/redoc` and `/openapi.json`, `/health/deep`,
`/health/ready`, `/health/model-activity` and every `/internal` route not
listed. On the console's topology nothing forwards them. An entry that splits
off a prefix has to hold the same line (rules E1 and E7 in the deployment
guide).

The console's routing cannot read the manifest: route handlers are files,
`rewrites()` and `redirects()` are compiled at build time, and the
middleware's `matcher` must be a literal string, because Next reads it without
running the module. Tests hold each of them to the manifest instead, and fail
when one changes without the others:

- `apps/frontend/next.config.public-paths.test.ts` checks that every gateway
  entry is served by exactly one route handler or one rewrite, that the
  middleware's `matcher` leaves out exactly the manifest's first segments, and
  that the trailing-slash rule in `redirects()` covers exactly the segments it
  should. `/healthz` is the one manifest path the middleware still sees.
- `apps/frontend/src/lib/server/gatewayRoutes.test.ts` checks its own list of
  gateway route files against the manifest.
- `tests/unit/middleware/test_trailing_slash.py` checks that the gateway's own
  trailing-slash redirect covers exactly the `entry_split` prefixes.

The rewrites, the redirect rule and the matcher work per first segment
(`internal`, not `/internal/verify-admin`); which `/internal` paths are
published is decided by the route files, one per path.

## The path table

Two of the manifest's paths are the console's own and reach nothing behind
it: `/healthz`, the container health check (`src/app/healthz/route.ts`), which
answers `200 ok` without asking the gateway, and `/site-assets`, the Site UI
module's images, served from files packaged into the console image
(`src/app/site-assets/[[...path]]/route.ts`). `/health` is the gateway's
check, forwarded.

The table below combines the static rewrites with the filesystem route
handlers. `apps/frontend/next.config.js` is the source of truth for rewrites;
the matching `route.ts` file is the source of truth for a runtime handler.

Destination resolution is deliberately split. Next resolves `rewrites()` at
**build** time and writes them into `.next/routes-manifest.json`. Route handlers
read their server-only environment at request time, so changing those targets
requires recreating the container, not rebuilding the image.

| Variable | Default | Resolution | Points at |
|---|---|---|---|
| `BACKEND_INTERNAL_URL` | `http://backend:8080` | build time for rewrites and server-side backend calls | the FastAPI gateway |
| `AGENT_WEB_INTERNAL_URL` | *(unset)* | runtime | the standalone cloud agent's web app |
| `AGENT_CONTROL_PLANE_INTERNAL_URL` | *(unset)* | runtime | that agent's control-plane API |
| `PGADMIN_INTERNAL_URL` | `http://pgadmin:80` | runtime | pgAdmin |

`BACKEND_INTERNAL_URL` is fixed when the console image is built, not when the
container starts. The same value is compiled into both the rewrite manifest and
the server-only `BUILT_BACKEND_INTERNAL_URL` used by the gateway's route
handlers, `/site-config` and pgAdmin's admin check, so a runtime override
cannot quietly send only some backend requests somewhere new. The published
image uses `http://backend:8080`, so a deployment that runs it must give the
backend that network name. A different backend address means rebuilding the
console.

### Runtime route handler — the cloud agent proxy

`apps/frontend/src/app/agents/[[...path]]/route.ts` reads both agent targets on
every request. With either variable unset, `/agents` truthfully answers 404;
an invalid target or an unreachable configured service answers 502.

For an agent served on its own hostname, set `AGENT_PUBLIC_URL` to its public
HTTPS URL (without credentials). The dashboard's Agents tile uses that URL
at runtime, independently of the proxy targets; `/agents` remains disabled
when either proxy target is unset. With no public URL, the tile links to
`/agents` only when both proxy targets are configured. The tile remains
restricted to internal users. Unlike the private targets, the public URL is
included in the browser's site configuration.

| Source | Destination | Note |
|---|---|---|
| `/agents/api/:path*` | `${AGENT_CONTROL_PLANE_INTERNAL_URL}/:path*` | prefix **stripped** — the control plane serves its routes at its own root |
| `/agents/:path*` | `${AGENT_WEB_INTERNAL_URL}/agents/:path*` | prefix **kept** — that app is built with `basePath=/agents` and generates links carrying it |
| `/agents` | `${AGENT_WEB_INTERNAL_URL}/agents` | bare prefix |

Three details matter here:

- `/agents/api/:path*` is matched before `/agents/:path*`. The first is a prefix
  of the second, so with the order reversed every API call is answered with the
  web app's HTML.
- The handler streams request and response bodies, including SSE, but it cannot
  accept WebSocket upgrades. A future WebSocket endpoint needs an upgrade-aware
  proxy in front of Next.js.
- Hop-by-hop headers are removed, and redirects that name either internal
  agent service are folded back through its public prefix. The agent's own
  cookies pass in both directions; the console's session cookie is never
  forwarded to it. A cookie the agent sets under one of the console's names is
  dropped, and the rest are confined to `/agents` (a `__Host-` cookie keeps the
  `Path=/` browsers require of it).

### Route handlers — the gateway

Every gateway path that takes a request body is forwarded by a route handler
rather than a rewrite. A rewrite copies the body: Next holds a copy of every
rewritten request body in memory until the request ends, and stops copying at
`middlewareClientMaxBodySize`, so a rewrite either cuts a large body short or
lets any caller pin that much of the console's memory per request. A route
handler streams the body to the gateway as it arrives and holds none of it.

| Source | Handler | Serves |
|---|---|---|
| `/v1/:path*` | `src/app/v1/[[...path]]/route.ts` | the OpenAI-compatible API surface |
| `/anthropic/:path*` | `src/app/anthropic/[[...path]]/route.ts` | the Anthropic Messages surface |
| `/auth/:path*` | `src/app/auth/[[...path]]/route.ts` | authentication routes |
| `/user/:path*` | `src/app/user/[[...path]]/route.ts` | the user dashboard API, batch input file uploads and downloads included |
| `/admin/:path*` | `src/app/admin/[[...path]]/route.ts` | the admin API |
| `/internal/playground/:path*` | `src/app/internal/playground/[[...path]]/route.ts` | the admin-only model playground the console's dashboard calls |
| `/internal/verify-admin` | `src/app/internal/verify-admin/route.ts` | cookie-session admin check (used by the pgAdmin handler) |
| `/internal/model-catalog` | `src/app/internal/model-catalog/route.ts` | model catalog read |
| `/internal/users/:userId/status` | `src/app/internal/users/[userId]/status/route.ts` | single-user status read |
| `/internal/users/:userId/agent-access` | `src/app/internal/users/[userId]/agent-access/route.ts` | whether one user may use the Cloud Agent, read by the standalone agent (see [Backend Extensions](backend-extensions.md#cloud-agent-access)) |
| `/internal/agent-grants` | `src/app/internal/agent-grants/[[...path]]/route.ts` | mint an inference grant (`POST`) |
| `/internal/agent-grants/:path*` | `src/app/internal/agent-grants/[[...path]]/route.ts` | renew / usage / revoke for one grant |
| `/internal/alert-history/events` | `src/app/internal/alert-history/events/route.ts` | alert-history ingestion from external sources (`POST`; scoped writer token, `404` while off — see [Alert history](alert-history.md)) |

Note on `/internal`: each published path has a route file of its own rather
than one handler for all of `/internal`, and that is deliberate. The prefix is
shared — `/internal/verify-admin` authenticates a browser session by cookie —
so a catch-all would publish whatever route lands under `/internal` next
without anyone deciding it should be reachable from outside.
`/internal/agent-grants` is the one exception granted a whole sub-prefix,
because every route on that router carries a dispatch-token dependency at the
router level and so is authorized by construction.

Each handler forwards to `${BACKEND_INTERNAL_URL}` plus the same path, provided
that path, once its dot segments are resolved, is still under the handler's
own prefix; anything else is answered with a 404. `BACKEND_INTERNAL_URL` is the
address fixed at build time, the same one the rewrites use. The code that does
the forwarding is `src/lib/server/gatewayProxy.ts`. A rewrite would also fail
these paths' responses, in three ways:

- When the gateway drops a connection after the response has started (a crash,
  a restart or a deploy), Next's rewrite proxy never ends the caller's
  response. The caller then waits until something in front of the console
  gives up.
- The rewrite proxy ends a response after 30 seconds with no data. The handler
  sets no deadline of its own: the gateway limits every request itself, and
  streaming responses send a keepalive every 15 seconds. It only closes a
  connection that has carried nothing in either direction for an hour.
- A failure comes back as Next's plain-text 500. The handler answers a
  gateway it cannot reach with a JSON 502.

The handler sends the gateway the same request the rewrite did. Every request
header is forwarded except the hop-by-hop ones, `Host` names the gateway, and
`X-Forwarded-Host` carries the caller's host. A body's framing is the
handler's own: it declares the length of the body it actually sends, or sends
it chunked, never a length the caller declared for a body Next did not pass
on. The forwarding headers travel exactly as the caller sent them; the handler
adds no address of its own, because a route handler cannot see the
connection's once the caller has sent `X-Forwarded-For`. When the caller sent
no `X-Forwarded-For`, `-Proto` or `-Port`, Next fills them in from the
connection. Before trusting any of them, see
[Behind the console](trusted-proxies-and-client-ips.md#behind-the-console).
When the caller hangs up, the gateway's request is cancelled at once.

### Legacy `beforeFiles` compatibility

`next.config.js` still emits the same three rules as `beforeFiles` rewrites when
both agent URLs are supplied **while building** the image, as some older
distribution pipelines do. This repository's Compose does not pass them, and
its images use the runtime handler above. Because `beforeFiles` wins over a
filesystem route, an image built with those values keeps its baked-in targets
and cannot be retargeted by changing only the container environment. Such an
image also keeps `middlewareClientMaxBodySize` at 32 MiB, because those
rewrites carry the agent's request bodies.

### `afterFiles` — the gateway

Every destination below is `${BACKEND_INTERNAL_URL}` plus the same path. These
paths only answer `GET`, so a rewrite has no body to copy; of whatever a caller
sends anyway, Next keeps at most `middlewareClientMaxBodySize` (16 KiB).

| Source | Serves |
|---|---|
| `/health` | health check |
| `/site-updates` | the public site banner |
| `/site-config` | public deployment identity, consumed by the console's `SiteConfigProvider` |
| `/public-stats` | the daily usage snapshot behind the public `/stats` page |

## Why `/pgadmin` is a route handler and not a rewrite

This section is for contributors who need to write a route handler of their
own; the pgAdmin proxy is the worked example.

pgAdmin must only be reachable by an admin. **A rewrite cannot authenticate** —
it is a static mapping evaluated before any of your code runs, with no way to
call out, inspect a session, or refuse. So `/pgadmin` is not in the table at
all. It is an app route,
`apps/frontend/src/app/pgadmin/[[...path]]/route.ts`, which is a filesystem
route and therefore wins over `afterFiles` rewrites. The handler proxies to
pgAdmin itself, after checking the caller.

The handler is a compact worked example of an authenticated reverse proxy in a
Next.js route handler, and every one of its decisions generalizes:

- **Fail closed.** `verifyAdmin()` calls `GET /internal/verify-admin` on the
  gateway with the caller's cookie and a 5-second timeout. Only an explicit
  `200` admits. A backend that is down, slow, or answering something unexpected
  denies. It assumes it is the only thing standing in front of a database
  console, because it cannot tell whether pgAdmin has its own login (that
  depends on `PGADMIN_CONFIG_SERVER_MODE`, which a deployment can set to
  `False`).
- **Strip the console's own session cookie** before forwarding. pgAdmin has no
  use for it, and forwarding a session credential to a proxied app is how those
  leak.
- **Drop hop-by-hop headers** (RFC 9110 §7.6.1) plus `host` and
  `content-length`, which `fetch` derives from the outgoing request. Drop
  `content-encoding` on the way back, because `fetch` already decompressed the
  body. Re-split `Set-Cookie` with `getSetCookie()` — `Headers.forEach` folds
  repeated values into one string.
- **Keep the app's cookies to the app.** It shares the console's origin, so a
  cookie it sets on `/` reaches every console and API request, and one named
  like the console's replaces it. `scopeProxiedSetCookie()` drops the second
  kind and confines the first to `/pgadmin`, without a `Domain`.
- **Fold upstream-absolute redirects back to bare paths.** When a request
  arrives without a trailing slash a route requires, Werkzeug builds an absolute
  redirect from the `Host` it saw — here the internal container name, which
  resolves nowhere in a browser. `foldUpstreamRedirect()` rewrites those to a
  path, matching on hostname rather than origin so it holds whatever port is on
  the URL.
- **Redirect with a bare path, not an absolute URL.** Behind a tunnel the app
  sees its own bind address as `Host`, so `new URL('/login', request.nextUrl)`
  renders as `https://0.0.0.0:3001/login` and strands the browser. The handler
  writes the `Location` header by hand, since `NextResponse.redirect()` accepts
  only absolute URLs.

### The trailing-slash interaction

`skipTrailingSlashRedirect: true` is set globally in `next.config.js`, because
proxied applications own their path semantics. Next normally redirects a
trailing slash away; pgAdmin (Flask) or the agent web app may add one back. Left
on, the two layers can bounce a browser between them forever. A proxied path has
to reach its upstream exactly as the browser asked for it.

Skipping the redirect everywhere would change every other URL on the site, so
`apps/frontend/src/middleware.ts` reimplements the redirect for everything
*except* the `/pgadmin` and `/agents` prefixes — a 308 to the slash-less path,
built from `new URL(request.url)` rather than `nextUrl.clone()` (a cloned
`NextURL` remembers the incoming trailing slash and re-serializes it,
redirecting the request to exactly where it already is).

The gateway's paths do not pass through the middleware (it would copy their
bodies), so a gateway path that ends in a slash gets its redirect from
`redirects()` in `next.config.js`: a `308` to the same path without its last
slash, as a bare path. Under the `entry_split` prefixes the gateway gives the
same answer itself, because there a request may reach it without passing the
console. Left to Starlette it would answer `307` with an absolute `Location`
built from the `Host` it received and the scheme it saw, which behind a
TLS-terminating entry is `http`.

## Adding a public path

1. Add the path to `deploy/public-paths.json`. Mark it `entry_split` only
   when the deployments that split their entry are to send it to the gateway
   directly; that is a new split prefix, and each of them reruns the probe.
2. Choose the routing mechanism. A gateway path that takes a request body is a
   route handler: a `route.ts` that exports `gatewayRoute('<its prefix>')`, from
   `src/lib/server/gatewayProxy.ts`, for every method. A gateway path that only
   answers `GET` may instead be a static rule in `rewrites()` in
   `apps/frontend/next.config.js`. Write a handler of its own when the target
   must remain configurable after build or the request needs application logic.
3. Name it specifically. Prefer `/prefix/thing` over `/prefix/:path*` unless
   every current and future route under that prefix is authorized by
   construction.
4. If the path needs a check the destination cannot make for itself, it is a
   route handler, not a rewrite.
5. Rebuild the console for a source or rewrite-table change. After that image
   is deployed, a runtime handler's target can be changed by recreating its
   container; a static rewrite target still requires another image build.
6. Add the path to the table above. The tests named under
   [The manifest](#the-manifest) fail until the manifest, the route files,
   `next.config.js` and the middleware's `matcher` agree.
