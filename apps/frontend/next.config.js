/** @type {import('next').NextConfig} */
const BACKEND_INTERNAL_URL = process.env.BACKEND_INTERNAL_URL || 'http://backend:8080';

const nextConfig = {
  reactStrictMode: true,
  output: 'standalone',
  // Next strips a trailing slash by redirecting; pgAdmin (Flask) adds one back
  // the same way. Left on, the two bounce a request between them forever the
  // first time anyone opens /pgadmin/browser/ — so the normalization is turned
  // off here and reimplemented in middleware.ts for everything except the
  // proxied path, which has to reach pgAdmin exactly as the browser asked.
  skipTrailingSlashRedirect: true,
  images: {
    // Remote host for team-member photos (branding.team). A deployment that
    // hosts photos off-site names the host; one that has no team, or serves
    // the photos itself, allows no remote host at all. CommonJS file: cannot
    // import the TS branding module, so read the env var inline.
    remotePatterns: process.env.NEXT_PUBLIC_TEAM_IMAGE_HOST
      ? [{ protocol: 'https', hostname: process.env.NEXT_PUBLIC_TEAM_IMAGE_HOST }]
      : [],
  },
  async rewrites() {
    return [
      { source: '/v1/:path*', destination: `${BACKEND_INTERNAL_URL}/v1/:path*` },
      // Anthropic Messages endpoint (Claude Code et al.). Clients point at
      // <site>/anthropic, so without this rewrite those requests fall through
      // to the Next.js 404 page.
      { source: '/anthropic/:path*', destination: `${BACKEND_INTERNAL_URL}/anthropic/:path*` },
      { source: '/auth/:path*', destination: `${BACKEND_INTERNAL_URL}/auth/:path*` },
      { source: '/user/:path*', destination: `${BACKEND_INTERNAL_URL}/user/:path*` },
      { source: '/admin/:path*', destination: `${BACKEND_INTERNAL_URL}/admin/:path*` },
      {
        source: '/internal/verify-grafana',
        destination: `${BACKEND_INTERNAL_URL}/internal/verify-grafana`,
      },
      {
        source: '/internal/verify-admin',
        destination: `${BACKEND_INTERNAL_URL}/internal/verify-admin`,
      },
      {
        source: '/internal/playground/:path*',
        destination: `${BACKEND_INTERNAL_URL}/internal/playground/:path*`,
      },
      // The cloud agent's control plane runs on its own machine and reaches
      // this gateway over its public origin. Nginx sends unmatched paths here,
      // so a path with no rewrite is answered by Next.js — and its 404 is an
      // HTML page, which reads to the caller as "the gateway is down" rather
      // than "this path is not forwarded". Every one of these was unreachable
      // in production and staging until this entry existed.
      //
      // Named individually, because this prefix is shared: /internal/verify-*
      // authenticate a browser session by cookie, and a blanket
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
      // Whole prefix, unlike the two above: every route on that router carries
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
      // Public homepage updates. Nginx routes unmatched paths to the frontend,
      // so this rewrite forwards the request on to FastAPI (same pattern as
      // /health). Without it the static frontend would 404 the fetch in prod.
      { source: '/site-updates', destination: `${BACKEND_INTERNAL_URL}/site-updates` },
      // Public distribution identity consumed by SiteConfigProvider.
      { source: '/site-config', destination: `${BACKEND_INTERNAL_URL}/site-config` },
    ];
  },
};

module.exports = nextConfig;
