# Edge and Console Routing

HybridInference ships two long-running HTTP services: the FastAPI gateway
(`apps/backend`) and a Next.js console (`apps/frontend`). Only one of them needs
to be reachable from the internet.

**The console owns the public path table.** It is the process that terminates
public traffic, and its `rewrites()` configuration in
`apps/frontend/next.config.js` decides which paths are proxied on to FastAPI (or
to another service) and which it answers itself. Anything not named there is
served by the console's own pages.

This is worth stating plainly because it is easy to get wrong from the outside:
adding a public route is an edit to `next.config.js`, not to whatever reverse
proxy or tunnel happens to sit in front. A path missing from that file is
answered by the console's HTML 404 page — which reads to an API client as "the
gateway is down" rather than "this path is not forwarded".

```text
client ──▶ (your edge: CDN / tunnel / reverse proxy)
              │
              ▼
        Next.js console ──┬──▶ FastAPI gateway   (rewrites, table below)
                          ├──▶ cloud agent       (rewrites, when configured)
                          ├──▶ pgAdmin           (route handler, admin-gated)
                          └──▶ its own pages     (everything else)
```

## The path table

The table below is generated from the `rewrites()` function in
`apps/frontend/next.config.js`. That file is the source of truth; if the two
disagree, the file is right. Regenerate this section when you change it.

Destination hosts come from environment variables. Next resolves `rewrites()`
at **build** time and writes the result into `.next/routes-manifest.json`, so
these are build args, not runtime environment: a value supplied only at
`docker run` is read by nothing, and the symptom is the old table continuing to
serve while every variable looks correctly set in `docker inspect`. See the
`frontend` service's `build.args` in `deploy/docker/docker-compose.yml`.

| Variable | Default | Points at |
|---|---|---|
| `BACKEND_INTERNAL_URL` | `http://backend:8080` | the FastAPI gateway |
| `AGENT_WEB_INTERNAL_URL` | *(unset)* | the standalone cloud agent's web app |
| `AGENT_CONTROL_PLANE_INTERNAL_URL` | *(unset)* | that agent's control-plane API |
| `PGADMIN_INTERNAL_URL` | `http://pgadmin:80` | pgAdmin (used by the route handler, not a rewrite) |

### `beforeFiles` — the cloud agent proxy

Emitted **only when both `AGENT_WEB_INTERNAL_URL` and
`AGENT_CONTROL_PLANE_INTERNAL_URL` are set.** With either unset the array is
empty and `/agents` has no route at all.

| Source | Destination | Note |
|---|---|---|
| `/agents/api/:path*` | `${AGENT_CONTROL_PLANE_INTERNAL_URL}/:path*` | prefix **stripped** — the control plane serves its routes at its own root |
| `/agents/:path*` | `${AGENT_WEB_INTERNAL_URL}/agents/:path*` | prefix **kept** — that app is built with `basePath=/agents` and generates links carrying it |
| `/agents` | `${AGENT_WEB_INTERNAL_URL}/agents` | bare prefix |

Two ordering facts are load-bearing here:

- `/agents/api/:path*` must precede `/agents/:path*`. The first is a prefix of
  the second, so with the order reversed every API call is answered with the web
  app's HTML.
- These are `beforeFiles`, not a flat array. A rewrite returned in a flat array
  is `afterFiles`, which Next checks *after* filesystem routes — `beforeFiles`
  keeps the proxy authoritative even if a page later appears under that prefix.

### `afterFiles` — the gateway

Every destination below is `${BACKEND_INTERNAL_URL}` plus the same path.

| Source | Serves |
|---|---|
| `/v1/:path*` | the OpenAI-compatible API surface |
| `/anthropic/:path*` | the Anthropic Messages surface |
| `/auth/:path*` | authentication routes |
| `/user/:path*` | the user dashboard API |
| `/admin/:path*` | the admin API |
| `/internal/verify-grafana` | *(see note below)* |
| `/internal/verify-admin` | cookie-session admin check (used by the pgAdmin handler) |
| `/internal/playground/:path*` | the admin-only model playground the console's dashboard calls |
| `/internal/model-catalog` | model catalog read |
| `/internal/users/:userId/status` | single-user status read |
| `/internal/agent-grants` | mint an inference grant (`POST`) |
| `/internal/agent-grants/:path*` | renew / usage / revoke for one grant |
| `/health` | health check |
| `/site-updates` | the public site banner |
| `/site-config` | public deployment identity, consumed by the console's `SiteConfigProvider` |

Note on `/internal`: the entries are named individually rather than forwarded as
a blanket `/internal/:path*`, and that is deliberate. The prefix is shared —
`/internal/verify-*` authenticate a browser session by cookie — so a blanket
rule would publish whatever route lands under `/internal` next without anyone
deciding it should be reachable from outside. `/internal/agent-grants` is the
one exception granted a whole sub-prefix, because every route on that router
carries a dispatch-token dependency at the router level and so is authorized by
construction.

Note on `/internal/verify-grafana`: the rewrite exists, but no route matching
that path is registered in `apps/backend` at this revision. It forwards to a
gateway 404.

## Why `/pgadmin` is a route handler and not a rewrite

`/pgadmin` is the interesting case, and the reason this page exists.

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
  depends on `PGADMIN_CONFIG_SERVER_MODE`, which defaults to `False`).
- **Strip the console's own session cookie** before forwarding. pgAdmin has no
  use for it, and forwarding a session credential to a proxied app is how those
  leak.
- **Drop hop-by-hop headers** (RFC 9110 §7.6.1) plus `host` and
  `content-length`, which `fetch` derives from the outgoing request. Drop
  `content-encoding` on the way back, because `fetch` already decompressed the
  body. Re-split `Set-Cookie` with `getSetCookie()` — `Headers.forEach` folds
  repeated values into one string.
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

`skipTrailingSlashRedirect: true` is set globally in `next.config.js`, and it
has to be, for this one path. Next normalizes a trailing slash away by
redirecting; pgAdmin (Flask) adds one back the same way. Left on, the two bounce
a browser between them forever the first time anyone opens `/pgadmin/browser/`.
The proxied path has to reach pgAdmin exactly as the browser asked for it.

Turning that flag off globally would change every other URL on the site, so
`apps/frontend/src/middleware.ts` reimplements the redirect for everything
*except* the `/pgadmin` prefix — a 308 to the slash-less path, built from
`new URL(request.url)` rather than `nextUrl.clone()` (a cloned `NextURL`
remembers the incoming trailing slash and re-serializes it, redirecting the
request to exactly where it already is).

## Adding a public path

1. Add the rule to `rewrites()` in `apps/frontend/next.config.js`. Put it in
   `afterFiles` unless it must outrank a filesystem route.
2. Name it specifically. Prefer `/prefix/thing` over `/prefix/:path*` unless
   every current and future route under that prefix is authorized by
   construction.
3. If the path needs a check the destination cannot make for itself, it is a
   route handler, not a rewrite.
4. Rebuild the console. The rewrite table is baked into
   `.next/routes-manifest.json` at build time, so a backend-only deploy will
   not pick the change up. Adopting or rolling back the cloud agent proxy is
   likewise a rebuild, not an environment change.
5. Regenerate the table above.
