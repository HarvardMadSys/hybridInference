/** @type {import('next').NextConfig} */
const BACKEND_INTERNAL_URL = process.env.BACKEND_INTERNAL_URL || 'http://backend:8080';

// Compatibility bridge for images built before `/agents` became a runtime
// route handler. A branded legacy build may still compile these rewrites in;
// a neutral build leaves both values empty and requests reach
// `src/app/agents/[[...path]]/route.ts`, which reads its targets at runtime.
//
// Two variables and not one because they are two services — the browser app
// and its control-plane API — and on a real deployment they are two ports, or
// two machines. See the cloud agent repository's ENVIRONMENT.md.
const AGENT_WEB_INTERNAL_URL = process.env.AGENT_WEB_INTERNAL_URL || '';
const AGENT_CONTROL_PLANE_INTERNAL_URL = process.env.AGENT_CONTROL_PLANE_INTERNAL_URL || '';

const nextConfig = {
  reactStrictMode: true,
  output: 'standalone',
  // Keep every backend consumer on the exact target compiled into the rewrite
  // manifest. next.config `env` values are inlined during `next build`, so a
  // container-level BACKEND_INTERNAL_URL cannot retarget only server code and
  // leave /v1, /auth, and the other rewrites pointing somewhere else.
  env: {
    BUILT_BACKEND_INTERNAL_URL: BACKEND_INTERNAL_URL,
  },
  // Next strips a trailing slash by redirecting; pgAdmin (Flask) adds one back
  // the same way. Left on, the two bounce a request between them forever the
  // first time anyone opens /pgadmin/browser/ — so the normalization is turned
  // off here and reimplemented in middleware.ts for everything except the
  // proxied path, which has to reach pgAdmin exactly as the browser asked.
  skipTrailingSlashRedirect: true,
  async rewrites() {
    // **`beforeFiles`, and it has to be.** A rewrite returned in a flat array
    // is `afterFiles`, which Next checks *after* filesystem routes. This app's
    // own `/agents` pages were removed at H4, but beforeFiles keeps the proxy
    // authoritative even if a page ever reappears under that prefix.
    //
    // With the build variables unset no rewrite is emitted. The runtime route
    // then answers 404 unless both server-only target variables are present.
    // The pre-H4 fallback (this app's own agent pages) is gone.
    //
    // Job history did not move to the standalone service (decision DR5): it
    // has its own database, so `/agents` history there started empty.
    const agentRewrites =
      AGENT_WEB_INTERNAL_URL && AGENT_CONTROL_PLANE_INTERNAL_URL
        ? [
            // The API first: `/agents/api/*` is a prefix of `/agents/*`, and
            // the more specific rule has to be matched first or every API call
            // is served the web app's HTML.
            //
            // The prefix is stripped here — the control plane serves
            // `/v1/agent/...` at its root and knows nothing about `/agents`.
            {
              source: '/agents/api/:path*',
              destination: `${AGENT_CONTROL_PLANE_INTERNAL_URL}/:path*`,
            },
            // The prefix is *kept* here: that app is built with
            // `basePath=/agents`, so it generates its own links and asset URLs
            // already carrying it and expects to receive them.
            { source: '/agents/:path*', destination: `${AGENT_WEB_INTERNAL_URL}/agents/:path*` },
            { source: '/agents', destination: `${AGENT_WEB_INTERNAL_URL}/agents` },
          ]
        : [];

    return {
      beforeFiles: agentRewrites,
      afterFiles: [
        { source: '/v1/:path*', destination: `${BACKEND_INTERNAL_URL}/v1/:path*` },
        // Anthropic Messages endpoint (Claude Code et al.). Clients point at
        // <site>/anthropic, so without this rewrite those requests fall through
        // to the Next.js 404 page.
        { source: '/anthropic/:path*', destination: `${BACKEND_INTERNAL_URL}/anthropic/:path*` },
        { source: '/auth/:path*', destination: `${BACKEND_INTERNAL_URL}/auth/:path*` },
        { source: '/user/:path*', destination: `${BACKEND_INTERNAL_URL}/user/:path*` },
        { source: '/admin/:path*', destination: `${BACKEND_INTERNAL_URL}/admin/:path*` },
        {
          source: '/internal/verify-admin',
          destination: `${BACKEND_INTERNAL_URL}/internal/verify-admin`,
        },
        {
          source: '/internal/playground/:path*',
          destination: `${BACKEND_INTERNAL_URL}/internal/playground/:path*`,
        },
        // The cloud agent's control plane runs on its own machine and reaches
        // this gateway over its public origin. The Cloudflare tunnel routes that
        // origin straight to this container, so a path with no rewrite in this
        // table is answered by Next.js — and its 404 is an HTML page, which reads
        // to the caller as "the gateway is down" rather than "this path is not
        // forwarded". Every one of these was unreachable in production and
        // staging until this entry existed.
        //
        // Named individually, because this prefix is shared: /internal/verify-admin
        // authenticates a browser session by cookie, and a blanket
        // /internal/:path* would forward whatever lands here next without anyone
        // deciding it should be reachable from outside.
        {
          source: '/internal/model-catalog',
          destination: `${BACKEND_INTERNAL_URL}/internal/model-catalog`,
        },
        {
          source: '/internal/users/:userId/status',
          destination: `${BACKEND_INTERNAL_URL}/internal/users/:userId/status`,
        },
        {
          source: '/internal/users/:userId/agent-access',
          destination: `${BACKEND_INTERNAL_URL}/internal/users/:userId/agent-access`,
        },
        // Whole prefix, unlike the lookups above: every route on that router carries
        // the dispatch token as a router-level dependency, so a route added
        // later is authorized by construction. Enumerating them here instead
        // would mean the next one 404s at this layer with nothing to say why.
        {
          source: '/internal/agent-grants',
          destination: `${BACKEND_INTERNAL_URL}/internal/agent-grants`,
        },
        {
          source: '/internal/agent-grants/:path*',
          destination: `${BACKEND_INTERNAL_URL}/internal/agent-grants/:path*`,
        },
        { source: '/health', destination: `${BACKEND_INTERNAL_URL}/health` },
        // Public homepage updates. The tunnel routes every public path to this
        // container, so this rewrite forwards the request on to FastAPI (same
        // pattern as /health). Without it the frontend would 404 the fetch in prod.
        { source: '/site-updates', destination: `${BACKEND_INTERNAL_URL}/site-updates` },
        // Public distribution identity consumed by SiteConfigProvider.
        { source: '/site-config', destination: `${BACKEND_INTERNAL_URL}/site-config` },
      ],
    };
  },
};

module.exports = nextConfig;
