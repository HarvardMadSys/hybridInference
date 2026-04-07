// Centralized configuration for the application.
//
// The frontend bundle is built once and then served statically, so deployment
// selection must be available at build time.

type DeployTarget = 'production' | 'staging';

const rawDeployTarget = process.env.NEXT_PUBLIC_DEPLOY_TARGET || 'production';
const deployTarget: DeployTarget = rawDeployTarget === 'staging' ? 'staging' : 'production';

const defaultApiBaseByTarget: Record<DeployTarget, string> = {
  production: 'https://freeinference.org',
  staging: 'http://localhost:8000',
};

export const config = {
  // API Configuration
  // `NEXT_PUBLIC_API_BASE` wins if explicitly provided. Otherwise we derive a
  // sensible default from the build target.
  apiBase: process.env.NEXT_PUBLIC_API_BASE || defaultApiBaseByTarget[deployTarget],
  deployTarget,

  // Application Configuration
  appName: process.env.NEXT_PUBLIC_APP_NAME || 'FreeInference',
  environment: process.env.NODE_ENV || 'development',

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
