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

/**
 * Fetch published homepage updates (banner + feed).
 *
 * The homepage is statically served, so this runs client-side. Any failure
 * (network, 5xx) resolves to an empty payload rather than throwing — a missing
 * updates section should never break the landing page.
 */
export async function getPublicSiteUpdates(): Promise<PublicSiteUpdatesResponse> {
  try {
    const resp = await fetch(`${API_BASE}/site-updates`, {
      headers: { Accept: 'application/json' },
    });
    if (!resp.ok) return EMPTY;
    return (await resp.json()) as PublicSiteUpdatesResponse;
  } catch {
    return EMPTY;
  }
}
