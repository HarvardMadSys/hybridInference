// Centralized configuration for the application.
//
// Client API routing and build identity are compiled into the neutral bundle.
// Distribution branding is intentionally absent here: the server layout loads
// that from /site-config for each request.

// Build-time fallbacks only: every real deployment passes NEXT_PUBLIC_API_BASE
// (deploy/docker/docker-compose.yml sets it for both targets). A build that
// supplies nothing is a local clone, so it must talk to a local backend —
// pointing it at someone else's production gateway would send an unconfigured
// operator's traffic to a service they do not run.
const DEFAULT_API_BASE = 'http://localhost:8080';

export const config = {
  // API Configuration
  // `NEXT_PUBLIC_API_BASE` wins if explicitly provided. An explicit empty
  // string means same-origin requests (for example, through Next rewrites), so
  // only an absent value falls back to the standalone local backend.
  apiBase: process.env.NEXT_PUBLIC_API_BASE ?? DEFAULT_API_BASE,

  // Application Configuration
  appName: process.env.NEXT_PUBLIC_APP_NAME || 'HybridInference',

  // Build Identity (baked in at build time by deploy scripts)
  buildSha: process.env.NEXT_PUBLIC_BUILD_SHA || '',
  buildTimestamp: process.env.NEXT_PUBLIC_BUILD_TIMESTAMP || '',

  // Computed
  isDevelopment: process.env.NODE_ENV === 'development',
  isProduction: process.env.NODE_ENV === 'production',
} as const;

if (config.isProduction && config.apiBase.startsWith('http://localhost')) {
  console.warn(
    'Warning: Production build is using a localhost API base. Check NEXT_PUBLIC_API_BASE.',
  );
}
