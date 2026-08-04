/** @type {import('next').NextConfig} */
const BACKEND_INTERNAL_URL = process.env.BACKEND_INTERNAL_URL || 'http://backend:8080';

const nextConfig = {
  reactStrictMode: true,
  output: 'standalone',
  images: {
    // Remote host for team-member photos (branding.team). A deployment that
    // hosts photos off-site names the host; one that has no team, or serves
    // the photos itself, allows no remote host at all. CommonJS file: cannot
    // import the TS branding module, so read the env var inline.
    remotePatterns: process.env.NEXT_PUBLIC_TEAM_IMAGE_HOST
      ? [{ protocol: 'https', hostname: process.env.NEXT_PUBLIC_TEAM_IMAGE_HOST }]
      : [],
  },
  async redirects() {
    // The cloud agent moved to its own deployment. Anyone who bookmarked
    // /agents, or follows a link from a PR this gateway's agent opened months
    // ago, lands here — and a 404 tells them the product was withdrawn rather
    // than moved.
    //
    // Configured, not hardcoded: this repository is deployed by more than one
    // operator, and a fixed destination would send a self-hosted user's traffic
    // to somebody else's site. A deployment that does not set it simply 404s,
    // which is the truthful answer when there is nowhere to send them.
    //
    // 307, not 308: a permanent redirect is cached by browsers indefinitely,
    // and a wrong value would be uncorrectable for the people who hit it first.
    const target = process.env.NEXT_PUBLIC_CLOUD_AGENT_URL;
    if (!target) return [];
    return [
      { source: '/agents', destination: target, permanent: false },
      // `:path*` on both sides, or every deep link lands on the new
      // service's root — a PR comment pointing at one job would open
      // somebody's task list instead, which reads as the link being stale.
      { source: '/agents/:path*', destination: `${target}/:path*`, permanent: false },
    ];
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
