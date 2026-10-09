// Public (unauthenticated) client for the daily usage snapshot behind /stats.

import { config } from '@/config/env';

const API_BASE = config.apiBase;

export type ClientKind = 'coding' | 'general' | 'custom' | 'chat' | 'direct';

export interface PublicStats {
  schema_version: 1;
  generated_at: string;
  window: { start: string; end: string; days: number };
  weeks: string[];
  first_week_partial: boolean;
  last_week_partial: boolean;
  totals: {
    tokens: number;
    input_tokens: number;
    output_tokens: number;
    requests: number;
    accounts: number;
    cached_input_share: number;
  };
  daily: { date: string; input_tokens: number; output_tokens: number; requests: number }[];
  countries: {
    total: number;
    total_min_requests: number;
    continents: number;
    weekly: { any: number; min_requests: number }[];
    top: { code: string; alpha2: string | null; share: number }[];
    all: { code: string; alpha2: string | null; continent: string; level: number }[];
  };
  languages: {
    total: number;
    weekly: number[];
    items: { code: string; accounts: number | null }[];
    accounts_classified: number;
    accounts_non_english: number;
    messages_sampled: number;
  } | null;
  agents: {
    clients_total: number;
    clients_multi_account: number;
    products_total: number;
    weekly: { clients: number; products: number }[];
    products: { name: string; kind: ClientKind; tokens: number; accounts: number | null }[];
    kinds: { kind: ClientKind; token_share: number }[];
    kind_weekly: Record<ClientKind, number>[];
  };
  thresholds: {
    min_client_requests: number;
    min_country_requests: number;
    min_public_accounts: number;
  };
}

export type PublicStatsResult =
  | { status: 'ok'; stats: PublicStats }
  | { status: 'unavailable' }
  | { status: 'error' };

/**
 * Fetch the newest public usage snapshot.
 *
 * Resolves to ``unavailable`` when the deployment does not publish stats (or
 * has not produced a snapshot yet) and to ``error`` on any other failure, so
 * the page can say which it is instead of throwing.
 */
export async function getPublicStats(): Promise<PublicStatsResult> {
  try {
    const resp = await fetch(`${API_BASE}/public-stats`, {
      headers: { Accept: 'application/json' },
    });
    if (resp.status === 404) return { status: 'unavailable' };
    if (!resp.ok) return { status: 'error' };
    const stats = (await resp.json()) as PublicStats;
    if (stats?.schema_version !== 1) return { status: 'error' };
    return { status: 'ok', stats };
  } catch {
    return { status: 'error' };
  }
}
