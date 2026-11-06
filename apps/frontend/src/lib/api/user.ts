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
  name?: string | null;
  warning: string;
  created_at: string;
}

export interface ApiKeyListItem {
  api_key?: string | null;
  key_prefix: string;
  key_masked: string;
  /** The owner's label for the key; absent for keys made before keys had names. */
  name?: string | null;
  created_at: string;
  last_used_at?: string | null;
  status: string;
  /** What requests with this key cost on the current UTC day and month. */
  spent_today_usd?: number;
  spent_month_usd?: number;
  /** The key's own limits; null or absent is no limit at the key. */
  budget_daily_usd?: number | null;
  budget_monthly_usd?: number | null;
  /** Model ids the key may call; null or absent is every model the account may use. */
  allowed_models?: string[] | null;
}

export interface ApiKeyListResponse {
  keys: ApiKeyListItem[];
}

/** Settings to change on a key: a field left out is unchanged, `null` clears it. */
export interface ApiKeyUpdate {
  name?: string | null;
  budget_daily_usd?: number | null;
  budget_monthly_usd?: number | null;
  allowed_models?: string[] | null;
}

export interface ApiKeyUpdateResponse {
  key_prefix: string;
  name?: string | null;
  budget_daily_usd?: number | null;
  budget_monthly_usd?: number | null;
  allowed_models?: string[] | null;
}

export interface ApiKeyRegenerateResponse {
  api_key: string;
  key_prefix: string;
  old_key_prefix: string;
  warning: string;
}

export interface ApiKeyDeleteResponse {
  key_prefix: string;
  status: string;
  message: string;
}

export interface UsageStats {
  period: 'today' | 'month' | 'all';
  quota: {
    has_key: boolean;
    daily_limit_usd?: number;
    monthly_limit_usd?: number;
    spent_today_usd?: number;
    spent_month_usd?: number;
    remaining_today_usd?: number;
    max_concurrency?: number;
    reset_at?: string | null;
    reset_timezone?: string;
    contact_email?: string;
    increase_request_message?: string;
  };
  usage: {
    requests: number;
    prompt_tokens: number;
    completion_tokens: number;
    /** Input tokens served from the prompt cache; part of `prompt_tokens`. */
    cache_read_tokens?: number;
    /** Input tokens written to the prompt cache; part of `prompt_tokens`. */
    cache_write_tokens?: number;
    /** Reasoning tokens; some providers already count them in `completion_tokens`. */
    reasoning_tokens?: number;
    cost_usd: number;
  };
}

export interface ModelCatalogItem {
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
  supported_sampling_parameters: string[];
  supported_features: string[];
  openrouter?: Record<string, unknown> | null;
}

export interface ModelCatalogResponse {
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

/** Create another key for the account; `name` labels it and may be omitted. */
export async function createApiKey(name?: string | null): Promise<ApiKeyResponse> {
  const resp = await fetchWithAuth(API_BASE, '/user/api-keys', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: name ?? null }),
  });
  return jsonOrThrow<ApiKeyResponse>(resp);
}

/** Change a key's name or limits; only the fields given change. */
export async function updateApiKey(
  keyPrefix: string,
  changes: ApiKeyUpdate,
): Promise<ApiKeyUpdateResponse> {
  const resp = await fetchWithAuth(API_BASE, `/user/api-keys/${encodeURIComponent(keyPrefix)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(changes),
  });
  return jsonOrThrow<ApiKeyUpdateResponse>(resp);
}

/** Relabel a key; a blank name clears it. */
export async function renameApiKey(
  keyPrefix: string,
  name: string | null,
): Promise<ApiKeyUpdateResponse> {
  return updateApiKey(keyPrefix, { name });
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

/** Give an active key a new secret; the old one stops working at once. The key keeps its name, limits and spend. */
export async function regenerateApiKey(keyPrefix: string): Promise<ApiKeyRegenerateResponse> {
  const resp = await fetchWithAuth(
    API_BASE,
    `/user/api-keys/${encodeURIComponent(keyPrefix)}/regenerate`,
    { method: 'POST' },
  );
  return jsonOrThrow<ApiKeyRegenerateResponse>(resp);
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
  cost_usd?: number | null;
  error?: string | null;
  routewise?: RouteWiseDecision | null;
  // "embedding" for /v1/embeddings traffic; null/undefined implies a
  // chat/completion request.
  request_type?: string | null;
  /** The account's key the request was made with, when it was attributed to one. */
  key_prefix?: string | null;
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
  requests: RecentRequestItem[];
  total: number;
  limit: number;
  offset: number;
}

export async function getRecentRequests(
  limit: number = 50,
  offset: number = 0,
  modelId?: string,
  keyPrefix?: string,
): Promise<RecentRequestsResponse> {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (modelId) params.set('model_id', modelId);
  if (keyPrefix) params.set('key_prefix', keyPrefix);
  const resp = await fetchWithAuth(API_BASE, `/user/recent-requests?${params.toString()}`);
  return jsonOrThrow<RecentRequestsResponse>(resp);
}
