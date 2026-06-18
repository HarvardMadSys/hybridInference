// Public (unauthenticated) client for homepage site updates.

import { config } from '@/config/env';

const API_BASE = config.apiBase;

export interface PublicSiteUpdate {
  id: string;
  title: string;
  body: string;
  link_url: string | null;
  link_label: string | null;
  created_at: string;
}

export interface PublicSiteUpdatesResponse {
  banner: PublicSiteUpdate | null;
  updates: PublicSiteUpdate[];
}

const EMPTY: PublicSiteUpdatesResponse = { banner: null, updates: [] };

// The banner and feed components both call this on mount. Share the in-flight
// request so a single homepage load issues one GET, not two. Cleared once it
// settles so later navigations refetch.
let inflight: Promise<PublicSiteUpdatesResponse> | null = null;

/**
 * Fetch published homepage updates (banner + feed).
 *
 * The homepage is statically served, so this runs client-side. Any failure
 * (network, 5xx) resolves to an empty payload rather than throwing — a missing
 * updates section should never break the landing page.
 */
export function getPublicSiteUpdates(): Promise<PublicSiteUpdatesResponse> {
  if (inflight) return inflight;
  const request = (async () => {
    try {
      const resp = await fetch(`${API_BASE}/site-updates`, {
        headers: { Accept: 'application/json' },
      });
      if (!resp.ok) return EMPTY;
      return (await resp.json()) as PublicSiteUpdatesResponse;
    } catch {
      return EMPTY;
    }
  })();
  inflight = request;
  void request.finally(() => {
    if (inflight === request) inflight = null;
  });
  return request;
}
