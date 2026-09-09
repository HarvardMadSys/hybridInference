import * as React from 'react';

import { resolveRuntimeSiteConfig, withAgentsUrl, type RuntimeSiteConfig } from './site-config';
import { SiteConfigLoadError } from './site-config-error';

const DEFAULT_BACKEND_INTERNAL_URL = 'http://backend:8080';
const SITE_CONFIG_TIMEOUT_MS = 3_000;

type CacheFunction = <T extends (...args: never[]) => unknown>(fn: T) => T;

// Next's RSC compiler aliases React to its server runtime, which exposes
// cache(). Vitest loads the React 18 client package instead, so direct unit
// execution uses the identity fallback without creating a process-wide cache.
const cachePerRender: CacheFunction =
  (React as typeof React & { cache?: CacheFunction }).cache ?? ((fn) => fn);

function runtimeAgentsUrl(): string {
  const publicUrl = process.env.AGENT_PUBLIC_URL?.trim();
  if (publicUrl) {
    try {
      const parsed = new URL(publicUrl);
      if (
        publicUrl.startsWith('https://') &&
        !/[\\\s]/.test(publicUrl) &&
        parsed.hostname &&
        !parsed.username &&
        !parsed.password
      ) {
        return publicUrl;
      }
    } catch {
      // Fall back to the local proxy, if configured, without exposing input.
    }
    console.warn('Ignoring AGENT_PUBLIC_URL: expected an HTTPS URL without credentials.');
  }

  return process.env.AGENT_WEB_INTERNAL_URL?.trim() &&
    process.env.AGENT_CONTROL_PLANE_INTERNAL_URL?.trim()
    ? '/agents'
    : '';
}

async function loadRuntimeSiteConfigUncached(): Promise<RuntimeSiteConfig> {
  const backendUrl = (
    process.env.BUILT_BACKEND_INTERNAL_URL || DEFAULT_BACKEND_INTERNAL_URL
  ).replace(/\/+$/, '');
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), SITE_CONFIG_TIMEOUT_MS);

  try {
    const response = await fetch(`${backendUrl}/site-config`, {
      cache: 'no-store',
      headers: { accept: 'application/json' },
      signal: controller.signal,
    });
    if (!response.ok) {
      throw new SiteConfigLoadError(
        `Runtime site configuration request returned HTTP ${response.status}.`,
      );
    }

    let document: unknown;
    try {
      // Keep body consumption under the same deadline as the headers.
      document = await response.json();
    } catch (error) {
      if (controller.signal.aborted) throw error;
      throw new SiteConfigLoadError('Runtime site configuration response is not valid JSON.');
    }

    return withAgentsUrl(resolveRuntimeSiteConfig(document), runtimeAgentsUrl());
  } catch (error) {
    const reason = controller.signal.aborted
      ? `Runtime site configuration request timed out after ${SITE_CONFIG_TIMEOUT_MS} ms.`
      : 'Unable to connect to the runtime site configuration endpoint.';
    const failure = error instanceof SiteConfigLoadError ? error : new SiteConfigLoadError(reason);
    // Do not log response bodies, URLs, or raw fetch/JSON errors: they may
    // contain operator-supplied values. The reason remains visible in logs.
    console.error(failure.message);
    throw failure;
  } finally {
    clearTimeout(timeout);
  }
}

// React cache is scoped to one server render. That lets metadata, layouts, and
// pages share the parsed snapshot even though the abort signal intentionally
// opts the underlying fetch out of Next.js fetch memoization. no-store still
// prevents the result from crossing request boundaries.
export const loadRuntimeSiteConfig = cachePerRender(loadRuntimeSiteConfigUncached);
