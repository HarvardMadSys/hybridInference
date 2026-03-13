// Centralized configuration for the application.
//
// For most developers, the defaults below are sufficient.
// If you need to override (e.g., backend on different port), you can:
// 1. Create .env.local and set NEXT_PUBLIC_API_BASE=http://localhost:YOUR_PORT
// 2. Or set environment variable when running: NEXT_PUBLIC_API_BASE=http://localhost:8080 npm run dev

const isDevelopment = process.env.NODE_ENV === 'development';

function resolveRuntimeApiBase(): string | null {
  if (typeof window === 'undefined') {
    return null;
  }

  const { hostname } = window.location;
  if (hostname === 'localhost' || hostname === '127.0.0.1') {
    return `http://${hostname}:8080`;
  }

  return null;
}

const defaultApiBase =
  resolveRuntimeApiBase() ||
  process.env.NEXT_PUBLIC_API_BASE ||
  (isDevelopment ? 'http://localhost:8080' : 'https://freeinference.org');

export const config = {
  // API Configuration
  // Development defaults to the local backend; production defaults to the public deployment.
  apiBase: defaultApiBase,

  // Application Configuration
  appName: process.env.NEXT_PUBLIC_APP_NAME || 'FreeInference',
  environment: process.env.NODE_ENV || 'development',

  // Feature Flags (can be toggled via environment variables if needed)
  enableAnalytics: process.env.NEXT_PUBLIC_ENABLE_ANALYTICS === 'true',
  enableDarkMode: process.env.NEXT_PUBLIC_ENABLE_DARK_MODE !== 'false',

  // Computed
  isDevelopment,
  isProduction: process.env.NODE_ENV === 'production',
} as const;

// Validate configuration on module load
if (config.isProduction && config.apiBase.startsWith('http://localhost')) {
  console.warn('Warning: Using localhost API in production. Please set NEXT_PUBLIC_API_BASE.');
}

export type Config = typeof config;
