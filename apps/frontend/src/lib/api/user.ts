import { fetchWithAuth, jsonOrThrow } from './client';
import { config } from '@/config/env';
import type { AccountLocaleChoice } from '@/lib/i18n/choice';

const API_BASE = config.apiBase;

export interface User {
  id: string;
  /** `null` for an account that signs in with a login name instead. */
  email: string | null;
  login_name?: string | null;
  phone?: string | null;
  phone_verified?: boolean;
  has_password?: boolean;
  user_name?: string;
  avatar_url?: string | null;
  role: string;
  status: string;
  email_verified: boolean;
  is_admin: boolean;
  created_at: string;
  /** The interface language this account last chose; absent when it never chose. */
  locale?: AccountLocaleChoice | null;
}

export interface ApiKeyResponse {
  api_key: string;
  key_prefix: string;
  warning: string;
  created_at: string;
}

export interface ApiKeyListItem {
  api_key?: string | null;
  key_prefix: string;
  key_masked: string;
  created_at: string;
  last_used_at?: string | null;
  status: string;
}

export interface ApiKeyListResponse {
  keys: ApiKeyListItem[];
}

export interface ApiKeyDeleteResponse {
  key_prefix: string;
  status: string;
  message: string;
}

export interface UsageStats {
  currency?: 'USD' | 'CNY';
  period: 'today' | 'month' | 'all';
  quota: {
    currency?: 'USD' | 'CNY';
    has_key: boolean;
    daily_limit?: number;
    daily_limit_usd?: number;
    monthly_limit?: number;
    monthly_limit_usd?: number;
    spent_today?: number;
    spent_today_usd?: number;
    spent_month?: number;
    spent_month_usd?: number;
    remaining_today?: number;
    remaining_today_usd?: number;
    max_concurrency?: number;
    reset_at?: string | null;
    reset_timezone?: string;
    contact_email?: string;
    increase_request_message?: string;
  };
  usage: {
    currency?: 'USD' | 'CNY';
    requests: number;
    prompt_tokens: number;
    completion_tokens: number;
    cost?: number;
    cost_usd: number;
  };
}

export interface ModelCatalogItem {
  currency?: 'USD' | 'CNY';
  id: string;
  name: string;
  object: 'model';
  created: number;
  owned_by: string;
  input_modalities: string[];
  output_modalities: string[];
  quantization: string;
  context_length: number;
  max_output_length: number;
  pricing: Record<string, string>;
  prices?: Record<string, string>;
  supported_sampling_parameters: string[];
  supported_features: string[];
  openrouter?: Record<string, unknown> | null;
}

export interface ModelCatalogResponse {
  currency?: 'USD' | 'CNY';
  object: 'list';
  data: ModelCatalogItem[];
}

export async function getMe(): Promise<User> {
  const resp = await fetchWithAuth(API_BASE, '/user/me');
  return jsonOrThrow<User>(resp);
}

/**
 * Store the account's interface language.
 *
 * `chosenAt` is passed only when the choice was made earlier, signed out, and
 * is being carried into the account; the server takes the current time
 * otherwise, and never a time later than its own clock.
 */
export async function updateLocalePreference(
  locale: string,
  chosenAt?: number,
): Promise<AccountLocaleChoice> {
  const resp = await fetchWithAuth(API_BASE, '/user/preferences/locale', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(
      chosenAt === undefined ? { locale } : { locale, chosen_at: new Date(chosenAt).toISOString() },
    ),
  });
  return jsonOrThrow<AccountLocaleChoice>(resp);
}

export async function getModels(): Promise<ModelCatalogResponse> {
  const resp = await fetchWithAuth(API_BASE, '/user/models');
  return jsonOrThrow<ModelCatalogResponse>(resp);
}

export async function createApiKey(): Promise<ApiKeyResponse> {
  const resp = await fetchWithAuth(API_BASE, '/user/api-keys', {
    method: 'POST',
  });
  return jsonOrThrow<ApiKeyResponse>(resp);
}

export async function listApiKeys(): Promise<ApiKeyListResponse> {
  const resp = await fetchWithAuth(API_BASE, '/user/api-keys/all');
  return jsonOrThrow<ApiKeyListResponse>(resp);
}

export async function deleteApiKey(keyPrefix: string): Promise<ApiKeyDeleteResponse> {
  const resp = await fetchWithAuth(API_BASE, `/user/api-keys/${encodeURIComponent(keyPrefix)}`, {
    method: 'DELETE',
  });
  return jsonOrThrow<ApiKeyDeleteResponse>(resp);
}

export async function regenerateApiKey(): Promise<ApiKeyResponse> {
  const resp = await fetchWithAuth(API_BASE, '/user/api-keys/regenerate', {
    method: 'POST',
  });
  return jsonOrThrow<ApiKeyResponse>(resp);
}

export async function getUsage(period: 'today' | 'month' | 'all' = 'today'): Promise<UsageStats> {
  // Periods start at 00:00 UTC, when the daily quota resets.
  const params = new URLSearchParams({ period });
  const resp = await fetchWithAuth(API_BASE, `/user/usage?${params.toString()}`);
  return jsonOrThrow<UsageStats>(resp);
}

export async function updateProfile(data: {
  user_name?: string;
  avatar_url?: string | null;
}): Promise<User> {
  const resp = await fetchWithAuth(API_BASE, '/user/profile', {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
  return jsonOrThrow<User>(resp);
}

/**
 * Change the password, or set a first one. An account without a password
 * confirms its owner with `phoneCode`, a `set_password` code.
 */
export async function updatePassword(
  currentPassword: string,
  newPassword: string,
  phoneCode?: string,
): Promise<{ message: string }> {
  const resp = await fetchWithAuth(API_BASE, '/user/change-password', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      old_password: currentPassword,
      new_password: newPassword,
      ...(phoneCode ? { phone_code: phoneCode } : {}),
    }),
  });
  return jsonOrThrow<{ message: string }>(resp);
}

// Recent requests types and API
export interface RecentRequestItem {
  currency?: 'USD' | 'CNY';
  request_id: string;
  model_id: string;
  provider: string;
  timestamp: string;
  status_code?: number | null;
  latency_ms?: number | null;
  ttft_ms?: number | null;
  stream?: boolean | null;
  prompt_tokens?: number | null;
  completion_tokens?: number | null;
  reasoning_tokens?: number | null;
  cache_read_tokens?: number | null;
  cache_write_tokens?: number | null;
  total_tokens?: number | null;
  cost?: number | null;
  cost_usd?: number | null;
  error?: string | null;
  routewise?: RouteWiseDecision | null;
  // "embedding" for /v1/embeddings traffic; null/undefined implies a
  // chat/completion request.
  request_type?: string | null;
}

export interface RouteWiseDecision {
  selected_provider_type?: string | null;
  selected_provider?: string | null;
  selected_endpoint_id?: string | null;
  hedging_triggered?: boolean | null;
  hedge_backup_provider?: string | null;
  hedge_backup_endpoint_id?: string | null;
  backup_won?: boolean | null;
  lp_status?: string | null;
}

export interface RecentRequestsResponse {
  currency?: 'USD' | 'CNY';
  requests: RecentRequestItem[];
  total: number;
  limit: number;
  offset: number;
}

export async function getRecentRequests(
  limit: number = 50,
  offset: number = 0,
  modelId?: string,
): Promise<RecentRequestsResponse> {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (modelId) params.set('model_id', modelId);
  const resp = await fetchWithAuth(API_BASE, `/user/recent-requests?${params.toString()}`);
  return jsonOrThrow<RecentRequestsResponse>(resp);
}
