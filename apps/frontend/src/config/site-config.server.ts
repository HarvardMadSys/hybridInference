import {
  buildTimeSiteConfig,
  resolveRuntimeSiteConfig,
  withAgentsFeature,
  type RuntimeSiteConfig,
} from './site-config';

const DEFAULT_BACKEND_INTERNAL_URL = 'http://backend:8080';
const SITE_CONFIG_TIMEOUT_MS = 3_000;

async function withTimeout<T>(promise: Promise<T>, timeoutMs: number): Promise<T> {
  let timeout: ReturnType<typeof setTimeout> | undefined;
  const deadline = new Promise<never>((_resolve, reject) => {
    timeout = setTimeout(
      () => reject(new Error('Runtime site config request timed out.')),
      timeoutMs,
    );
  });

  try {
    return await Promise.race([promise, deadline]);
  } finally {
    if (timeout !== undefined) {
      clearTimeout(timeout);
    }
  }
}

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
    // A caller-provided AbortSignal opts this GET out of Next.js request memoization.
    // Keep the timeout outside fetch so metadata, layouts, and pages share one response
    // during a render, while no-store still prevents reuse across requests.
    const response = await withTimeout(
      fetch(`${backendUrl}/site-config`, {
        cache: 'no-store',
        headers: { accept: 'application/json' },
      }),
      SITE_CONFIG_TIMEOUT_MS,
    );
    if (response.ok) {
      siteConfig = resolveRuntimeSiteConfig(await response.json());
    }
  } catch (error) {
    console.warn('Unable to load runtime site config; using build-time defaults.', error);
  }

  return withAgentsFeature(siteConfig, hasRuntimeAgentProxy());
}
