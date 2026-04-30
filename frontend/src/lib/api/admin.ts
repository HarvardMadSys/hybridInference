import { fetchWithAuth, jsonOrThrow } from './client';
import { config } from '@/config/env';

const API_BASE = config.apiBase;

// ========================================
// User Management
// ========================================

export interface AdminUser {
  id: string;
  email: string;
  user_name: string | null;
  role: string;
  status: string;
  email_verified: boolean;
  approval_note: string | null;
  reviewed_at: string | null;
  reviewed_by: string | null;
  created_at: string;
  last_login_at: string | null;
  has_key: boolean;
  key_prefix: string | null;
  key_status: string | null;
  key_tier: string | null;
  usage_today_usd: number;
  usage_month_usd: number;
  usage_alltime_usd: number;
}

export type UserSortBy = 'created' | 'cost_today' | 'cost_month' | 'cost_alltime' | 'last_login';

export interface StatusCounts {
  all: number;
  pending_approval: number;
  active: number;
  suspended: number;
  rejected: number;
  deleted: number;
}

export interface ListUsersResponse {
  total: number;
  users: AdminUser[];
  status_counts: StatusCounts;
}

export interface ApproveRejectResponse {
  user_id: string;
  email: string;
  status: string;
  message: string;
}

export async function listUsers(
  status?: string,
  limit = 100,
  offset = 0,
  search?: string,
  sortBy?: UserSortBy,
): Promise<ListUsersResponse> {
  const params = new URLSearchParams();
  if (status) params.set('status', status);
  if (search) params.set('search', search);
  if (sortBy) params.set('sort_by', sortBy);
  params.set('limit', String(limit));
  params.set('offset', String(offset));
  const resp = await fetchWithAuth(API_BASE, `/admin/users?${params.toString()}`);
  return jsonOrThrow<ListUsersResponse>(resp);
}

export async function approveUser(userId: string, note?: string): Promise<ApproveRejectResponse> {
  const resp = await fetchWithAuth(API_BASE, `/admin/users/${encodeURIComponent(userId)}/approve`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(note ? { note } : {}),
  });
  return jsonOrThrow<ApproveRejectResponse>(resp);
}

export async function rejectUser(userId: string, reason: string): Promise<ApproveRejectResponse> {
  const resp = await fetchWithAuth(API_BASE, `/admin/users/${encodeURIComponent(userId)}/reject`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reason }),
  });
  return jsonOrThrow<ApproveRejectResponse>(resp);
}

// ========================================
// API Key Management
// ========================================

export interface AdminApiKey {
  user_id: string;
  user_name: string | null;
  key_prefix: string;
  tier: string;
  status: string;
  quota_daily_cost_usd: number;
  quota_monthly_cost_usd: number | null;
  created_at: string;
  last_used_at: string | null;
  expires_at: string | null;
  usage_today_usd: number;
  usage_month_usd: number;
  notes: string | null;
}

export interface ListApiKeysResponse {
  total: number;
  keys: AdminApiKey[];
}

export interface CreateApiKeyRequest {
  user_id: string;
  user_name?: string;
  tier?: string;
  quota_daily_cost_usd?: number;
  quota_monthly_cost_usd?: number | null;
  expires_at?: string | null;
  notes?: string | null;
}

export interface CreateApiKeyResponse {
  api_key: string;
  user_id: string;
  key_prefix: string;
  tier: string;
  quota_daily_cost_usd: number;
  quota_monthly_cost_usd: number | null;
  expires_at: string | null;
  created_at: string;
  warning: string;
}

export async function listApiKeys(
  status?: string,
  tier?: string,
  limit = 100,
  offset = 0,
): Promise<ListApiKeysResponse> {
  const params = new URLSearchParams();
  if (status) params.set('status', status);
  if (tier) params.set('tier', tier);
  params.set('limit', String(limit));
  params.set('offset', String(offset));
  const resp = await fetchWithAuth(API_BASE, `/admin/api-keys?${params.toString()}`);
  return jsonOrThrow<ListApiKeysResponse>(resp);
}

export async function createApiKeyAdmin(data: CreateApiKeyRequest): Promise<CreateApiKeyResponse> {
  const resp = await fetchWithAuth(API_BASE, '/admin/api-keys', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      user_id: data.user_id,
      user_name: data.user_name || null,
      tier: data.tier || 'free',
      quota_daily_cost_usd: data.quota_daily_cost_usd ?? 1000,
      quota_monthly_cost_usd: data.quota_monthly_cost_usd ?? null,
      expires_at: data.expires_at || null,
      notes: data.notes || null,
      metadata: null,
    }),
  });
  return jsonOrThrow<CreateApiKeyResponse>(resp);
}

export async function revokeApiKeyAdmin(userId: string): Promise<{ message: string }> {
  const resp = await fetchWithAuth(API_BASE, `/admin/api-keys/${encodeURIComponent(userId)}`, {
    method: 'DELETE',
  });
  return jsonOrThrow<{ message: string }>(resp);
}

export async function regenerateApiKeyAdmin(
  userId: string,
): Promise<{ api_key: string; key_prefix: string }> {
  const resp = await fetchWithAuth(
    API_BASE,
    `/admin/api-keys/${encodeURIComponent(userId)}/regenerate`,
    { method: 'POST' },
  );
  return jsonOrThrow<{ api_key: string; key_prefix: string }>(resp);
}

// ========================================
// User Detail & Edit
// ========================================

export interface UserDetail {
  id: string;
  email: string;
  user_name: string | null;
  role: string;
  status: string;
  email_verified: boolean;
  created_at: string;
  last_login_at: string | null;
  has_key: boolean;
  key_prefix: string | null;
  key_tier: string | null;
  quota_daily_usd: number | null;
  quota_monthly_usd: number | null;
  usage_today_usd: number;
  usage_today_requests: number;
  usage_month_usd: number;
  usage_month_requests: number;
  models_used: string[];
  last_request_at: string | null;
}

export async function getUserDetail(userId: string): Promise<UserDetail> {
  const resp = await fetchWithAuth(API_BASE, `/admin/users/${encodeURIComponent(userId)}/detail`);
  return jsonOrThrow<UserDetail>(resp);
}

export interface UpdateUserData {
  role?: string;
  tier?: string;
  status?: string;
  quota_daily_cost_usd?: number;
  quota_monthly_cost_usd?: number;
}

export async function updateUser(
  userId: string,
  data: UpdateUserData,
): Promise<{ user_id: string; updated_fields: string[]; message: string }> {
  const resp = await fetchWithAuth(API_BASE, `/admin/users/${encodeURIComponent(userId)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
  return jsonOrThrow<{ user_id: string; updated_fields: string[]; message: string }>(resp);
}

export async function deleteUser(userId: string, reason: string): Promise<ApproveRejectResponse> {
  const resp = await fetchWithAuth(API_BASE, `/admin/users/${encodeURIComponent(userId)}/delete`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reason }),
  });
  return jsonOrThrow<ApproveRejectResponse>(resp);
}

// ========================================
// Audit Log
// ========================================

export interface AuditLogEntry {
  id: number;
  timestamp: string;
  admin_ip: string;
  action: string;
  target_user_id: string | null;
  details: Record<string, unknown> | null;
  success: boolean;
}

export interface ListAuditLogResponse {
  total: number;
  entries: AuditLogEntry[];
}

export async function listAuditLog(
  action?: string,
  targetUserId?: string,
  limit = 50,
  offset = 0,
): Promise<ListAuditLogResponse> {
  const params = new URLSearchParams();
  if (action) params.set('action', action);
  if (targetUserId) params.set('target_user_id', targetUserId);
  params.set('limit', String(limit));
  params.set('offset', String(offset));
  const resp = await fetchWithAuth(API_BASE, `/admin/audit-log?${params.toString()}`);
  return jsonOrThrow<ListAuditLogResponse>(resp);
}

// ========================================
// Recent Requests (Admin View)
// ========================================

export interface AdminRequestMetricsBucket {
  start_time: string;
  request_count: number;
  success_count: number;
  error_count: number;
  avg_latency_ms?: number | null;
}

export interface AdminRequestMetricsWindow {
  key: string;
  label: string;
  window_minutes: number;
  bucket_minutes: number;
  total_requests: number;
  success_requests: number;
  error_requests: number;
  avg_latency_ms?: number | null;
  buckets: AdminRequestMetricsBucket[];
}

export interface AdminRequestMetricsResponse {
  generated_at: string;
  windows: AdminRequestMetricsWindow[];
}

export async function getRequestMetrics(): Promise<AdminRequestMetricsResponse> {
  const resp = await fetchWithAuth(API_BASE, '/admin/request-metrics');
  return jsonOrThrow<AdminRequestMetricsResponse>(resp);
}

export interface AdminRecentRequestItem {
  request_id: string;
  user_id: string | null;
  user_name?: string | null;
  user_email?: string | null;
  user_ip?: string | null;
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
  total_tokens?: number | null;
  cost_usd?: number | null;
  prompt?: string | null;
  response?: string | null;
  error?: string | null;
}

export interface AdminRecentRequestsResponse {
  requests: AdminRecentRequestItem[];
  total: number;
  limit: number;
  offset: number;
}

export async function listRecentRequests(
  limit = 50,
  offset = 0,
  userId?: string,
  modelId?: string,
  errorsOnly = false,
): Promise<AdminRecentRequestsResponse> {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (userId) params.set('user_id', userId);
  if (modelId) params.set('model_id', modelId);
  if (errorsOnly) params.set('errors_only', 'true');
  const resp = await fetchWithAuth(API_BASE, `/admin/recent-requests?${params.toString()}`);
  return jsonOrThrow<AdminRecentRequestsResponse>(resp);
}

// ========================================
// Analytics
// ========================================

export type AnalyticsPeriod = 'hour' | 'day' | 'week' | 'month';

export interface SparklineBucket {
  start_time: string;
  request_count: number;
}

export interface AnalyticsUserEntry {
  email: string;
  user_id: string;
  requests: number;
  fraction: number; // 0.0–1.0 share of all requests in period
}

export interface AnalyticsBreakdownEntry {
  name: string; // model_id, provider, or "others"
  requests: number;
  fraction: number;
}

export interface AdminAnalyticsResponse {
  period: AnalyticsPeriod;
  active_users: number;
  sparkline: SparklineBucket[];
  top_users: AnalyticsUserEntry[];
  by_model: AnalyticsBreakdownEntry[];
  by_provider: AnalyticsBreakdownEntry[];
  generated_at: string;
}

export async function getAnalytics(period: AnalyticsPeriod): Promise<AdminAnalyticsResponse> {
  const resp = await fetchWithAuth(API_BASE, `/admin/analytics?period=${period}`);
  return jsonOrThrow<AdminAnalyticsResponse>(resp);
}

// ========================================
// Provider Quotas
// ========================================

export interface ProviderQuotaUsage {
  label: string;
  used: number | null;
  limit: number | null;
  unit: string;
  reset_at: string | null;
}

export interface ProviderQuotaResult {
  name: string;
  display_name: string;
  key_configured: boolean;
  key_masked: string | null;
  fetched_at: string | null;
  ok: boolean;
  error: string | null;
  usages: ProviderQuotaUsage[];
}

export interface AdminProviderQuotasResponse {
  generated_at: string;
  providers: ProviderQuotaResult[];
}

export async function getProviderQuotas(): Promise<AdminProviderQuotasResponse> {
  const resp = await fetchWithAuth(API_BASE, '/admin/provider-quotas');
  return jsonOrThrow<AdminProviderQuotasResponse>(resp);
}
