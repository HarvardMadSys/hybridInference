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
  signup_reason: string | null;
  admin_note: string | null;
  created_at: string;
  last_login_at: string | null;
  has_key: boolean;
  key_prefix: string | null;
  key_status: string | null;
  usage_today_usd: number;
  usage_month_usd: number;
  usage_alltime_usd: number;
  usage_alltime_requests: number;
  usage_alltime_tokens: number;
}

export type UserSortBy =
  | 'created'
  | 'cost_today'
  | 'cost_month'
  | 'cost_alltime'
  | 'last_login'
  | 'requests'
  | 'tokens';

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
  admin_note: string | null;
  suspension_message: string | null;
  avg_turns: number | null;
  avg_user_turns: number | null;
  ask_question_fraction: number | null;
}

export async function getUserDetail(
  userId: string,
  opts: { includeActivityStats?: boolean } = {},
): Promise<UserDetail> {
  // The all-time activity stats (avg_turns, avg_user_turns,
  // ask_question_fraction) require full-history log scans, so the detail
  // endpoint skips them unless explicitly requested. Ask for them only when the
  // admin opts in, keeping the default row-expand fast.
  const qs = opts.includeActivityStats ? '?include_activity_stats=true' : '';
  const resp = await fetchWithAuth(
    API_BASE,
    `/admin/users/${encodeURIComponent(userId)}/detail${qs}`,
  );
  return jsonOrThrow<UserDetail>(resp);
}

export interface UserTurnAverages {
  avg_turns: number | null;
  avg_user_turns: number | null;
}

export interface BulkTurnAveragesResponse {
  averages: Record<string, UserTurnAverages>;
}

export async function getBulkTurnAverages(userIds: string[]): Promise<BulkTurnAveragesResponse> {
  if (userIds.length === 0) return { averages: {} };
  const params = new URLSearchParams({ user_ids: userIds.join(',') });
  const resp = await fetchWithAuth(API_BASE, `/admin/users/turn-averages?${params.toString()}`);
  return jsonOrThrow<BulkTurnAveragesResponse>(resp);
}

export interface UserAskQuestionFraction {
  ask_question_fraction: number | null;
  n_requests: number;
  n_ask_requests: number;
}

export interface BulkAskQuestionFractionsResponse {
  fractions: Record<string, UserAskQuestionFraction>;
}

export async function getBulkAskQuestionFractions(
  userIds: string[],
): Promise<BulkAskQuestionFractionsResponse> {
  if (userIds.length === 0) return { fractions: {} };
  const params = new URLSearchParams({ user_ids: userIds.join(',') });
  const resp = await fetchWithAuth(
    API_BASE,
    `/admin/users/ask-question-fractions?${params.toString()}`,
  );
  return jsonOrThrow<BulkAskQuestionFractionsResponse>(resp);
}

// One signal's contribution to a user's automation score. `sub` is the signal's
// automation sub-score in [0,1] (null when the signal was dropped for lack of data).
export interface AutomationSignal {
  sub: number | null;
  weight: number;
  available: boolean;
}

// Per-user human-vs-script automation score. `score` in [0,1]: HIGH means the
// traffic looks script/batch/cron-driven, LOW means an interactive human.
export interface UserAutomationScore {
  user_id: string;
  days: number;
  score: number;
  confidence: number;
  band: string;
  insufficient_data: boolean;
  n_req: number;
  agent_share: number;
  signals: Record<string, AutomationSignal>;
  detail: Record<string, number | null>;
}

export interface BulkAutomationScoresResponse {
  days: number;
  scores: Record<string, UserAutomationScore>;
}

export async function getUserAutomationScore(
  userId: string,
  days = 30,
): Promise<UserAutomationScore> {
  const params = new URLSearchParams({ days: String(days) });
  const resp = await fetchWithAuth(
    API_BASE,
    `/admin/users/${encodeURIComponent(userId)}/automation-score?${params.toString()}`,
  );
  return jsonOrThrow<UserAutomationScore>(resp);
}

export async function getBulkAutomationScores(
  userIds: string[],
  days = 30,
): Promise<BulkAutomationScoresResponse> {
  if (userIds.length === 0) return { days, scores: {} };
  const params = new URLSearchParams({ user_ids: userIds.join(','), days: String(days) });
  const resp = await fetchWithAuth(API_BASE, `/admin/users/automation-scores?${params.toString()}`);
  return jsonOrThrow<BulkAutomationScoresResponse>(resp);
}

export interface UpdateUserData {
  role?: string;
  status?: string;
  quota_daily_cost_usd?: number;
  quota_monthly_cost_usd?: number;
  disabled_models?: string[];
  max_concurrent_requests?: number | null;
  admin_note?: string | null;
  suspension_message?: string | null;
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

export async function getPerformanceMetrics({
  refresh = false,
}: { refresh?: boolean } = {}): Promise<AdminPerformanceMetricsResponse> {
  const resp = await fetchWithAuth(
    API_BASE,
    refresh ? '/admin/performance-metrics?refresh=true' : '/admin/performance-metrics',
  );
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
  // Inbound HTTP Referer header (metadata->>'referer'); which site/app origin
  // drove the request. Null when the client sent no Referer.
  referer?: string | null;
  // Calling agent's self-declared name, parsed backend-side from the system
  // prompt's "You are <Name>" opener. Preferred over user_agent for the client
  // column when present.
  agent?: string | null;
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

// Per-request RouteWise decision blob. The backend passes the raw persisted
// `metadata->'routewise'` dict straight through (see
// apps/backend/serving/servers/routers/admin/metrics.py and
// apps/backend/routing/routewise/router.py `_decision_metadata`), so the real
// field names differ from the older narrow shape below. Every field is optional
// because older rows and error paths may omit any of them. The first group is
// kept for backward compatibility with existing consumers (e.g.
// lib/utils/routewise.ts); the second group mirrors the real persisted keys used
// by the RouteWise decisions panel.
export interface AdminRouteWiseDecision {
  // --- legacy / compatibility fields (do not remove) ---
  selected_provider_type?: string | null;
  selected_provider?: string | null;
  selected_endpoint_id?: string | null;
  hedging_triggered?: boolean | null;
  hedge_backup_provider?: string | null;
  hedge_backup_endpoint_id?: string | null;
  backup_won?: boolean | null;
  // --- raw persisted metadata fields ---
  candidate_costs_usd?: Record<string, number>;
  candidate_mean_ttft_sec?: Record<string, number>;
  candidate_provider_types?: Record<string, string>;
  candidate_mean_ttft_sources?: Record<string, string>;
  candidate_quota_remaining?: Record<string, number>;
  lp_weights?: Record<string, number>;
  lp_status?: string | null;
  budget_usd?: number | null;
  selected_endpoint?: string | null;
  final_endpoint?: string | null;
  final_provider_type?: string | null;
  hedged?: boolean | null;
  hedge_winner?: string | null;
  backup_provider?: string | null;
  fallback_attempts?: number | null;
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
  requestType?: 'chat' | 'embedding',
  days?: number,
): Promise<AdminRecentRequestsResponse> {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (userId) params.set('user_id', userId);
  if (modelId) params.set('model_id', modelId);
  if (errorsOnly) params.set('errors_only', 'true');
  if (requestType) params.set('request_type', requestType);
  if (days != null) params.set('days', String(days));
  const resp = await fetchWithAuth(API_BASE, `/admin/recent-requests?${params.toString()}`);
  return jsonOrThrow<AdminRecentRequestsResponse>(resp);
}

// ----------------------------------------------------------------------------
// Per-route performance — TTFT / decode throughput per served (model, endpoint)
// ----------------------------------------------------------------------------

export interface AdminRequestPerfDistribution {
  // Requests the metric was defined on. Lower than the group's request_count
  // when TTFT wasn't recorded, or the decode window was too short to measure.
  count: number;
  mean: number | null;
  p10: number | null;
  p50: number | null;
  p90: number | null;
}

export interface AdminRequestPerfGroup {
  // The *served* model/endpoint (falling back to the requested model and the
  // provider label on rows logged before those columns existed).
  model_id: string;
  endpoint_id: string;
  request_count: number;
  ttft_ms: AdminRequestPerfDistribution;
  decode_throughput_tps: AdminRequestPerfDistribution;
}

export interface AdminRequestPerfBreakdownResponse {
  generated_at: string;
  days: number;
  groups: AdminRequestPerfGroup[];
  truncated: boolean;
}

export async function getRecentRequestsPerformance({
  days,
  userId,
  modelId,
  requestType,
  refresh = false,
}: {
  days?: number;
  userId?: string;
  modelId?: string;
  requestType?: 'chat' | 'embedding';
  // Bypass the backend's short-lived per-filter cache. Set when the admin
  // explicitly asks for fresh numbers, not on filter changes.
  refresh?: boolean;
} = {}): Promise<AdminRequestPerfBreakdownResponse> {
  const params = new URLSearchParams();
  if (days != null) params.set('days', String(days));
  if (userId) params.set('user_id', userId);
  if (modelId) params.set('model_id', modelId);
  if (requestType) params.set('request_type', requestType);
  if (refresh) params.set('refresh', 'true');
  const query = params.toString();
  const resp = await fetchWithAuth(
    API_BASE,
    query ? `/admin/recent-requests/performance?${query}` : '/admin/recent-requests/performance',
  );
  return jsonOrThrow<AdminRequestPerfBreakdownResponse>(resp);
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

export interface AnalyticsModelUserEntry {
  email: string;
  user_id: string;
  requests: number;
  tokens: number; // prompt + completion tokens for this user + model
}

export interface AnalyticsModelUsers {
  model: string; // model_id
  requests: number; // total signed-in-user requests for this model
  tokens: number; // total signed-in-user tokens for this model
  users: AnalyticsModelUserEntry[];
}

export interface AdminAnalyticsResponse {
  period: AnalyticsPeriod;
  active_users: number;
  // Mean conversation depth per chat request; null when the period has no
  // chat-style requests.
  avg_turns: number | null;
  avg_user_turns: number | null;
  sparkline: SparklineBucket[];
  top_users: AnalyticsUserEntry[];
  by_model: AnalyticsBreakdownEntry[];
  by_provider: AnalyticsBreakdownEntry[];
  by_model_top_users: AnalyticsModelUsers[];
  generated_at: string;
}

export async function getAnalytics(period: AnalyticsPeriod): Promise<AdminAnalyticsResponse> {
  const resp = await fetchWithAuth(API_BASE, `/admin/analytics?period=${period}`);
  return jsonOrThrow<AdminAnalyticsResponse>(resp);
}

// Growth — daily DAU / token series and its fitted slope. Ranged in days rather
// than by AnalyticsPeriod: a daily slope needs weeks of buckets to mean anything.
export type GrowthRange = 30 | 60 | 90;

export interface GrowthPoint {
  day: string; // UTC midnight opening the day
  active_users: number; // distinct signed-in users that day
  new_users: number; // first seen within the requested range on this day
  tokens: number; // prompt + completion, all traffic
  requests: number;
  partial: boolean; // today, still accumulating — excluded from the trends
}

export interface GetGrowthAnalyticsOptions {
  signal?: AbortSignal;
}

export interface GrowthTrend {
  slope_per_day: number;
  recent_avg: number;
  previous_avg: number;
  change_pct: number | null; // null when the older half is flat zero
  compare_days: number;
}

export interface AdminGrowthResponse {
  days: number;
  points: GrowthPoint[];
  users_trend: GrowthTrend;
  tokens_trend: GrowthTrend;
  generated_at: string;
}

export async function getGrowthAnalytics(
  days: GrowthRange,
  options: GetGrowthAnalyticsOptions = {},
): Promise<AdminGrowthResponse> {
  const resp = await fetchWithAuth(API_BASE, `/admin/analytics/growth?days=${days}`, {
    signal: options.signal,
  });
  return jsonOrThrow<AdminGrowthResponse>(resp);
}

export type GeoBucketColumn = 'c' | 'cont' | 'n' | 'tout';

export type GeoMetric = 'n' | 'tout';

export type GeoBucketRow = [
  countryAlpha3: string,
  continent: string,
  requests: number,
  outputTokens: number,
];

export interface GeoIpAttribution {
  label: string;
  url: string;
}

export interface GeoAnalyticsMeta {
  source: string;
  generated_at: string;
  start: string | null;
  hours: number;
  rows_total: number;
  rows_with_ip: number;
  geoip: {
    country: boolean;
    provider: string | null;
    attribution: GeoIpAttribution | null;
  };
  degraded: boolean;
  degraded_reasons: string[];
  unmapped_alpha2: string[];
  notes: string[];
}

export interface GeoHour {
  b: GeoBucketRow[];
}

export interface GeoAnalyticsResponse {
  meta: GeoAnalyticsMeta;
  bucket_cols: ['c', 'cont', 'n', 'tout'];
  hours_index: string[];
  hours: GeoHour[];
}

export interface GetGeoAnalyticsOptions {
  days?: 7 | 14 | 30 | 90;
  signal?: AbortSignal;
}

const GEO_BUCKET_COLUMNS: GeoAnalyticsResponse['bucket_cols'] = ['c', 'cont', 'n', 'tout'];

function isGeoRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

function isGeoStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === 'string');
}

function isGeoBucketRow(value: unknown): value is GeoBucketRow {
  return (
    Array.isArray(value) &&
    value.length === GEO_BUCKET_COLUMNS.length &&
    typeof value[0] === 'string' &&
    typeof value[1] === 'string' &&
    typeof value[2] === 'number' &&
    Number.isFinite(value[2]) &&
    value[2] >= 0 &&
    typeof value[3] === 'number' &&
    Number.isFinite(value[3]) &&
    value[3] >= 0
  );
}

function validateGeoAnalyticsResponse(data: unknown): asserts data is GeoAnalyticsResponse {
  if (
    !isGeoRecord(data) ||
    !isGeoRecord(data.meta) ||
    !isGeoRecord(data.meta.geoip) ||
    !isGeoStringArray(data.hours_index) ||
    !Array.isArray(data.hours) ||
    typeof data.meta.source !== 'string' ||
    typeof data.meta.generated_at !== 'string' ||
    !(data.meta.start === null || typeof data.meta.start === 'string') ||
    typeof data.meta.hours !== 'number' ||
    !Number.isFinite(data.meta.hours) ||
    data.meta.hours < 0 ||
    typeof data.meta.rows_total !== 'number' ||
    !Number.isFinite(data.meta.rows_total) ||
    data.meta.rows_total < 0 ||
    typeof data.meta.rows_with_ip !== 'number' ||
    !Number.isFinite(data.meta.rows_with_ip) ||
    data.meta.rows_with_ip < 0 ||
    typeof data.meta.degraded !== 'boolean' ||
    !isGeoStringArray(data.meta.degraded_reasons) ||
    !isGeoStringArray(data.meta.unmapped_alpha2) ||
    !isGeoStringArray(data.meta.notes) ||
    typeof data.meta.geoip.country !== 'boolean' ||
    !(data.meta.geoip.provider === null || typeof data.meta.geoip.provider === 'string') ||
    !(
      data.meta.geoip.attribution === null ||
      (isGeoRecord(data.meta.geoip.attribution) &&
        typeof data.meta.geoip.attribution.label === 'string' &&
        typeof data.meta.geoip.attribution.url === 'string')
    ) ||
    !data.hours.every(
      (hour) =>
        isGeoRecord(hour) && Array.isArray(hour.b) && hour.b.every((row) => isGeoBucketRow(row)),
    )
  ) {
    throw new Error('The geographic demand response has an invalid structure');
  }
  const bucketColumns = data.bucket_cols;
  if (
    !Array.isArray(bucketColumns) ||
    bucketColumns.length !== GEO_BUCKET_COLUMNS.length ||
    !GEO_BUCKET_COLUMNS.every((column, index) => bucketColumns[index] === column)
  ) {
    throw new Error('The geographic demand response has an invalid bucket column contract');
  }
  if (data.hours_index.length !== data.hours.length || data.meta.hours !== data.hours.length) {
    throw new Error('The geographic demand response has mismatched hourly data');
  }
}

export async function getGeoAnalytics(
  options: GetGeoAnalyticsOptions = {},
): Promise<GeoAnalyticsResponse> {
  const params = new URLSearchParams({ days: String(options.days ?? 14) });
  const resp = await fetchWithAuth(API_BASE, `/admin/analytics/geo?${params.toString()}`, {
    signal: options.signal,
  });
  const data = await jsonOrThrow<unknown>(resp);
  validateGeoAnalyticsResponse(data);
  return data;
}

// ========================================
// Usage Insights (LLM-powered request analysis)
// ========================================

// The analysis provider (API key + model) is configured server-side in Admin →
// Settings; the request only chooses the scope and sample size.
export interface UsageInsightsRequest {
  user_id?: string;
  user_email?: string;
  limit?: number;
  max_chars?: number;
}

export interface UsageInsightsResponse {
  analysis: string; // Markdown narrative
  model: string;
  sampled_requests: number;
  scope: string;
  generated_at: string;
}

export async function analyzeUsageInsights(
  req: UsageInsightsRequest = {},
): Promise<UsageInsightsResponse> {
  const resp = await fetchWithAuth(API_BASE, '/admin/usage-insights/analyze', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(req),
  });
  return jsonOrThrow<UsageInsightsResponse>(resp);
}

// Stored analysis-provider config. The raw key is never returned — only whether
// one is set and a masked tail hint.
export interface UsageInsightsSettings {
  configured: boolean;
  api_key_hint: string | null;
  model: string;
}

export interface UsageInsightsSettingsUpdate {
  // Omit api_key to keep the current one; '' clears it; any other value replaces it.
  api_key?: string;
  model?: string;
}

export async function getUsageInsightsSettings(): Promise<UsageInsightsSettings> {
  const resp = await fetchWithAuth(API_BASE, '/admin/usage-insights/settings');
  return jsonOrThrow<UsageInsightsSettings>(resp);
}

export async function updateUsageInsightsSettings(
  patch: UsageInsightsSettingsUpdate,
): Promise<UsageInsightsSettings> {
  const resp = await fetchWithAuth(API_BASE, '/admin/usage-insights/settings', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  });
  return jsonOrThrow<UsageInsightsSettings>(resp);
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
  // When set, restrict recipients to users who have spent more than this many
  // USD today (UTC). Omit/null for no spend filter.
  min_spend_today_usd?: number | null;
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
  requestType?: 'chat' | 'embedding';
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
  if (params.requestType) qs.set('request_type', params.requestType);
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
  disabled: boolean;
  /** Handle for toggling this single key; null for cookie-based credentials. */
  key_ref: string | null;
  key_disabled: boolean;
}

export interface AdminProviderQuotasResponse {
  generated_at: string;
  providers: ProviderQuotaResult[];
}

export async function getProviderQuotas(): Promise<AdminProviderQuotasResponse> {
  const resp = await fetchWithAuth(API_BASE, '/admin/provider-quotas');
  return jsonOrThrow<AdminProviderQuotasResponse>(resp);
}

export interface ProviderKeyByRefResponse {
  provider: string;
  key_ref: string;
  source: 'db' | 'env';
  status: 'active' | 'disabled';
  pools_updated: number;
}

export async function setProviderQuotaKeyDisabled(
  provider: string,
  keyRef: string,
  disabled: boolean,
): Promise<ProviderKeyByRefResponse> {
  const action = disabled ? 'disable' : 'enable';
  const resp = await fetchWithAuth(API_BASE, `/admin/provider-keys/by-ref/${action}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ provider, key_ref: keyRef }),
  });
  return jsonOrThrow<ProviderKeyByRefResponse>(resp);
}

// ========================================
// Provider Availability (disable / enable)
// ========================================

export interface RoutableProvider {
  provider: string;
  display_name: string;
  model_count: number;
  endpoint_count: number;
  disabled: boolean;
}

export interface ListRoutableProvidersResponse {
  providers: RoutableProvider[];
}

export interface SetProviderDisabledResponse {
  provider: string;
  disabled: boolean;
  affected_model_count: number;
}

export async function getRoutableProviders(): Promise<ListRoutableProvidersResponse> {
  const resp = await fetchWithAuth(API_BASE, '/admin/providers/routable');
  return jsonOrThrow<ListRoutableProvidersResponse>(resp);
}

export async function setProviderDisabled(
  provider: string,
  disabled: boolean,
): Promise<SetProviderDisabledResponse> {
  const resp = await fetchWithAuth(
    API_BASE,
    `/admin/providers/${encodeURIComponent(provider)}/disabled`,
    {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ disabled }),
    },
  );
  return jsonOrThrow<SetProviderDisabledResponse>(resp);
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
  // Provider label -> human-readable name. Sparse: labels retained only in
  // history are absent, and the raw label is shown for those.
  provider_display_names?: Record<string, string>;
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

export interface ProviderObservabilityTotals {
  request_count: number;
  error_count: number;
  rate_limited_count: number;
  timeout_count: number;
  server_error_count: number;
  cache_eligible_count: number;
  cache_hit_count: number;
  input_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
}

export interface ProviderObservabilityBucket {
  start_time: string;
  request_count: number;
  error_count: number;
  cache_eligible_count: number;
  cache_hit_count: number;
  cache_read_tokens: number;
  input_tokens: number;
}

export interface ProviderErrorTypeRow {
  error_type: string;
  count: number;
  fraction: number;
}

export interface ProviderObservabilityResponse {
  provider: string;
  provider_display_name?: string | null;
  window: { from: string; to: string };
  bucket_minutes: number;
  totals: ProviderObservabilityTotals;
  buckets: ProviderObservabilityBucket[];
  error_types: ProviderErrorTypeRow[];
}

export async function getProviderObservability(params: {
  provider: string;
  model_id?: string;
  from?: string;
  to?: string;
}): Promise<ProviderObservabilityResponse> {
  const search = new URLSearchParams({
    provider: params.provider,
    ...(params.model_id ? { model_id: params.model_id } : {}),
    ...(params.from ? { from: params.from } : {}),
    ...(params.to ? { to: params.to } : {}),
  });
  const resp = await fetchWithAuth(
    API_BASE,
    `/admin/api/provider-observability?${search.toString()}`,
  );
  return jsonOrThrow<ProviderObservabilityResponse>(resp);
}

// ========================================
// Provider Token Usage
// ========================================

export type TokenUsageRange = '1h' | '24h' | '7d' | '30d';

export interface ProviderTokenUsageRow {
  provider: string;
  provider_display_name?: string | null;
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
// Slack Alert Snooze
// ========================================

export interface AlertSnoozeStatus {
  snoozed: boolean;
  snooze_until: number | null;
  seconds_remaining: number;
}

export async function getAlertSnooze(): Promise<AlertSnoozeStatus> {
  const resp = await fetchWithAuth(API_BASE, '/admin/alerts/snooze');
  return jsonOrThrow<AlertSnoozeStatus>(resp);
}

export async function snoozeAlerts(durationSeconds: number): Promise<AlertSnoozeStatus> {
  const resp = await fetchWithAuth(API_BASE, '/admin/alerts/snooze', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ duration_seconds: durationSeconds }),
  });
  return jsonOrThrow<AlertSnoozeStatus>(resp);
}

export async function clearAlertSnooze(): Promise<AlertSnoozeStatus> {
  const resp = await fetchWithAuth(API_BASE, '/admin/alerts/snooze', { method: 'DELETE' });
  return jsonOrThrow<AlertSnoozeStatus>(resp);
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
export type RoutewiseSettingSource = 'runtime_override' | 'model_config' | 'global_default';

export interface RoutewiseSettingItem {
  key: string;
  value: RoutewiseSettingValue;
  value_type: string;
  default_value: RoutewiseSettingValue;
  source: RoutewiseSettingSource;
  overridden: boolean;
  description: string;
  min?: number | null;
  max?: number | null;
}

export interface ListRoutewiseSettingsResponse {
  model_id: string;
  settings: RoutewiseSettingItem[];
}

export interface RoutewiseProbeSampleItem {
  model_id: string;
  endpoint_id: string;
  ttft_ms: number | null;
  ok: boolean;
  error: string | null;
  checked_at: string;
}

export interface ListRoutewiseProbeSamplesResponse {
  samples: RoutewiseProbeSampleItem[];
}

export interface ListRoutewiseProbeSamplesOptions {
  modelId?: string;
  endpointId?: string;
  sinceSeconds?: number;
  limit?: number;
}

export interface RunRoutewiseProbeRequest {
  model_id?: string | null;
  endpoint_id?: string | null;
  idle_only?: boolean;
}

export interface RoutewiseProbeRunResult {
  model_id: string;
  endpoint_id: string;
  ok: boolean;
  ttft_ms: number | null;
  error: string | null;
}

export interface RunRoutewiseProbeResponse {
  results: RoutewiseProbeRunResult[];
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

export async function listRoutewiseSettings(
  modelId: string,
): Promise<ListRoutewiseSettingsResponse> {
  const params = new URLSearchParams({ model_id: modelId });
  const resp = await fetchWithAuth(
    API_BASE,
    `/admin/routewise/model-settings?${params.toString()}`,
  );
  return jsonOrThrow<ListRoutewiseSettingsResponse>(resp);
}

export async function updateRoutewiseSetting(
  modelId: string,
  key: string,
  value: RoutewiseSettingValue,
): Promise<RoutewiseSettingItem> {
  const params = new URLSearchParams({ model_id: modelId });
  const resp = await fetchWithAuth(
    API_BASE,
    `/admin/routewise/model-settings/${encodeURIComponent(key)}?${params.toString()}`,
    {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ value }),
    },
  );
  return jsonOrThrow<RoutewiseSettingItem>(resp);
}

export async function resetRoutewiseSetting(
  modelId: string,
  key: string,
): Promise<RoutewiseSettingItem> {
  const params = new URLSearchParams({ model_id: modelId });
  const resp = await fetchWithAuth(
    API_BASE,
    `/admin/routewise/model-settings/${encodeURIComponent(key)}?${params.toString()}`,
    { method: 'DELETE' },
  );
  return jsonOrThrow<RoutewiseSettingItem>(resp);
}

export async function listRoutewiseProbeSamples(
  opts: ListRoutewiseProbeSamplesOptions = {},
): Promise<ListRoutewiseProbeSamplesResponse> {
  const params = new URLSearchParams();
  if (opts.modelId) params.set('model_id', opts.modelId);
  if (opts.endpointId) params.set('endpoint_id', opts.endpointId);
  if (opts.sinceSeconds !== undefined) params.set('since_seconds', String(opts.sinceSeconds));
  if (opts.limit !== undefined) params.set('limit', String(opts.limit));
  const suffix = params.toString() ? `?${params.toString()}` : '';
  const resp = await fetchWithAuth(API_BASE, `/admin/routewise/probes${suffix}`);
  return jsonOrThrow<ListRoutewiseProbeSamplesResponse>(resp);
}

export async function runRoutewiseProbe(
  payload: RunRoutewiseProbeRequest,
): Promise<RunRoutewiseProbeResponse> {
  const resp = await fetchWithAuth(API_BASE, '/admin/routewise/probes/run', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  return jsonOrThrow<RunRoutewiseProbeResponse>(resp);
}

// ========================================
// RouteWise Decisions (aggregate selection distribution per model)
// ========================================

export type RoutewiseDecisionsRange = '24h' | '7d' | '30d';

export interface RoutewiseSelectionShareItem {
  endpoint: string;
  provider_type: string;
  count: number;
}

// Hedge outcome counts over ALL routewise rows in one bucket. The three counts
// partition every routewise row: not_hedged + hedged_primary_won +
// hedged_backup_won == total routewise rows in the bucket.
export interface RoutewiseDecisionBucketHedge {
  not_hedged: number;
  hedged_primary_won: number;
  hedged_backup_won: number;
}

export interface RoutewiseDecisionBucket {
  bucket_start: string; // ISO8601 UTC
  counts: Record<string, number>; // endpoint -> selection count in the bucket (attributed only)
  hedge: RoutewiseDecisionBucketHedge;
}

// Window-level hedge KPIs. hedge_rate is over all requests; backup_win_rate is
// over hedged requests. median_hedge_delay_ms is null when no hedged rows carry
// a delay sample.
export interface RoutewiseHedgeSummary {
  hedged: number;
  hedge_rate: number;
  backup_won: number;
  backup_win_rate: number;
  median_hedge_delay_ms: number | null;
}

export interface RoutewiseDecisionsResponse {
  model_id: string;
  range: RoutewiseDecisionsRange;
  bucket_seconds: number;
  total_requests: number;
  unattributed_requests: number;
  lp_status_counts: Record<string, number>;
  selection_share: RoutewiseSelectionShareItem[];
  hedge_summary: RoutewiseHedgeSummary;
  buckets: RoutewiseDecisionBucket[];
}

export async function getRoutewiseDecisions(
  modelId: string,
  range: RoutewiseDecisionsRange = '24h',
): Promise<RoutewiseDecisionsResponse> {
  const params = new URLSearchParams({ model_id: modelId, range });
  const resp = await fetchWithAuth(API_BASE, `/admin/routewise/decisions?${params.toString()}`);
  return jsonOrThrow<RoutewiseDecisionsResponse>(resp);
}

// ========================================
// Provider Routes (admin-managed runtime route targets)
// ========================================

export type ProviderRouteKeySource = 'default' | 'db' | 'env' | 'missing';
export type ProviderRouteSource = 'yaml' | 'override' | 'runtime';

export interface ProviderRouteApiKeyRef {
  id: string | null;
  provider: string;
  label: string | null;
  key_prefix: string | null;
  source: ProviderRouteKeySource;
}

export interface ProviderRouteOption {
  provider: string;
  label: string;
  kind: string;
  key_provider: string;
  default_base_url: string;
}

export interface OpenRouterProviderOption {
  provider: string;
  label: string;
}

export type OpenRouterSortPolicy = 'price' | 'throughput' | 'latency';

export interface ProviderRoute {
  model_id: string;
  strategy: string;
  route_id: string;
  route_type: string;
  provider: string;
  upstream_provider: string;
  openrouter_provider?: string | null;
  openrouter_sort?: OpenRouterSortPolicy | null;
  key_provider: string;
  base_url: string;
  api_key_id: string | null;
  api_key: ProviderRouteApiKeyRef;
  provider_model_id: string | null;
  quota_limit: number | null;
  concurrency_limit: number | null;
  quota_current_limit?: number | null;
  quota_used?: number | null;
  quota_remaining?: number | null;
  quota_reset_at?: string | null;
  endpoint_id: string;
  yaml_weight: number;
  effective_weight: number;
  source: ProviderRouteSource;
  updated_at: string | null;
  updated_by: string | null;
}

export interface ListProviderRoutesResponse {
  model_id?: string;
  strategy?: string;
  provider_options: ProviderRouteOption[];
  openrouter_provider_options?: OpenRouterProviderOption[];
  routes: ProviderRoute[];
}

export interface ListOpenRouterProviderOptionsResponse {
  provider_model_id: string;
  providers: OpenRouterProviderOption[];
}

export interface UpdateProviderRoutePayload {
  upstream_provider: string;
  openrouter_provider?: string | null;
  openrouter_sort?: OpenRouterSortPolicy | null;
  base_url: string;
  api_key_id?: string | null;
  provider_model_id?: string | null;
  quota_limit?: number | null;
  concurrency_limit?: number | null;
}

export type ProviderRouteStrategy = 'fixed' | 'routewise';
export type ProviderRouteType = 'quota' | 'concurrency' | 'on_demand';

export interface CreateProviderRoutePayload {
  route_type: ProviderRouteType;
  upstream_provider: string;
  openrouter_provider?: string | null;
  openrouter_sort?: OpenRouterSortPolicy | null;
  base_url: string;
  api_key_id?: string | null;
  provider_model_id: string;
  quota_limit?: number | null;
  concurrency_limit?: number | null;
  weight: number;
}

export interface UpdateProviderRouteCandidatePayload {
  concurrency_limit: number;
}

export interface CreateProviderRouteModelPayload extends CreateProviderRoutePayload {
  model_id: string;
  strategy: ProviderRouteStrategy;
  required_role?: Role;
  pricing: Record<string, string>;
}

export interface VerifyProviderRouteResponse {
  ok: boolean;
}

export async function listProviderRoutes(modelId?: string): Promise<ListProviderRoutesResponse> {
  const path = modelId
    ? `/admin/routing/provider-routes/${encodeURIComponent(modelId)}`
    : '/admin/routing/provider-routes';
  const resp = await fetchWithAuth(API_BASE, path);
  return jsonOrThrow<ListProviderRoutesResponse>(resp);
}

export async function listOpenRouterProviderOptions(
  providerModelId: string,
): Promise<ListOpenRouterProviderOptionsResponse> {
  const params = new URLSearchParams({ provider_model_id: providerModelId });
  const resp = await fetchWithAuth(API_BASE, `/admin/routing/openrouter-providers?${params}`);
  return jsonOrThrow<ListOpenRouterProviderOptionsResponse>(resp);
}

export async function updateProviderRouteStrategy(
  modelId: string,
  strategy: ProviderRouteStrategy,
): Promise<ListProviderRoutesResponse> {
  const resp = await fetchWithAuth(
    API_BASE,
    `/admin/routing/provider-route-strategies/${encodeURIComponent(modelId)}`,
    {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ strategy }),
    },
  );
  return jsonOrThrow<ListProviderRoutesResponse>(resp);
}

export async function createProviderRouteModel(
  payload: CreateProviderRouteModelPayload,
): Promise<ProviderRoute> {
  const resp = await fetchWithAuth(API_BASE, '/admin/routing/provider-route-models', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  return jsonOrThrow<ProviderRoute>(resp);
}

export async function verifyProviderRouteModel(
  payload: CreateProviderRouteModelPayload,
): Promise<VerifyProviderRouteResponse> {
  const resp = await fetchWithAuth(API_BASE, '/admin/routing/provider-route-model-verifications', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  return jsonOrThrow<VerifyProviderRouteResponse>(resp);
}

export async function updateProviderRoute(
  modelId: string,
  routeId: string,
  payload: UpdateProviderRoutePayload,
): Promise<ProviderRoute> {
  const resp = await fetchWithAuth(
    API_BASE,
    `/admin/routing/provider-routes/${encodeURIComponent(modelId)}/${encodeURIComponent(routeId)}`,
    {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        upstream_provider: payload.upstream_provider,
        openrouter_provider: payload.openrouter_provider ?? null,
        openrouter_sort: payload.openrouter_sort ?? null,
        base_url: payload.base_url,
        api_key_id: payload.api_key_id ?? null,
        provider_model_id: payload.provider_model_id ?? null,
        quota_limit: payload.quota_limit ?? null,
        concurrency_limit: payload.concurrency_limit ?? null,
      }),
    },
  );
  return jsonOrThrow<ProviderRoute>(resp);
}

export async function verifyProviderRoute(
  modelId: string,
  routeId: string,
  payload: UpdateProviderRoutePayload,
): Promise<VerifyProviderRouteResponse> {
  const resp = await fetchWithAuth(
    API_BASE,
    `/admin/routing/provider-route-verifications/${encodeURIComponent(modelId)}/${encodeURIComponent(routeId)}`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        upstream_provider: payload.upstream_provider,
        openrouter_provider: payload.openrouter_provider ?? null,
        openrouter_sort: payload.openrouter_sort ?? null,
        base_url: payload.base_url,
        api_key_id: payload.api_key_id ?? null,
        provider_model_id: payload.provider_model_id ?? null,
        quota_limit: payload.quota_limit ?? null,
        concurrency_limit: payload.concurrency_limit ?? null,
      }),
    },
  );
  return jsonOrThrow<VerifyProviderRouteResponse>(resp);
}

export async function createProviderRouteCandidate(
  modelId: string,
  payload: CreateProviderRoutePayload,
): Promise<ProviderRoute> {
  const resp = await fetchWithAuth(
    API_BASE,
    `/admin/routing/provider-route-candidates/${encodeURIComponent(modelId)}`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    },
  );
  return jsonOrThrow<ProviderRoute>(resp);
}

export async function updateProviderRouteCandidate(
  modelId: string,
  routeId: string,
  payload: UpdateProviderRouteCandidatePayload,
): Promise<ProviderRoute> {
  const resp = await fetchWithAuth(
    API_BASE,
    `/admin/routing/provider-route-candidates/${encodeURIComponent(modelId)}/${encodeURIComponent(routeId)}`,
    {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    },
  );
  return jsonOrThrow<ProviderRoute>(resp);
}

export async function verifyProviderRouteCandidate(
  modelId: string,
  payload: CreateProviderRoutePayload,
): Promise<VerifyProviderRouteResponse> {
  const resp = await fetchWithAuth(
    API_BASE,
    `/admin/routing/provider-route-candidate-verifications/${encodeURIComponent(modelId)}`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    },
  );
  return jsonOrThrow<VerifyProviderRouteResponse>(resp);
}

export async function deleteProviderRoute(
  modelId: string,
  routeId: string,
): Promise<ProviderRoute> {
  const resp = await fetchWithAuth(
    API_BASE,
    `/admin/routing/provider-routes/${encodeURIComponent(modelId)}/${encodeURIComponent(routeId)}`,
    {
      method: 'DELETE',
    },
  );
  return jsonOrThrow<ProviderRoute>(resp);
}

export async function deleteProviderRouteCandidate(
  modelId: string,
  routeId: string,
): Promise<ListProviderRoutesResponse> {
  const resp = await fetchWithAuth(
    API_BASE,
    `/admin/routing/provider-route-candidates/${encodeURIComponent(modelId)}/${encodeURIComponent(routeId)}`,
    {
      method: 'DELETE',
    },
  );
  return jsonOrThrow<ListProviderRoutesResponse>(resp);
}

// ========================================
// Provider API Keys (admin-managed runtime credentials)
// ========================================

export type ProviderDefinitionSource = 'built_in' | 'custom';
export type ProviderDefinitionAdapterKind = 'openai_compat';

export interface ProviderDefinitionItem {
  provider: string;
  display_name: string;
  adapter_kind: string;
  default_base_url: string;
  source: ProviderDefinitionSource;
  status: string;
  keys_count: number;
  models_count: number;
  created_at: string | null;
  updated_at: string | null;
}

export interface ListProviderDefinitionsResponse {
  providers: ProviderDefinitionItem[];
  adapter_kinds: ProviderDefinitionAdapterKind[];
}

export interface ProbeProviderDefinitionPayload {
  default_base_url: string;
  api_key: string;
  probe_model_id: string;
}

export interface ProbeProviderDefinitionResponse {
  ok: boolean;
  streaming: boolean;
  first_event_ttft_ms: number | null;
  first_content_ttft_ms: number | null;
  preview: string | null;
}

export interface CreateProviderDefinitionPayload extends ProbeProviderDefinitionPayload {
  provider: string;
  display_name: string;
  adapter_kind: ProviderDefinitionAdapterKind;
  api_key_label?: string | null;
}

export interface UpdateProviderDefinitionPayload {
  display_name?: string | null;
  default_base_url?: string | null;
  api_key?: string | null;
  probe_model_id?: string | null;
}

export interface DeleteProviderDefinitionResponse {
  provider: string;
  deleted_keys: number;
}

export async function listProviderDefinitions(): Promise<ListProviderDefinitionsResponse> {
  const resp = await fetchWithAuth(API_BASE, '/admin/provider-definitions');
  return jsonOrThrow<ListProviderDefinitionsResponse>(resp);
}

export async function probeProviderDefinition(
  payload: ProbeProviderDefinitionPayload,
): Promise<ProbeProviderDefinitionResponse> {
  const resp = await fetchWithAuth(API_BASE, '/admin/provider-definitions/verify', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  return jsonOrThrow<ProbeProviderDefinitionResponse>(resp);
}

export async function createProviderDefinition(
  payload: CreateProviderDefinitionPayload,
): Promise<ProviderDefinitionItem> {
  const resp = await fetchWithAuth(API_BASE, '/admin/provider-definitions', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  return jsonOrThrow<ProviderDefinitionItem>(resp);
}

export async function updateProviderDefinition(
  provider: string,
  payload: UpdateProviderDefinitionPayload,
): Promise<ProviderDefinitionItem> {
  const resp = await fetchWithAuth(
    API_BASE,
    `/admin/provider-definitions/${encodeURIComponent(provider)}`,
    {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    },
  );
  return jsonOrThrow<ProviderDefinitionItem>(resp);
}

export async function deleteProviderDefinition(
  provider: string,
): Promise<DeleteProviderDefinitionResponse> {
  const resp = await fetchWithAuth(
    API_BASE,
    `/admin/provider-definitions/${encodeURIComponent(provider)}`,
    { method: 'DELETE' },
  );
  return jsonOrThrow<DeleteProviderDefinitionResponse>(resp);
}

export type ProviderKeySource = 'env' | 'db';

/** Lowest user role allowed to spend a provider key. 'free' = shared by all tiers. */
export type ProviderKeyMinRole = 'free' | 'pro' | 'internal' | 'admin';

export interface ProviderApiKeyItem {
  id: string | null;
  provider: string;
  key_prefix: string;
  label: string | null;
  source: ProviderKeySource;
  status: string;
  created_at: string | null;
  /** The tier currently enforced for this credential (strictest declaration wins). */
  min_role: ProviderKeyMinRole;
  /**
   * The tier this record itself declares — what editing this row changes. Lower
   * than `min_role` when another record (a row with the same key, or an env
   * reservation for it) declares something stricter.
   */
  declared_min_role?: ProviderKeyMinRole;
  /**
   * True for an env entry listed only to expose its tier reservation, because an
   * active DB row holds the same credential. Only the tier is editable: the DB row
   * is what enables, disables or deletes the key.
   */
  reservation_only?: boolean;
}

export interface ListProviderApiKeysResponse {
  provider: string | null;
  keys: ProviderApiKeyItem[];
}

export interface ListProviderApiKeyProvidersResponse {
  providers: string[];
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

export interface EnableProviderEnvKeyResponse {
  id: string;
  provider: string;
  pools_updated: number;
}

export interface SetProviderApiKeyStatusResponse {
  id: string;
  provider: string;
  status: 'active' | 'disabled';
  pools_updated: number;
}

export interface SetProviderApiKeyMinRoleResponse {
  id: string;
  provider: string;
  min_role: ProviderKeyMinRole;
  pools_updated: number;
}

export interface VerifyProviderApiKeyResponse {
  ok: boolean;
}

export async function listProviderKeys(provider?: string): Promise<ListProviderApiKeysResponse> {
  const params = new URLSearchParams();
  if (provider) params.set('provider', provider);
  const qs = params.toString();
  const path = qs ? `/admin/provider-keys?${qs}` : '/admin/provider-keys';
  const resp = await fetchWithAuth(API_BASE, path);
  return jsonOrThrow<ListProviderApiKeysResponse>(resp);
}

export async function listProviderKeyProviders(): Promise<ListProviderApiKeyProvidersResponse> {
  const resp = await fetchWithAuth(API_BASE, '/admin/provider-keys/providers');
  return jsonOrThrow<ListProviderApiKeyProvidersResponse>(resp);
}

export async function addProviderKey(
  provider: string,
  apiKey: string,
  label?: string,
  minRole?: ProviderKeyMinRole,
): Promise<AddProviderApiKeyResponse> {
  const resp = await fetchWithAuth(API_BASE, '/admin/provider-keys', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      provider,
      api_key: apiKey,
      ...(label ? { label } : {}),
      ...(minRole ? { min_role: minRole } : {}),
    }),
  });
  return jsonOrThrow<AddProviderApiKeyResponse>(resp);
}

/** Reserve a DB-sourced key for a tier ('free' releases it back to every tier). */
export async function setProviderKeyMinRole(
  id: string,
  minRole: ProviderKeyMinRole,
): Promise<SetProviderApiKeyMinRoleResponse> {
  const resp = await fetchWithAuth(
    API_BASE,
    `/admin/provider-keys/${encodeURIComponent(id)}/min-role`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ min_role: minRole }),
    },
  );
  return jsonOrThrow<SetProviderApiKeyMinRoleResponse>(resp);
}

/**
 * Reserve an env-sourced key for a tier. Env keys have no row id, so they are
 * addressed by provider + env key id (as with disable/enable-env).
 */
export async function setProviderEnvKeyMinRole(
  provider: string,
  envKeyId: string,
  minRole: ProviderKeyMinRole,
): Promise<SetProviderApiKeyMinRoleResponse> {
  const resp = await fetchWithAuth(API_BASE, '/admin/provider-keys/min-role-env', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ provider, env_key_id: envKeyId, min_role: minRole }),
  });
  return jsonOrThrow<SetProviderApiKeyMinRoleResponse>(resp);
}

export async function verifyProviderKey(
  provider: string,
  apiKey: string,
): Promise<VerifyProviderApiKeyResponse> {
  const resp = await fetchWithAuth(API_BASE, '/admin/provider-keys/verify', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ provider, api_key: apiKey }),
  });
  return jsonOrThrow<VerifyProviderApiKeyResponse>(resp);
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

export async function enableProviderEnvKey(
  provider: string,
  envKeyId: string,
): Promise<EnableProviderEnvKeyResponse> {
  const resp = await fetchWithAuth(API_BASE, '/admin/provider-keys/enable-env', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ provider, env_key_id: envKeyId }),
  });
  return jsonOrThrow<EnableProviderEnvKeyResponse>(resp);
}

export async function setProviderKeyStatus(
  id: string,
  enabled: boolean,
): Promise<SetProviderApiKeyStatusResponse> {
  const action = enabled ? 'enable' : 'disable';
  const resp = await fetchWithAuth(
    API_BASE,
    `/admin/provider-keys/${encodeURIComponent(id)}/${action}`,
    { method: 'POST' },
  );
  return jsonOrThrow<SetProviderApiKeyStatusResponse>(resp);
}

// ========================================
// Site Updates (homepage announcements / banner)
// ========================================

export type SiteUpdatePlacement = 'feed' | 'banner';

export interface SiteUpdateItem {
  id: string;
  title: string;
  body: string;
  placement: SiteUpdatePlacement;
  published: boolean;
  link_url: string | null;
  link_label: string | null;
  created_by: string;
  created_at: string;
  updated_at: string;
}

export interface ListSiteUpdatesResponse {
  total: number;
  updates: SiteUpdateItem[];
}

export interface SiteUpdateInput {
  title: string;
  body: string;
  placement: SiteUpdatePlacement;
  published: boolean;
  link_url: string | null;
  link_label: string | null;
}

export async function listSiteUpdates(): Promise<ListSiteUpdatesResponse> {
  const resp = await fetchWithAuth(API_BASE, '/admin/site-updates');
  return jsonOrThrow<ListSiteUpdatesResponse>(resp);
}

export async function createSiteUpdate(input: SiteUpdateInput): Promise<SiteUpdateItem> {
  const resp = await fetchWithAuth(API_BASE, '/admin/site-updates', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  });
  return jsonOrThrow<SiteUpdateItem>(resp);
}

export async function updateSiteUpdate(
  id: string,
  patch: Partial<SiteUpdateInput>,
): Promise<SiteUpdateItem> {
  const resp = await fetchWithAuth(API_BASE, `/admin/site-updates/${encodeURIComponent(id)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  });
  return jsonOrThrow<SiteUpdateItem>(resp);
}

export async function deleteSiteUpdate(id: string): Promise<void> {
  const resp = await fetchWithAuth(API_BASE, `/admin/site-updates/${encodeURIComponent(id)}`, {
    method: 'DELETE',
  });
  await jsonOrThrow<{ message: string }>(resp);
}
