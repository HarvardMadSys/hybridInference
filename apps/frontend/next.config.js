/** @type {import('next').NextConfig} */
const BACKEND_INTERNAL_URL = process.env.BACKEND_INTERNAL_URL || 'http://backend:8080';

// Where the standalone cloud agent lives, once a deployment runs one. Unset on
// every deployment that does not, and then nothing below changes: `/agents`
// keeps resolving to this app's own pages, which is what it did before the
// split and what it must keep doing for anyone who never adopts the new
// service.
//
// Two variables and not one because they are two services — the browser app
// and its control-plane API — and on a real deployment they are two ports, or
// two machines. See the cloud agent repository's ENVIRONMENT.md.
const AGENT_WEB_INTERNAL_URL = process.env.AGENT_WEB_INTERNAL_URL || '';
const AGENT_CONTROL_PLANE_INTERNAL_URL = process.env.AGENT_CONTROL_PLANE_INTERNAL_URL || '';

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
  async rewrites() {
    // **`beforeFiles`, and it has to be.** A rewrite returned in a flat array
    // is `afterFiles`, which Next checks *after* filesystem routes — and this
    // app still ships its own `/agents` pages, so those would win and the
    // rewrite would look like it did nothing. Handing `/agents` to the
    // standalone service means overriding a page that exists.
    //
    // That override is the cutover for the UI: the moment this deploys with
    // the variables set, `/agents` is the new service and this app's agent
    // pages are unreachable. Rolling back is unsetting the variables or
    // reverting this commit — no DNS, no dashboard, no ssh.
    //
    // Job history does not move with it (decision DR5). The new service has
    // its own database, so what a user sees at `/agents` afterwards starts
    // empty.
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
      ],
    };
  },
};

module.exports = nextConfig;
