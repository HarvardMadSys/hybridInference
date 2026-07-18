/** @type {import('next').NextConfig} */
const BACKEND_INTERNAL_URL = process.env.BACKEND_INTERNAL_URL || 'http://backend:8080';

const nextConfig = {
  reactStrictMode: true,
  output: 'standalone',
  images: {
    remotePatterns: [
      {
        protocol: 'https',
        // Remote host for team-member photos (branding.team). CommonJS file:
        // cannot import the TS branding module, so read the env var inline.
        hostname: process.env.NEXT_PUBLIC_TEAM_IMAGE_HOST || 'junchengyang.com',
      },
    ],
  },
  async rewrites() {
    return [
      { source: '/v1/:path*', destination: `${BACKEND_INTERNAL_URL}/v1/:path*` },
      // Anthropic Messages endpoint (Claude Code et al.). The public docs
      // advertise https://freeinference.org/anthropic as the base URL; without
      // this rewrite those requests fall through to the Next.js 404 page.
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
