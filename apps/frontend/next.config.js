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
const LEGACY_AGENT_REWRITES = Boolean(AGENT_WEB_INTERNAL_URL && AGENT_CONTROL_PLANE_INTERNAL_URL);

// Which public-site UI this build compiles in, and the generated files that
// answer it. Resolved here, at config load, because that is the one moment all
// three toolchains can be pointed at the same decision: Webpack through the
// alias below, TypeScript through the tsconfig this writes, Vitest through the
// same alias. A distribution that points SITE_UI_DIR at a module this build
// cannot use fails right here, before any compilation starts — the failure has
// to be loud, because the alternative is publishing a site whose home page
// silently reverted to the console's.
// eslint-disable-next-line @typescript-eslint/no-require-imports
const { prepareSiteUi, webpackAlias } = require('./src/site-ui/resolve');
const siteUi = prepareSiteUi(__dirname);
const siteUiName = siteUi.kind === 'distribution' ? ` '${siteUi.id}'` : '';
// One line in the build log naming what this image compiles in.
// eslint-disable-next-line no-console
console.log(`[site-ui] compiling in the ${siteUi.kind} Site UI${siteUiName}, API v${siteUi.api}`);

const nextConfig = {
  reactStrictMode: true,
  output: 'standalone',
  // Point Webpack at the module the resolution chose, and at the generated
  // tsconfig that describes it. Both are needed: an alias alone leaves `tsc`
  // checking the neutral UI while the build ships a distribution's, which is
  // exactly the disagreement this seam exists to prevent.
  //
  // `tsconfigPath` is set unconditionally, because the file is generated on
  // every config load — including for `next dev`, where Next compiles the types
  // itself. `next.config.js` is plain JS, so this is not type-checked; the
  // generated project is checked by `npm run type-check`.
  webpack: (config) => {
    config.resolve = config.resolve || {};
    config.resolve.alias = {
      ...(config.resolve.alias || {}),
      ...webpackAlias(__dirname, siteUi),
    };
    // Hold a distribution's module to the imports the interface promises where
    // the bundler resolves them — client, server and edge alike — using the
    // module directory and facade alias this config already resolved. The
    // source check in `prepareSiteUi` reports what it can see earlier; this is
    // the one that sees every request. See src/site-ui/containment.js.
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    require('./src/site-ui/containment').installSiteUiContainment(config, __dirname, siteUi);
    return config;
  },
  // Keep every backend consumer on the exact target compiled into the rewrite
  // manifest. next.config `env` values are inlined during `next build`, so a
  // container-level BACKEND_INTERNAL_URL cannot retarget only server code and
  // leave /v1, /auth, and the other rewrites pointing somewhere else.
  env: {
    BUILT_BACKEND_INTERNAL_URL: BACKEND_INTERNAL_URL,
  },
  // Type-check the build with the generated project (`siteUi.tsconfigPath`,
  // written by the resolver above), the one that maps `@site-ui/*` to the
  // module this build compiles. Without this key `next build` read
  // `tsconfig.json`, whose globs took in every file of a staged module — its
  // tests and test-runner configuration too, which import packages this
  // application does not install. Next joins the value to the project
  // directory, so it is the relative name.
  typescript: {
    tsconfigPath: 'tsconfig.generated.json',
  },
  // Next strips a trailing slash by redirecting; pgAdmin (Flask) adds one back
  // the same way. Left on, the two bounce a request between them forever the
  // first time anyone opens /pgadmin/browser/ — so the normalization is turned
  // off here and reimplemented in middleware.ts for everything except the
  // proxied path, which has to reach pgAdmin exactly as the browser asked.
  skipTrailingSlashRedirect: true,
  // How long a rewrite waits for the next byte from the gateway. Next's default
  // is 30s, and when it runs out before the response has started the browser
  // gets a bare HTTP 500: every non-streaming API call that takes longer to
  // answer -- a long completion on /v1/responses or /anthropic, a usage
  // insights report -- failed here although the gateway was still working on
  // it. An hour matches the gateway's own cap on streams
  // (STREAM_REQUEST_TIMEOUT_SECONDS); the gateway ends everything else sooner
  // (REQUEST_TIMEOUT_SECONDS, 120s by default) with a proper 504, and streams
  // send a keepalive every 15s, so this is never the limit that fires.
  //
  // And how much of a request body Next copies. Next makes a copy of the body
  // of every request a middleware sees, and of every request it forwards
  // through a rewrite below, keeps the copy in memory until the request ends,
  // and stops copying at this size -- forwarding what it has under the caller's
  // original Content-Length, so the upstream waits for bytes that never come.
  // That once failed every API call over the 10MB default, when the middleware
  // matched every path and the API went through rewrites; raising the limit to
  // 100MB, later 32MiB, let any anonymous caller pin that much memory per
  // request (twice on a page), and a few dozen stalled uploads were enough to
  // end the container at its memory limit.
  //
  // Nothing that takes a body goes through either any more. Every gateway path
  // that accepts one -- `/v1`, `/anthropic`, `/auth`, `/user`, `/admin` and
  // the published `/internal` paths -- is a route handler the middleware does
  // not match (src/middleware.ts), which streams the body to the gateway
  // whatever its size; so are `/pgadmin` and `/agents`. What is left are the
  // console's pages, which take no body (there are no Server Actions), and the
  // rewrites below, which forward GETs. So the limit only has to cover what a
  // page might be sent by mistake; a body past it reaches nothing that reads
  // it. next.config.proxy.test.ts holds the table to that.
  //
  // The exception is an image built with the legacy `/agents` rewrites
  // compiled in (above): they forward the agent's API bodies, so such a build
  // keeps the old limit.
  experimental: {
    proxyTimeout: 60 * 60 * 1000,
    middlewareClientMaxBodySize: LEGACY_AGENT_REWRITES ? 32 * 1024 * 1024 : 16 * 1024,
  },
  // Headers for every response this server sends of its own or relays from the
  // gateway: no MIME sniffing, no framing, and no full URL -- a reset token,
  // say -- in the Referer sent to another origin. Pages also get a
  // Content-Security-Policy, per response, from src/middleware.ts.
  //
  // Not under /pgadmin or /agents: those are other applications' pages, which
  // set their own. pgAdmin, for one, frames its own panels.
  // A gateway path that ends in a slash goes to the same path without it, as
  // every path did before `skipTrailingSlashRedirect`. The console's pages get
  // theirs from src/middleware.ts, which the gateway's paths do not pass
  // through (it would copy their bodies). Left to the gateway, `/v1/models/`
  // would be redirected to the gateway's own internal address.
  async redirects() {
    return [
      {
        source:
          '/:prefix(v1|anthropic|internal|auth|user|admin|health|site-config|site-updates|public-stats|site-assets)/:path*/',
        destination: '/:prefix/:path*',
        permanent: true,
      },
    ];
  },
  async headers() {
    return [
      {
        source: '/:path((?!pgadmin(?:/|$)|agents(?:/|$)).*)',
        headers: [
          { key: 'X-Content-Type-Options', value: 'nosniff' },
          { key: 'X-Frame-Options', value: 'DENY' },
          { key: 'Referrer-Policy', value: 'strict-origin-when-cross-origin' },
        ],
      },
    ];
  },
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
    const agentRewrites = LEGACY_AGENT_REWRITES
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
      // Only the gateway's public GET documents are rewrites. Every gateway
      // path that takes a request body -- `/v1`, `/anthropic`, `/auth`, `/user`,
      // `/admin` and the published `/internal` paths, each named on its own --
      // is a route handler under src/app instead (src/lib/server/gatewayProxy.ts):
      // the rewrite proxy copies a body into memory, cut off at
      // `middlewareClientMaxBodySize`, and never reports a gateway that goes
      // away mid-response.
      //
      // Next checks these rules before any dynamic route, a `[[...path]]`
      // handler included, so a rule here must not match a path a handler
      // serves: `/user/:path*` once took every `/user/batch-files` upload
      // from its handler that way. next.config.routes.test.ts checks every
      // handler against this table.
      afterFiles: [
        { source: '/health', destination: `${BACKEND_INTERNAL_URL}/health` },
        // Public homepage updates. The tunnel routes every public path to this
        // container, so this rewrite forwards the request on to FastAPI (same
        // pattern as /health). Without it the frontend would 404 the fetch in prod.
        { source: '/site-updates', destination: `${BACKEND_INTERNAL_URL}/site-updates` },
        // Public distribution identity consumed by SiteConfigProvider.
        { source: '/site-config', destination: `${BACKEND_INTERNAL_URL}/site-config` },
        // Daily aggregate usage snapshot rendered by the public /stats page.
        { source: '/public-stats', destination: `${BACKEND_INTERNAL_URL}/public-stats` },
      ],
    };
  },
};

module.exports = nextConfig;
