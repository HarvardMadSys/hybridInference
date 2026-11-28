# The Public Path Table

HybridInference runs two HTTP services: the FastAPI gateway (`apps/backend`)
and the Next.js console (`apps/frontend`). Only the console needs to be
reachable from the internet.

`deploy/public-paths.json` is the public path table. It lists every public path
that is not one of the console's own pages, and what serves it. The console
implements it: route handlers forward the gateway's paths, static rewrites in
`apps/frontend/next.config.js` forward a few that only answer `GET`, and the
cloud agent and pgAdmin have route handlers of their own. Every other path is
one of the console's pages.

```text
client ──▶ (your edge: CDN / tunnel / reverse proxy)
              │   └── optional: /v1, /anthropic ──────────▶ FastAPI gateway
              ▼
        Next.js console ──┬──▶ FastAPI gateway   (route handlers; rewrites for a few GETs)
                          ├──▶ cloud agent       (runtime route handler)
                          ├──▶ pgAdmin           (runtime route handler, admin-gated)
                          └──▶ its own pages     (everything else)
```

Exposing the console's port therefore exposes every gateway path in the table.
To publish a new path, add it to the manifest and to the console; the reverse
proxy or tunnel in front does not change. A path missing from the console gets
its HTML 404 page, which an API client reads as "the gateway is down".

A deployment's entry (its CDN, tunnel or load balancer) may send the paths
marked `entry_split`, `/v1` and `/anthropic`, straight to the gateway, so that a
console release or restart does not interrupt inference. That choice belongs to
the deployment's entry; this repository ships no entry configuration.
[Splitting inference traffic at the entry](deployment.md#splitting-inference-traffic-at-the-entry)
lists the conditions an entry must meet.

The console keeps forwarding those prefixes either way, so removing the entry's
rules sends the traffic back through it, and a new route under a split prefix
needs no entry change. Adding or moving a split prefix does change the entry,
and the [probe](deployment.md#checking-an-entry-split-with-the-probe) must run
again.

## The manifest

Each entry of `deploy/public-paths.json` has four fields:

| Field | Meaning |
|---|---|
| `path` | The public path. A segment written `:name` stands for any one segment: `/internal/users/:userId/status` |
| `match` | `prefix`: the path itself or anything below it, so `/v1` covers `/v1` and `/v1/...` but not `/v10`. `exact`: the path alone. Both are judged after dot segments are resolved, as the console's `isUnder()` judges them |
| `owner` | What serves it: `gateway`, `console` (a route handler of the console's own, such as `/healthz` and `/site-assets`), `cloud-agent` or `pgadmin` |
| `entry_split` | `true` when a deployment's entry may send the path straight to the gateway. Only `/v1` and `/anthropic` are |

A gateway path that is not in the manifest must not be reachable from the
internet: FastAPI's `/docs`, `/redoc` and `/openapi.json`, `/health/deep`,
`/health/ready`, `/health/model-activity`, and every `/internal` route not
listed. The console forwards none of them, and an entry that splits off a
prefix must keep them out too (rules E1 and E7 in the deployment guide).

The console's routing cannot read the manifest: route handlers are files,
`rewrites()` and `redirects()` are compiled at build time, and the middleware's
`matcher` must be a literal. Tests hold each of them to the manifest and fail
when one changes without the others:

- `apps/frontend/next.config.public-paths.test.ts` checks that each gateway
  entry is served by one route handler or one rewrite, that the middleware's
  `matcher` leaves out the manifest's first segments, and that the
  trailing-slash rule in `redirects()` covers the right segments. `/healthz` is
  the one manifest path the middleware still sees.
- `apps/frontend/src/lib/server/gatewayRoutes.test.ts` checks the gateway route
  files against the manifest.
- `tests/unit/middleware/test_trailing_slash.py` checks that the gateway's own
  trailing-slash redirect covers the `entry_split` prefixes.

The rewrites, the redirect rule and the matcher work per first segment
(`internal`, not `/internal/verify-admin`). The route files, one per path,
decide which `/internal` paths are published.

## The path table

Two manifest paths are the console's own and reach nothing behind it:
`/healthz`, the container health check (`src/app/healthz/route.ts`), which
answers `200 ok` without asking the gateway, and `/site-assets`, the Site UI
module's images (`src/app/site-assets/[[...path]]/route.ts`). `/health` is the
gateway's check, forwarded.

`apps/frontend/next.config.js` is the source of truth for rewrites, and each
`route.ts` for its handler. Next resolves rewrites at build time, into
`.next/routes-manifest.json`. Route handlers read their server-only environment
at request time, so changing their targets needs a recreated container, not a
new image.

| Variable | Default | Resolution | Points at |
|---|---|---|---|
| `BACKEND_INTERNAL_URL` | `http://backend:8080` | build time for rewrites and server-side backend calls | the FastAPI gateway |
| `AGENT_WEB_INTERNAL_URL` | *(unset)* | runtime | the standalone cloud agent's web app |
| `AGENT_CONTROL_PLANE_INTERNAL_URL` | *(unset)* | runtime | that agent's control-plane API |
| `PGADMIN_INTERNAL_URL` | `http://pgadmin:80` | runtime | pgAdmin |

`BACKEND_INTERNAL_URL` is fixed when the console image is built. The same value
goes into the rewrites and into the server-only `BUILT_BACKEND_INTERNAL_URL`
that the gateway's route handlers, `/site-config` and pgAdmin's admin check
use, so a runtime override cannot send only some backend requests elsewhere.
The published image uses `http://backend:8080`, so the backend must have that
network name; another address means rebuilding the console.

### The cloud agent proxy

`apps/frontend/src/app/agents/[[...path]]/route.ts` reads both agent targets on
every request. With either one unset, `/agents` answers 404; an invalid target
or an unreachable service answers 502.

For an agent on its own hostname, set `AGENT_PUBLIC_URL` to its public HTTPS URL
(without credentials). The dashboard's Agents tile, shown only to internal
users, then links there, whatever the proxy targets are. Without it, the tile
links to `/agents` only when both proxy targets are set. Unlike the private
targets, the public URL is sent to the browser in the site configuration.

| Source | Destination | Note |
|---|---|---|
| `/agents/api/:path*` | `${AGENT_CONTROL_PLANE_INTERNAL_URL}/:path*` | prefix **stripped**: the control plane serves its routes at its own root |
| `/agents/:path*` | `${AGENT_WEB_INTERNAL_URL}/agents/:path*` | prefix **kept**: that app is built with `basePath=/agents` and generates links carrying it |
| `/agents` | `${AGENT_WEB_INTERNAL_URL}/agents` | bare prefix |

- `/agents/api/:path*` is matched before `/agents/:path*`. In the other order,
  every API call would get the web app's HTML.
- The handler streams request and response bodies, SSE included, but cannot
  accept WebSocket upgrades. A WebSocket endpoint would need an upgrade-aware
  proxy in front of Next.js.
- Hop-by-hop headers are removed, and redirects that name either internal
  service are folded back through the public prefix. The agent's own cookies
  pass both ways; the console's session cookie is never forwarded to it. A
  cookie the agent sets under one of the console's names is dropped, and the
  rest are confined to `/agents` (a `__Host-` cookie keeps the `Path=/` that
  browsers require of it).

### Gateway route handlers

Every gateway path that takes a request body is forwarded by a route handler,
not a rewrite. Next keeps a copy of every rewritten request body in memory until
the request ends, and stops copying at `middlewareClientMaxBodySize`, so a
rewrite either cuts a large body short or lets any caller pin that much console
memory per request. A route handler streams the body to the gateway and holds
none of it.

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
| `/internal/alert-history/events` | `src/app/internal/alert-history/events/route.ts` | alert-history ingestion from external sources (`POST`; scoped writer token, `404` while off; see [Alert history](alert-history.md)) |
| `/internal/openinfra/ingest` | `src/app/internal/openinfra/ingest/route.ts` | OpenInfra snapshots from the reporters on the deployment's GPU servers (`POST`; ingest token, `404` while none is configured; see [OpenInfra](openinfra.md)) |

Each published `/internal` path has a route file of its own. The prefix is
shared with `/internal/verify-admin`, which authenticates a browser session by
cookie, so a catch-all would publish whatever route lands under `/internal`
next without anyone deciding it should be public. `/internal/agent-grants` is
the one sub-prefix published whole, because every route on that router
requires the dispatch token.

Each handler forwards to `${BACKEND_INTERNAL_URL}` plus the same path, if that
path, with its dot segments resolved, is still under the handler's prefix;
anything else gets a 404. The forwarding code is
`src/lib/server/gatewayProxy.ts`. Unlike a rewrite, the handler:

- ends the caller's response when the gateway drops the connection after the
  response has started (a crash, a restart or a deploy). A rewrite leaves the
  caller waiting until something in front of the console gives up;
- sets no deadline of its own, where a rewrite ends a response after 30 seconds
  without data. The gateway limits every request, and streams send a keepalive
  every 15 seconds; the handler only closes a connection that has carried
  nothing in either direction for an hour;
- answers a gateway it cannot reach with a JSON 502, not Next's plain-text 500.

The gateway receives every request header except the hop-by-hop ones, with
`Host` set to the gateway and the caller's host in `X-Forwarded-Host`; the
handler frames the body itself. The caller's own `X-Forwarded-For` reaches the
gateway unchanged, with no address added; Next fills in `X-Forwarded-For`,
`-Proto` and `-Port` from the connection only when the caller sent none. Read
[Behind the console](trusted-proxies-and-client-ips.md#behind-the-console)
before trusting them. When the caller hangs up, the gateway's request is
cancelled at once.

### Gateway rewrites (`afterFiles`)

These paths only answer `GET`, so a rewrite has no body to copy; Next keeps at
most `middlewareClientMaxBodySize` (16 KiB) of anything a caller sends anyway.
Each destination is `${BACKEND_INTERNAL_URL}` plus the same path.

| Source | Serves |
|---|---|
| `/health` | health check |
| `/site-updates` | the public site banner |
| `/site-config` | public deployment identity, consumed by the console's `SiteConfigProvider` |
| `/public-stats` | the daily usage snapshot behind the public `/stats` page |
| `/public-openinfra` | the live GPU, host and engine snapshot behind the public `/openinfra` page |
| `/public-openinfra/history` | that page's charts, for the range it asks for |

## Why `/pgadmin` is a route handler and not a rewrite

pgAdmin must be reachable by admins only, and a rewrite cannot authenticate: it
is a static mapping applied before any of your code runs. So `/pgadmin` is an
app route, `apps/frontend/src/app/pgadmin/[[...path]]/route.ts`, which wins over
`afterFiles` rewrites and proxies to pgAdmin after checking the caller. Its
choices apply to any authenticated proxy written as a route handler:

- **Fail closed.** `verifyAdmin()` calls `GET /internal/verify-admin` with the
  caller's cookie and a 5-second timeout, and only an explicit `200` admits.
  The handler acts as the only guard, because pgAdmin may run without its own
  login (`PGADMIN_CONFIG_SERVER_MODE` set to `False`).
- **Strip the console's session cookie** before forwarding, so the credential
  never reaches the proxied app.
- **Drop hop-by-hop headers** (RFC 9110 §7.6.1), plus `host` and
  `content-length`, which `fetch` derives itself, and drop `content-encoding`
  on the way back, because `fetch` already decompressed the body. Re-split
  `Set-Cookie` with `getSetCookie()`, since `Headers.forEach` folds repeated
  values together.
- **Keep the app's cookies to the app.** pgAdmin shares the console's origin.
  `scopeProxiedSetCookie()` drops a cookie named like the console's and
  confines the rest to `/pgadmin`, without a `Domain`.
- **Fold upstream-absolute redirects back to paths.** Werkzeug builds absolute
  redirects from the internal container name, which no browser can resolve;
  `foldUpstreamRedirect()` rewrites them to paths, matching on the hostname.
- **Redirect with a bare path.** Behind a tunnel the app sees its bind address as
  `Host`, so an absolute URL would point at `https://0.0.0.0:3001/...`. The
  handler writes `Location` itself, because `NextResponse.redirect()` accepts
  only absolute URLs.

### The trailing-slash interaction

`next.config.js` sets `skipTrailingSlashRedirect: true`, because proxied
applications own their path semantics. Next removes a trailing slash, pgAdmin
(Flask) or the agent web app may add it back, and the two would bounce a
browser between them forever. `apps/frontend/src/middleware.ts` redoes the
redirect for every path except the `/pgadmin` and `/agents` prefixes: a 308 to
the path without the slash, built from `new URL(request.url)`. It avoids
`nextUrl.clone()`, which remembers the incoming slash and redirects the request
to where it already is.

Gateway paths skip the middleware, which would copy their bodies, so
`redirects()` in `next.config.js` gives a gateway path that ends in a slash the
same `308`, as a bare path. Under the `entry_split` prefixes the gateway gives
that answer itself, because a request may reach it without passing the
console. Left to Starlette, it would answer `307` with an absolute `Location`
built from the `Host` and scheme it saw, which behind a TLS-terminating entry
is `http`.

## Adding a public path

1. Add the path to `deploy/public-paths.json`. Mark it `entry_split` only if
   the deployments that split their entry should send it straight to the
   gateway; that makes it a new split prefix, and each of them reruns the
   probe.
2. Choose the mechanism. A gateway path that takes a request body needs a route
   handler: a `route.ts` that exports `gatewayRoute('<its prefix>')`, from
   `src/lib/server/gatewayProxy.ts`, for every method. A path that only answers
   `GET` may be a static rule in `rewrites()` instead. Write a handler of its
   own when the target must stay configurable after the build, or when the
   request needs a check the destination cannot make itself.
3. Name it specifically. Prefer `/prefix/thing` over `/prefix/:path*` unless
   every current and future route under that prefix is authorized by
   construction.
4. Rebuild the console. Once that image is deployed, a runtime handler's target
   can change by recreating the container; a static rewrite target needs
   another image build.
5. Add the path to the tables above. The tests under
   [The manifest](#the-manifest) fail until the manifest, the route files,
   `next.config.js` and the middleware's `matcher` agree.
