// Centralized configuration for the application.
//
// The frontend bundle is built once and then served statically, so deployment
// selection must be available at build time.

type DeployTarget = 'production' | 'staging';

const rawDeployTarget = process.env.NEXT_PUBLIC_DEPLOY_TARGET || 'production';
const deployTarget: DeployTarget = rawDeployTarget === 'staging' ? 'staging' : 'production';

// Build-time fallbacks only: every real deployment passes NEXT_PUBLIC_API_BASE
// (deploy/docker/docker-compose.yml sets it for both targets). A build that
// supplies nothing is a local clone, so it must talk to a local backend —
// pointing it at someone else's production gateway would send an unconfigured
// operator's traffic to a service they do not run.
const defaultApiBaseByTarget: Record<DeployTarget, string> = {
  production: 'http://localhost:8080',
  staging: 'http://localhost:8080',
};

export const config = {
  // API Configuration
  // `NEXT_PUBLIC_API_BASE` wins if explicitly provided. An explicit empty
  // string means same-origin requests (for example, through Next rewrites), so
  // only an absent value falls back to the standalone local backend.
  apiBase: process.env.NEXT_PUBLIC_API_BASE ?? defaultApiBaseByTarget[deployTarget],
  deployTarget,

  // Application Configuration
  appName: process.env.NEXT_PUBLIC_APP_NAME || 'HybridInference',
  environment: process.env.NODE_ENV || 'development',

  // Build Identity (baked in at build time by deploy scripts)
  buildSha: process.env.NEXT_PUBLIC_BUILD_SHA || '',
  buildTimestamp: process.env.NEXT_PUBLIC_BUILD_TIMESTAMP || '',

  // Feature Flags (can be toggled via environment variables if needed)
  enableAnalytics: process.env.NEXT_PUBLIC_ENABLE_ANALYTICS === 'true',
  enableDarkMode: process.env.NEXT_PUBLIC_ENABLE_DARK_MODE !== 'false',

  // Computed
  isDevelopment: process.env.NODE_ENV === 'development',
  isProduction: process.env.NODE_ENV === 'production',
  isStagingTarget: deployTarget === 'staging',
} as const;

if (
  config.isProduction &&
  config.deployTarget === 'production' &&
  config.apiBase.startsWith('http://localhost')
) {
  console.warn(
    'Warning: Production build is using a localhost API base. Check NEXT_PUBLIC_API_BASE.',
  );
}

export type Config = typeof config;
