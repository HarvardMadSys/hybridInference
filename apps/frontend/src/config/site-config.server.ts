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

/**
 * How long a parsed document answers every render before the next one asks the
 * gateway again. Each page render reads it -- and so did the container's health
 * check, every 15s -- while the document changes only when an admin edits the
 * deployment's identity, which then shows within this long.
 */
const FRESH_FOR_MS = 30_000;

/** How long the last good document stands in for a gateway that just failed. */
const RETRY_AFTER_FAILURE_MS = 5_000;

let remembered: { config: RuntimeSiteConfig; refreshAt: number } | null = null;
let refreshing: Promise<RuntimeSiteConfig> | null = null;

/**
 * Whether a document may be kept. One that says first-run setup is pending, or
 * a required setting is missing, describes a deployment an admin is about to
 * change: setup ends with a full page load that must see `setup.required` turn
 * false (see SetupGate), so those are read fresh on every render.
 */
function settled(config: RuntimeSiteConfig): boolean {
  return !config.setup.required && !config.configuration.incomplete;
}

/**
 * The document, shared across requests for `FRESH_FOR_MS`, and fetched once
 * however many renders ask at the same moment.
 *
 * When the gateway fails, the last good document keeps answering -- the pages
 * render as they did a moment ago rather than as the error page -- and the
 * gateway is asked again after `RETRY_AFTER_FAILURE_MS`, not on every render.
 * With no good document yet, the failure propagates as before.
 */
async function loadRuntimeSiteConfigShared(): Promise<RuntimeSiteConfig> {
  if (remembered && Date.now() < remembered.refreshAt) return remembered.config;

  refreshing ??= loadRuntimeSiteConfigUncached().finally(() => {
    refreshing = null;
  });
  try {
    const config = await refreshing;
    remembered = settled(config) ? { config, refreshAt: Date.now() + FRESH_FOR_MS } : null;
    return config;
  } catch (error) {
    if (!remembered) throw error;
    remembered = { config: remembered.config, refreshAt: Date.now() + RETRY_AFTER_FAILURE_MS };
    return remembered.config;
  }
}

/** Forget the shared document. For tests, which each start from nothing. */
export function forgetRuntimeSiteConfig(): void {
  remembered = null;
  refreshing = null;
}

// React cache is scoped to one server render. That lets metadata, layouts, and
// pages share one snapshot even when the shared copy above is refreshed while
// the render is under way. The fetch itself stays out of Next.js's fetch cache
// (no-store, and an abort signal); the copy above is the only one kept.
export const loadRuntimeSiteConfig = cachePerRender(loadRuntimeSiteConfigShared);
