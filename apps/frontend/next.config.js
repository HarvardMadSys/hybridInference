/** @type {import('next').NextConfig} */
const BACKEND_INTERNAL_URL = process.env.BACKEND_INTERNAL_URL || 'http://backend:8080';

const nextConfig = {
  reactStrictMode: true,
  output: 'standalone',
  images: {
    remotePatterns: [{ protocol: 'https', hostname: 'junchengyang.com' }],
  },
  async rewrites() {
    return [
      { source: '/v1/:path*', destination: `${BACKEND_INTERNAL_URL}/v1/:path*` },
      { source: '/auth/:path*', destination: `${BACKEND_INTERNAL_URL}/auth/:path*` },
      { source: '/user/:path*', destination: `${BACKEND_INTERNAL_URL}/user/:path*` },
      { source: '/admin/:path*', destination: `${BACKEND_INTERNAL_URL}/admin/:path*` },
      { source: '/internal/verify-grafana', destination: `${BACKEND_INTERNAL_URL}/internal/verify-grafana` },
      { source: '/internal/verify-admin', destination: `${BACKEND_INTERNAL_URL}/internal/verify-admin` },
      { source: '/internal/playground/:path*', destination: `${BACKEND_INTERNAL_URL}/internal/playground/:path*` },
      { source: '/health', destination: `${BACKEND_INTERNAL_URL}/health` },
    ];
  },
};

module.exports = nextConfig;
