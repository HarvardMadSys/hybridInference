# The Public Path Table

HybridInference ships two long-running HTTP services: the FastAPI gateway
(`apps/backend`) and a Next.js console (`apps/frontend`). Only one of them needs
to be reachable from the internet.

**The console owns the public path table.** It is the process that terminates
public traffic. Static gateway forwarding lives in the `rewrites()`
configuration in `apps/frontend/next.config.js`; proxies whose destinations
must remain changeable after the image is built live in App Router route
handlers. Anything not named by either mechanism is served by the console's
own pages.

This is worth stating plainly because it is easy to get wrong from the outside:
adding a public route is an edit to `next.config.js` or a route handler, not to
whatever reverse proxy or tunnel happens to sit in front. A path missing from
both is answered by the console's HTML 404 page — which reads to an API client
as "the gateway is down" rather than "this path is not forwarded".

```text
client ──▶ (your edge: CDN / tunnel / reverse proxy)
              │
              ▼
        Next.js console ──┬──▶ FastAPI gateway   (build-time rewrites)
                          ├──▶ cloud agent       (runtime route handler)
                          ├──▶ pgAdmin           (runtime route handler, admin-gated)
                          └──▶ its own pages     (everything else)
```

## The path table

The table below combines the static rewrites with the filesystem route
handlers. `apps/frontend/next.config.js` is the source of truth for rewrites;
the matching `route.ts` file is the source of truth for a runtime handler.

Destination resolution is deliberately split. Next resolves `rewrites()` at
**build** time and writes them into `.next/routes-manifest.json`. Route handlers
read their server-only environment at request time, so changing those targets
requires recreating the container, not rebuilding the image.

| Variable | Default | Resolution | Points at |
|---|---|---|---|
| `BACKEND_INTERNAL_URL` | `http://backend:8080` | build time for rewrites; also runtime for server-side site config fetches | the FastAPI gateway |
| `AGENT_WEB_INTERNAL_URL` | *(unset)* | runtime | the standalone cloud agent's web app |
| `AGENT_CONTROL_PLANE_INTERNAL_URL` | *(unset)* | runtime | that agent's control-plane API |
| `PGADMIN_INTERNAL_URL` | `http://pgadmin:80` | runtime | pgAdmin |

### Runtime route handler — the cloud agent proxy

`apps/frontend/src/app/agents/[[...path]]/route.ts` reads both agent targets on
every request. With either variable unset, `/agents` truthfully answers 404;
an invalid target or an unreachable configured service answers 502.

| Source | Destination | Note |
|---|---|---|
| `/agents/api/:path*` | `${AGENT_CONTROL_PLANE_INTERNAL_URL}/:path*` | prefix **stripped** — the control plane serves its routes at its own root |
| `/agents/:path*` | `${AGENT_WEB_INTERNAL_URL}/agents/:path*` | prefix **kept** — that app is built with `basePath=/agents` and generates links carrying it |
| `/agents` | `${AGENT_WEB_INTERNAL_URL}/agents` | bare prefix |

Three implementation facts are load-bearing here:

- `/agents/api/:path*` is matched before `/agents/:path*`. The first is a prefix
  of the second, so with the order reversed every API call is answered with the
  web app's HTML.
- The handler streams request and response bodies, including SSE, but it cannot
  accept WebSocket upgrades. A future WebSocket endpoint needs an upgrade-aware
  proxy in front of Next.js.
- Hop-by-hop headers are removed, cookies are preserved, and redirects that
  name either internal agent service are folded back through its public prefix.

### Legacy `beforeFiles` compatibility

`next.config.js` still emits the same three rules as `beforeFiles` rewrites when
both agent URLs are supplied **while building** an older distribution image.
This is only a transition bridge: canonical neutral images supply neither
build arg and use the runtime handler above. Because `beforeFiles` wins over a
filesystem route, an image built with those legacy values keeps its baked-in
targets and cannot be retargeted by changing only the container environment.

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

`skipTrailingSlashRedirect: true` is set globally in `next.config.js`, because
proxied applications own their path semantics. Next normally redirects a
trailing slash away; pgAdmin (Flask) or the agent web app may add one back. Left
on, the two layers can bounce a browser between them forever. A proxied path has
to reach its upstream exactly as the browser asked for it.

Turning that flag off globally would change every other URL on the site, so
`apps/frontend/src/middleware.ts` reimplements the redirect for everything
*except* the `/pgadmin` and `/agents` prefixes — a 308 to the slash-less path,
built from `new URL(request.url)` rather than `nextUrl.clone()` (a cloned
`NextURL` remembers the incoming trailing slash and re-serializes it,
redirecting the request to exactly where it already is).

## Adding a public path

1. Choose the routing mechanism. Add a static rule to `rewrites()` in
   `apps/frontend/next.config.js`; use a `route.ts` handler when its target must
   remain configurable after build or the request needs application logic.
2. Name it specifically. Prefer `/prefix/thing` over `/prefix/:path*` unless
   every current and future route under that prefix is authorized by
   construction.
3. If the path needs a check the destination cannot make for itself, it is a
   route handler, not a rewrite.
4. Rebuild the console for a source or rewrite-table change. After that image
   is deployed, a runtime handler's target can be changed by recreating its
   container; a static rewrite target still requires another image build.
5. Regenerate the table above.
