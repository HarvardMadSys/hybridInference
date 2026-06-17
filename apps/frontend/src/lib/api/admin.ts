import { fetchWithAuth, jsonOrThrow } from './client';
import { config } from '@/config/env';
import type {
  CostHistoryPoint,
  QuotaStateFilter,
  UsersSummary,
} from '@/app/dashboard/admin/users/types';

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

export interface ListUsersOptions {
  status?: string;
  search?: string;
  sortBy?: UserSortBy;
  limit?: number;
  offset?: number;
  minCostToday?: number;
  minCostMonth?: number;
  quotaState?: QuotaStateFilter;
  provider?: string;
  activeWithinHours?: number;
  anomaly?: boolean;
}

export async function listUsers(opts: ListUsersOptions = {}): Promise<ListUsersResponse> {
  const params = new URLSearchParams();
  if (opts.status) params.set('status', opts.status);
  if (opts.search) params.set('search', opts.search);
  if (opts.sortBy) params.set('sort_by', opts.sortBy);
  params.set('limit', String(opts.limit ?? 100));
  params.set('offset', String(opts.offset ?? 0));
  if (opts.minCostToday !== undefined) params.set('min_cost_today', String(opts.minCostToday));
  if (opts.minCostMonth !== undefined) params.set('min_cost_month', String(opts.minCostMonth));
  if (opts.quotaState) params.set('quota_state', opts.quotaState);
  if (opts.provider) params.set('provider', opts.provider);
  if (opts.activeWithinHours !== undefined) {
    params.set('active_within_hours', String(opts.activeWithinHours));
  }
  if (opts.anomaly !== undefined) params.set('anomaly', String(opts.anomaly));
  const resp = await fetchWithAuth(API_BASE, `/admin/users?${params.toString()}`);
  return jsonOrThrow<ListUsersResponse>(resp);
}

// ========================================
// Users cost-history + summary (new in admin Users redesign)
// ========================================

export interface UserCostHistoryResponse {
  user_id: string;
  days: number;
  points: CostHistoryPoint[];
}

export interface BulkCostHistoryResponse {
  days: number;
  histories: Record<string, CostHistoryPoint[]>;
}

export async function getUserCostHistory(
  userId: string,
  days = 7,
): Promise<UserCostHistoryResponse> {
  const resp = await fetchWithAuth(
    API_BASE,
    `/admin/users/${encodeURIComponent(userId)}/cost-history?days=${days}`,
  );
  return jsonOrThrow<UserCostHistoryResponse>(resp);
}

export async function getBulkCostHistory(
  userIds: string[],
  days = 7,
): Promise<BulkCostHistoryResponse> {
  if (userIds.length === 0) return { days, histories: {} };
  const params = new URLSearchParams({
    user_ids: userIds.join(','),
    days: String(days),
  });
  const resp = await fetchWithAuth(API_BASE, `/admin/users/cost-history?${params.toString()}`);
  return jsonOrThrow<BulkCostHistoryResponse>(resp);
}

export async function getUsersSummary(): Promise<UsersSummary> {
  const resp = await fetchWithAuth(API_BASE, '/admin/users/summary');
  return jsonOrThrow<UsersSummary>(resp);
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
  quota_daily_usd: number | null;
  quota_monthly_usd: number | null;
  usage_today_usd: number;
  usage_today_requests: number;
  usage_month_usd: number;
  usage_month_requests: number;
  models_used: string[];
  disabled_models: string[];
  last_request_at: string | null;
  max_concurrent_requests: number | null;
}

export async function getUserDetail(userId: string): Promise<UserDetail> {
  const resp = await fetchWithAuth(API_BASE, `/admin/users/${encodeURIComponent(userId)}/detail`);
  return jsonOrThrow<UserDetail>(resp);
}

export interface UpdateUserData {
  role?: string;
  status?: string;
  quota_daily_cost_usd?: number;
  quota_monthly_cost_usd?: number;
  disabled_models?: string[];
  max_concurrent_requests?: number | null;
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

export async function resumeUser(userId: string, reason?: string): Promise<ApproveRejectResponse> {
  const resp = await fetchWithAuth(API_BASE, `/admin/users/${encodeURIComponent(userId)}/resume`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reason: reason ?? null }),
  });
  return jsonOrThrow<ApproveRejectResponse>(resp);
}

export interface HardDeleteUserResponse {
  user_id: string;
  email: string;
  message: string;
}

export async function hardDeleteUser(
  userId: string,
  reason?: string,
): Promise<HardDeleteUserResponse> {
  const resp = await fetchWithAuth(
    API_BASE,
    `/admin/users/${encodeURIComponent(userId)}/hard-delete`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ confirm: true, reason: reason ?? null }),
    },
  );
  return jsonOrThrow<HardDeleteUserResponse>(resp);
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

// ----------------------------------------------------------------------------
// Performance metrics — prompt/response length, TTFT, Throughput distributions
// ----------------------------------------------------------------------------

export interface AdminHistogramBucket {
  lower_bound: number;
  upper_bound: number | null;
  count: number;
}

export interface AdminMetricDistribution {
  count: number;
  mean: number | null;
  min: number | null;
  max: number | null;
  p50: number | null;
  p90: number | null;
  p95: number | null;
  p99: number | null;
  histogram: AdminHistogramBucket[];
}

export interface AdminPerformanceMetricsWindow {
  key: string;
  label: string;
  window_minutes: number;
  prompt_tokens: AdminMetricDistribution;
  completion_tokens: AdminMetricDistribution;
  ttft_ms: AdminMetricDistribution;
  throughput_tps: AdminMetricDistribution;
}

export interface AdminPerformanceMetricsResponse {
  generated_at: string;
  windows: AdminPerformanceMetricsWindow[];
}

export async function getPerformanceMetrics(): Promise<AdminPerformanceMetricsResponse> {
  const resp = await fetchWithAuth(API_BASE, '/admin/performance-metrics');
  return jsonOrThrow<AdminPerformanceMetricsResponse>(resp);
}

// ----------------------------------------------------------------------------
// TTFT vs input length scatter — last 1000 successful streaming requests/model
// ----------------------------------------------------------------------------

export interface AdminTtftScatterPoint {
  prompt_tokens: number;
  ttft_ms: number;
  cache_hit: boolean;
  timestamp: string;
}

export interface AdminTtftScatterModel {
  model_id: string;
  provider: string;
  points: AdminTtftScatterPoint[];
}

export interface AdminTtftScatterResponse {
  models: AdminTtftScatterModel[];
}

export async function getTtftScatter(): Promise<AdminTtftScatterResponse> {
  const resp = await fetchWithAuth(API_BASE, '/admin/ttft-scatter');
  return jsonOrThrow<AdminTtftScatterResponse>(resp);
}

export interface AdminRecentRequestItem {
  request_id: string;
  user_id: string | null;
  user_name?: string | null;
  user_email?: string | null;
  user_ip?: string | null;
  peer_ip?: string | null;
  ip_source?: string | null;
  x_forwarded_for?: string | null;
  user_agent?: string | null;
  session_id?: string | null;
  request_surface?: string | null;
  model_id: string;
  provider: string;
  timestamp: string;
  status_code?: number | null;
  latency_ms?: number | null;
  ttft_ms?: number | null;
  decode_throughput_tps?: number | null;
  stream?: boolean | null;
  prompt_tokens?: number | null;
  completion_tokens?: number | null;
  reasoning_tokens?: number | null;
  cache_read_tokens?: number | null;
  cache_write_tokens?: number | null;
  total_tokens?: number | null;
  cost_usd?: number | null;
  error?: string | null;
  routewise?: AdminRouteWiseDecision | null;
  // "embedding" for /v1/embeddings traffic; null/undefined implies a
  // chat/completion request.
  request_type?: string | null;
  // Conversation shape derived from the stored request payload's messages
  // array. null when the payload is absent or not a chat request.
  num_turns?: number | null;
  num_user_turns?: number | null;
  num_tool_calls?: number | null;
}

export interface AdminRouteWiseDecision {
  selected_provider_type?: string | null;
  selected_provider?: string | null;
  selected_endpoint_id?: string | null;
  hedging_triggered?: boolean | null;
  hedge_backup_provider?: string | null;
  hedge_backup_endpoint_id?: string | null;
  backup_won?: boolean | null;
  lp_status?: string | null;
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

export interface AdminRecentRequestContentResponse {
  prompt: string | null;
  response: string | null;
  reasoning_content: string | null;
}

export async function getRecentRequestContent(
  requestId: string,
): Promise<AdminRecentRequestContentResponse> {
  const resp = await fetchWithAuth(
    API_BASE,
    `/admin/recent-requests/${encodeURIComponent(requestId)}/content`,
  );
  return jsonOrThrow<AdminRecentRequestContentResponse>(resp);
}

export interface ClearErrorRequestsResponse {
  deleted_count: number;
  hours: number;
  message: string;
}

export async function clearErrorRequests(hours = 1): Promise<ClearErrorRequestsResponse> {
  const resp = await fetchWithAuth(API_BASE, `/admin/recent-requests/clear-errors?hours=${hours}`, {
    method: 'POST',
  });
  return jsonOrThrow<ClearErrorRequestsResponse>(resp);
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
  fraction: number; // 0.0–1.0 share of user-attributed requests in period
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
// Broadcast Email
// ========================================

export interface BroadcastPreviewRequest {
  template_key?: string | null;
  template_vars?: Record<string, string>;
  subject?: string;
  body_html?: string;
  body_markdown?: string;
  body_text?: string;
  target_roles: string[];
  target_statuses: string[];
}

export interface BroadcastPreviewResponse {
  recipient_count: number;
  rendered_subject: string;
  rendered_body_html: string;
  rendered_body_text: string;
}

export interface CreateBroadcastRequest extends BroadcastPreviewRequest {
  scheduled_at?: string | null;
}

export interface CreateBroadcastResponse {
  id: string;
  status: string;
  recipient_count: number;
  scheduled_at: string | null;
}

export interface BroadcastListItem {
  id: string;
  subject: string;
  status: string;
  recipient_count: number;
  scheduled_at: string | null;
  sent_at: string | null;
  created_by: string;
  created_at: string;
}

export interface ListBroadcastsResponse {
  total: number;
  broadcasts: BroadcastListItem[];
}

export interface BroadcastRecipientItem {
  user_id: string;
  email: string;
  status: string;
  error: string | null;
  sent_at: string | null;
}

export interface BroadcastDetailResponse {
  broadcast: BroadcastListItem;
  recipients: BroadcastRecipientItem[];
  total_recipients: number;
}

export async function previewBroadcast(
  req: BroadcastPreviewRequest,
): Promise<BroadcastPreviewResponse> {
  const resp = await fetchWithAuth(API_BASE, '/admin/broadcast-email/preview', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(req),
  });
  return jsonOrThrow<BroadcastPreviewResponse>(resp);
}

export async function sendTestBroadcastEmail(req: BroadcastPreviewRequest): Promise<void> {
  const resp = await fetchWithAuth(API_BASE, '/admin/broadcast-email/test', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(req),
  });
  await jsonOrThrow<{ message: string }>(resp);
}

export async function createBroadcast(
  req: CreateBroadcastRequest,
): Promise<CreateBroadcastResponse> {
  const resp = await fetchWithAuth(API_BASE, '/admin/broadcast-email', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(req),
  });
  return jsonOrThrow<CreateBroadcastResponse>(resp);
}

export async function listBroadcasts(limit = 50, offset = 0): Promise<ListBroadcastsResponse> {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  const resp = await fetchWithAuth(API_BASE, `/admin/broadcast-email?${params}`);
  return jsonOrThrow<ListBroadcastsResponse>(resp);
}

export async function getBroadcastDetail(
  id: string,
  limit = 100,
  offset = 0,
): Promise<BroadcastDetailResponse> {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  const resp = await fetchWithAuth(
    API_BASE,
    `/admin/broadcast-email/${encodeURIComponent(id)}?${params}`,
  );
  return jsonOrThrow<BroadcastDetailResponse>(resp);
}

export async function cancelBroadcast(id: string): Promise<void> {
  const resp = await fetchWithAuth(API_BASE, `/admin/broadcast-email/${encodeURIComponent(id)}`, {
    method: 'DELETE',
  });
  await jsonOrThrow<{ message: string }>(resp);
}

// ========================================
// Request Export
// ========================================

export interface ExportRequestsParams {
  startTime: string;
  endTime?: string;
  userId?: string;
  modelId?: string;
  errorsOnly?: boolean;
  includeContent?: boolean;
}

export async function exportRequests(params: ExportRequestsParams): Promise<void> {
  const qs = new URLSearchParams({
    start_time: params.startTime,
  });
  if (params.endTime) qs.set('end_time', params.endTime);
  if (params.userId) qs.set('user_id', params.userId);
  if (params.modelId) qs.set('model_id', params.modelId);
  if (params.errorsOnly) qs.set('errors_only', 'true');
  if (params.includeContent) qs.set('include_content', 'true');

  const resp = await fetchWithAuth(API_BASE, `/admin/export/requests?${qs.toString()}`);
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    const message = (err as { detail?: string }).detail ?? `Export failed (HTTP ${resp.status})`;
    throw new Error(message);
  }

  const blob = await resp.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  const startDate = params.startTime.slice(0, 10).replace(/-/g, '');
  const endDate = (params.endTime ?? new Date().toISOString()).slice(0, 10).replace(/-/g, '');
  a.href = url;
  a.download = `requests-${startDate}-${endDate}.jsonl`;
  try {
    document.body.appendChild(a);
    a.click();
  } finally {
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }
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
  key_index: number | null;
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

// ========================================
// Provider Hourly Performance Stats
// ========================================

export interface ProviderStatsRow {
  hour_bucket: string;
  provider: string;
  model_id: string;
  request_count: number;
  error_count: number;
  stream_count: number;
  ttft_p50_ms: number | null;
  ttft_p95_ms: number | null;
  ttft_p99_ms: number | null;
  latency_p50_ms: number | null;
  latency_p95_ms: number | null;
  latency_p99_ms: number | null;
  throughput_avg_tps: number | null;
  throughput_p50_tps: number | null;
  throughput_p95_tps: number | null;
  prompt_tokens_avg: number | null;
  completion_tokens_avg: number | null;
  total_completion_tokens: number;
  total_prompt_tokens: number | null;
  total_reasoning_tokens: number | null;
}

export interface ProviderModelPair {
  provider: string;
  model_id: string;
}

export interface ProviderStatsResponse {
  rows: ProviderStatsRow[];
  providers: string[];
  models: string[];
  pairs: ProviderModelPair[];
  // Providers that have rows inside the selected window. The dropdown lists
  // above span the full retained table regardless of the selected range.
  window_providers: string[];
}

export async function getProviderStats(params: {
  provider: string;
  model_id: string;
  from?: string;
  to?: string;
}): Promise<ProviderStatsResponse> {
  const search = new URLSearchParams({
    provider: params.provider,
    model_id: params.model_id,
    ...(params.from ? { from: params.from } : {}),
    ...(params.to ? { to: params.to } : {}),
  });
  const resp = await fetchWithAuth(API_BASE, `/admin/api/provider-stats?${search.toString()}`);
  return jsonOrThrow<ProviderStatsResponse>(resp);
}

// ========================================
// Provider Token Usage
// ========================================

export type TokenUsageRange = '1h' | '24h' | '7d' | '30d';

export interface ProviderTokenUsageRow {
  provider: string;
  model_id: string;
  input_tokens: number;
  output_tokens: number;
  cached_tokens: number;
  reasoning_tokens: number;
  cost_usd: number;
  request_count: number;
}

export interface ProviderTokenUsageTotals {
  input_tokens: number;
  output_tokens: number;
  cached_tokens: number;
  reasoning_tokens: number;
  cost_usd: number;
  request_count: number;
}

export interface ProviderTokenUsageResponse {
  range: TokenUsageRange;
  window: { from: string; to: string };
  refreshed_at: string;
  rows: ProviderTokenUsageRow[];
  totals: ProviderTokenUsageTotals;
}

export async function getProviderTokenUsage(
  range: TokenUsageRange,
): Promise<ProviderTokenUsageResponse> {
  const search = new URLSearchParams({ range });
  const resp = await fetchWithAuth(
    API_BASE,
    `/admin/api/provider-token-usage?${search.toString()}`,
  );
  return jsonOrThrow<ProviderTokenUsageResponse>(resp);
}

// ========================================
// Signup Domain Allowlist
// ========================================

export interface SignupAllowedDomain {
  domain: string;
  is_wildcard: boolean;
  created_at: string | null;
  created_by: string | null;
  created_by_email: string | null;
}

export interface ListSignupAllowedDomainsResponse {
  domains: SignupAllowedDomain[];
}

export async function listSignupAllowedDomains(): Promise<ListSignupAllowedDomainsResponse> {
  const resp = await fetchWithAuth(API_BASE, '/admin/signup-domains');
  return jsonOrThrow<ListSignupAllowedDomainsResponse>(resp);
}

export async function addSignupAllowedDomain(domain: string): Promise<SignupAllowedDomain> {
  const resp = await fetchWithAuth(API_BASE, '/admin/signup-domains', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ domain }),
  });
  return jsonOrThrow<SignupAllowedDomain>(resp);
}

export async function removeSignupAllowedDomain(
  domain: string,
  isWildcard: boolean,
): Promise<void> {
  const params = new URLSearchParams({ wildcard: String(isWildcard) });
  const resp = await fetchWithAuth(
    API_BASE,
    `/admin/signup-domains/${encodeURIComponent(domain)}?${params.toString()}`,
    { method: 'DELETE' },
  );
  if (resp.ok) return; // 204 has no body
  // Delegate to the standard error path; jsonOrThrow throws on !ok.
  await jsonOrThrow<unknown>(resp);
}

// ========================================
// Runtime Settings (Feature Flags)
// ========================================

export interface RuntimeSettingItem {
  key: string;
  value: unknown;
  value_type: string;
  default_value: unknown;
  description: string;
  min?: number | null;
  max?: number | null;
}

export interface ListSettingsResponse {
  settings: RuntimeSettingItem[];
}

export async function listRuntimeSettings(): Promise<ListSettingsResponse> {
  const resp = await fetchWithAuth(API_BASE, '/admin/settings');
  return jsonOrThrow<ListSettingsResponse>(resp);
}

export async function updateRuntimeSetting(
  key: string,
  value: unknown,
): Promise<RuntimeSettingItem> {
  const resp = await fetchWithAuth(API_BASE, `/admin/settings/${encodeURIComponent(key)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ value }),
  });
  return jsonOrThrow<RuntimeSettingItem>(resp);
}

// ========================================
// Per-Role Daily Quota
// ========================================

export type Role = 'free' | 'pro' | 'internal' | 'admin';

export interface AdminModelVisibilityItem {
  model_id: string;
  baseline_required_role: Role;
  override_required_role: Role | null;
  effective_required_role: Role;
}

export interface ListAdminModelVisibilityResponse {
  models: AdminModelVisibilityItem[];
}

export interface RoleQuotaPreview {
  role: Role;
  quota: number;
  keys_affected: number;
  users_affected: number;
}

export interface RoleQuotaApplyResult {
  role: Role;
  quota: number;
  keys_updated: number;
}

export async function previewRoleQuotaApply(role: Role): Promise<RoleQuotaPreview> {
  const resp = await fetchWithAuth(
    API_BASE,
    `/admin/quota/role-apply-preview?role=${encodeURIComponent(role)}`,
  );
  return jsonOrThrow<RoleQuotaPreview>(resp);
}

export async function applyRoleQuota(role: Role): Promise<RoleQuotaApplyResult> {
  const resp = await fetchWithAuth(API_BASE, '/admin/quota/role-apply', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ role }),
  });
  return jsonOrThrow<RoleQuotaApplyResult>(resp);
}

export async function listModelVisibility(): Promise<ListAdminModelVisibilityResponse> {
  const resp = await fetchWithAuth(API_BASE, '/admin/models/visibility');
  return jsonOrThrow<ListAdminModelVisibilityResponse>(resp);
}

export async function updateModelVisibility(
  modelId: string,
  requiredRole: Role | null,
): Promise<AdminModelVisibilityItem> {
  const resp = await fetchWithAuth(
    API_BASE,
    `/admin/models/${encodeURIComponent(modelId)}/visibility`,
    {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ required_role: requiredRole }),
    },
  );
  return jsonOrThrow<AdminModelVisibilityItem>(resp);
}

export interface AdminModelConcurrencyItem {
  model_id: string;
  exempt: boolean;
}

export interface ListAdminModelConcurrencyResponse {
  models: AdminModelConcurrencyItem[];
}

export async function listModelConcurrency(): Promise<ListAdminModelConcurrencyResponse> {
  const resp = await fetchWithAuth(API_BASE, '/admin/models/concurrency');
  return jsonOrThrow<ListAdminModelConcurrencyResponse>(resp);
}

export async function updateModelConcurrency(
  modelId: string,
  exempt: boolean,
): Promise<AdminModelConcurrencyItem> {
  const resp = await fetchWithAuth(
    API_BASE,
    `/admin/models/${encodeURIComponent(modelId)}/concurrency`,
    {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ exempt }),
    },
  );
  return jsonOrThrow<AdminModelConcurrencyItem>(resp);
}

export interface RouteWeight {
  model_id: string;
  strategy: string;
  endpoint_id: string;
  provider: string;
  base_url: string | null;
  yaml_weight: number;
  override_weight: number | null;
  effective_weight: number;
}

export interface ListRouteWeightsResponse {
  model_id?: string;
  routes: RouteWeight[];
}

export type RoutewiseSettingValue = string | number | boolean | null;

export interface RoutewiseSettingItem {
  key: string;
  value: RoutewiseSettingValue;
  value_type: string;
  default_value: RoutewiseSettingValue;
  description: string;
  min?: number | null;
  max?: number | null;
}

export interface ListRoutewiseSettingsResponse {
  settings: RoutewiseSettingItem[];
}

export async function listRouteWeights(modelId?: string): Promise<RouteWeight[]> {
  const path = modelId
    ? `/admin/routing/weights/${encodeURIComponent(modelId)}`
    : '/admin/routing/weights';
  const resp = await fetchWithAuth(API_BASE, path);
  const data = await jsonOrThrow<ListRouteWeightsResponse>(resp);
  return data.routes;
}

export async function setRouteWeight(
  modelId: string,
  endpointId: string,
  weight: number,
): Promise<RouteWeight> {
  const resp = await fetchWithAuth(
    API_BASE,
    `/admin/routing/weights/${encodeURIComponent(modelId)}/${encodeURIComponent(endpointId)}`,
    {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ weight }),
    },
  );
  return jsonOrThrow<RouteWeight>(resp);
}

export async function clearRouteWeight(modelId: string, endpointId: string): Promise<RouteWeight> {
  const resp = await fetchWithAuth(
    API_BASE,
    `/admin/routing/weights/${encodeURIComponent(modelId)}/${encodeURIComponent(endpointId)}`,
    { method: 'DELETE' },
  );
  return jsonOrThrow<RouteWeight>(resp);
}

export async function listRoutewiseSettings(): Promise<ListRoutewiseSettingsResponse> {
  const resp = await fetchWithAuth(API_BASE, '/admin/routewise/settings');
  return jsonOrThrow<ListRoutewiseSettingsResponse>(resp);
}

export async function updateRoutewiseSetting(
  key: string,
  value: RoutewiseSettingValue,
): Promise<RoutewiseSettingItem> {
  const resp = await fetchWithAuth(
    API_BASE,
    `/admin/routewise/settings/${encodeURIComponent(key)}`,
    {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ value }),
    },
  );
  return jsonOrThrow<RoutewiseSettingItem>(resp);
}

// ========================================
// Provider API Keys (admin-managed runtime credentials)
// ========================================

export type ProviderKeySource = 'env' | 'db';

export interface ProviderApiKeyItem {
  id: string | null;
  provider: string;
  key_prefix: string;
  label: string | null;
  source: ProviderKeySource;
  status: string;
  created_at: string | null;
}

export interface ListProviderApiKeysResponse {
  provider: string | null;
  keys: ProviderApiKeyItem[];
}

export interface AddProviderApiKeyResponse {
  key: ProviderApiKeyItem;
  pools_updated: number;
}

export interface DeleteProviderApiKeyResponse {
  id: string;
  provider: string;
  pools_updated: number;
}

export interface DisableProviderEnvKeyResponse {
  id: string;
  provider: string;
  pools_updated: number;
}

export async function listProviderKeys(provider?: string): Promise<ListProviderApiKeysResponse> {
  const params = new URLSearchParams();
  if (provider) params.set('provider', provider);
  const qs = params.toString();
  const path = qs ? `/admin/provider-keys?${qs}` : '/admin/provider-keys';
  const resp = await fetchWithAuth(API_BASE, path);
  return jsonOrThrow<ListProviderApiKeysResponse>(resp);
}

export async function addProviderKey(
  provider: string,
  apiKey: string,
  label?: string,
): Promise<AddProviderApiKeyResponse> {
  const resp = await fetchWithAuth(API_BASE, '/admin/provider-keys', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      provider,
      api_key: apiKey,
      ...(label ? { label } : {}),
    }),
  });
  return jsonOrThrow<AddProviderApiKeyResponse>(resp);
}

export async function deleteProviderKey(id: string): Promise<DeleteProviderApiKeyResponse> {
  const resp = await fetchWithAuth(API_BASE, `/admin/provider-keys/${encodeURIComponent(id)}`, {
    method: 'DELETE',
  });
  return jsonOrThrow<DeleteProviderApiKeyResponse>(resp);
}

export async function disableProviderEnvKey(
  provider: string,
  envKeyId: string,
): Promise<DisableProviderEnvKeyResponse> {
  const resp = await fetchWithAuth(API_BASE, '/admin/provider-keys/disable-env', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ provider, env_key_id: envKeyId }),
  });
  return jsonOrThrow<DisableProviderEnvKeyResponse>(resp);
}
