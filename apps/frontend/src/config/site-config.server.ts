import {
  buildTimeSiteConfig,
  resolveRuntimeSiteConfig,
  withAgentsFeature,
  type RuntimeSiteConfig,
} from './site-config';

const DEFAULT_BACKEND_INTERNAL_URL = 'http://backend:8080';

function hasRuntimeAgentProxy(): boolean {
  return Boolean(
    process.env.AGENT_WEB_INTERNAL_URL?.trim() &&
    process.env.AGENT_CONTROL_PLANE_INTERNAL_URL?.trim(),
  );
}

export async function loadRuntimeSiteConfig(): Promise<RuntimeSiteConfig> {
  let siteConfig = buildTimeSiteConfig;
  const backendUrl = (process.env.BACKEND_INTERNAL_URL || DEFAULT_BACKEND_INTERNAL_URL).replace(
    /\/+$/,
    '',
  );

  try {
    const response = await fetch(`${backendUrl}/site-config`, {
      cache: 'no-store',
      headers: { accept: 'application/json' },
      signal: AbortSignal.timeout(3_000),
    });
    if (response.ok) {
      siteConfig = resolveRuntimeSiteConfig(await response.json());
    }
  } catch (error) {
    console.warn('Unable to load runtime site config; using build-time defaults.', error);
  }

  return withAgentsFeature(siteConfig, hasRuntimeAgentProxy());
}
