import { fetchWithAuth, jsonOrThrow } from './client';
import { config } from '@/config/env';

const API_BASE = config.apiBase;

/** The API a batch was created through. */
export type BatchSurface = 'openai_inline' | 'openai_file' | 'anthropic';

/**
 * One batch as the console shows it, whatever its API surface.
 *
 * `status` is the batch's status as its own protocol names it: OpenAI's
 * (`validating`, `in_progress`, `finalizing`, `completed`, `failed`,
 * `expired`, `cancelling`, `cancelled`) or Anthropic's (`in_progress`,
 * `canceling`, `ended`).
 */
export interface ConsoleBatch {
  id: string;
  user_id?: string | null;
  api_surface: BatchSurface;
  target: string | null;
  endpoint: string | null;
  status: string;
  phase: string | null;
  models: string[];
  /** OpenAI's `{total, completed, failed}` or Anthropic's five tallies. */
  request_counts: Record<string, number>;
  cancelled: number;
  expired: number;
  usage: { input_tokens: number; output_tokens: number; total_tokens: number };
  /** What the batch's finished attempts were billed; attempts of unknown cost are not in it. */
  cost_usd: number;
  /** Attempts whose cost is unknown (an upstream may have run without reporting back). */
  cost_unknown_attempts: number;
  created_at: number | null;
  expires_at: number | null;
  ended_at: number | null;
  results_expires_at: number | null;
  input_file_id: string | null;
  output_file_id: string | null;
  error_file_id: string | null;
  results_available: boolean;
  metadata: Record<string, string> | null;
  error: { code?: string; message?: string } | null;
  validation_error: { data?: { code?: string; message?: string; line?: number | null }[] } | null;
  protocol_profile: string | null;
}

export interface ConsoleBatchPage {
  object: 'list';
  data: ConsoleBatch[];
  has_more: boolean;
  last_id: string | null;
}

/** The signed-in user's batches, newest first. */
export async function listMyBatches(after?: string | null, limit = 20): Promise<ConsoleBatchPage> {
  const params = new URLSearchParams({ limit: String(limit) });
  if (after) params.set('after', after);
  const resp = await fetchWithAuth(API_BASE, `/user/batches?${params.toString()}`);
  return jsonOrThrow<ConsoleBatchPage>(resp);
}

export async function cancelMyBatch(batchId: string): Promise<ConsoleBatch> {
  const resp = await fetchWithAuth(
    API_BASE,
    `/user/batches/${encodeURIComponent(batchId)}/cancel`,
    {
      method: 'POST',
    },
  );
  return jsonOrThrow<ConsoleBatch>(resp);
}

export async function deleteMyBatch(batchId: string): Promise<{ id: string; deleted: boolean }> {
  const resp = await fetchWithAuth(API_BASE, `/user/batches/${encodeURIComponent(batchId)}`, {
    method: 'DELETE',
  });
  return jsonOrThrow<{ id: string; deleted: boolean }>(resp);
}

/** Which result file to download: all results, or a file batch's outputs or errors. */
export type ResultKind = 'all' | 'output' | 'error';

/** Download a finished batch's results as a JSONL file the browser saves. */
export async function downloadMyResults(batchId: string, kind: ResultKind = 'all'): Promise<void> {
  const params = new URLSearchParams({ kind });
  const resp = await fetchWithAuth(
    API_BASE,
    `/user/batches/${encodeURIComponent(batchId)}/results?${params.toString()}`,
  );
  if (!resp.ok) {
    await jsonOrThrow<never>(resp);
    return;
  }
  const blob = await resp.blob();
  if (typeof URL.createObjectURL !== 'function') return;
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = `${batchId}_${kind}.jsonl`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

/** A user's batch webhook. `secret` is present only right after it was created or rotated. */
export interface BatchWebhook {
  configured: boolean;
  url?: string;
  enabled?: boolean;
  created_at?: number | null;
  updated_at?: number | null;
  secret?: string;
}

export interface BatchWebhookEvent {
  id: string;
  batch_id: string;
  type: string;
  status: 'pending' | 'delivered' | 'failed';
  attempts: number;
  last_status_code: number | null;
  last_error: string | null;
  created_at: number | null;
  delivered_at: number | null;
  next_attempt_at: number | null;
}

export async function getMyWebhook(): Promise<BatchWebhook> {
  const resp = await fetchWithAuth(API_BASE, '/user/batch-webhook');
  return jsonOrThrow<BatchWebhook>(resp);
}

export async function saveMyWebhook(settings: {
  url: string;
  enabled: boolean;
  rotate_secret?: boolean;
}): Promise<BatchWebhook> {
  const resp = await fetchWithAuth(API_BASE, '/user/batch-webhook', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(settings),
  });
  return jsonOrThrow<BatchWebhook>(resp);
}

export async function deleteMyWebhook(): Promise<{ deleted: boolean }> {
  const resp = await fetchWithAuth(API_BASE, '/user/batch-webhook', { method: 'DELETE' });
  return jsonOrThrow<{ deleted: boolean }>(resp);
}

export async function listMyWebhookEvents(): Promise<{ data: BatchWebhookEvent[] }> {
  const resp = await fetchWithAuth(API_BASE, '/user/batch-webhook/events');
  return jsonOrThrow<{ data: BatchWebhookEvent[] }>(resp);
}

// -- administrators ------------------------------------------------------------------

export interface AdminBatchFilters {
  userId?: string;
  status?: string;
  surface?: BatchSurface | '';
  after?: string | null;
  limit?: number;
}

/** Every user's batches, newest first. */
export async function listAllBatches(filters: AdminBatchFilters = {}): Promise<ConsoleBatchPage> {
  const params = new URLSearchParams({ limit: String(filters.limit ?? 50) });
  if (filters.userId) params.set('user_id', filters.userId);
  if (filters.status) params.set('status', filters.status);
  if (filters.surface) params.set('surface', filters.surface);
  if (filters.after) params.set('after', filters.after);
  const resp = await fetchWithAuth(API_BASE, `/admin/batch/jobs?${params.toString()}`);
  return jsonOrThrow<ConsoleBatchPage>(resp);
}

export interface AdminBatchUserSummary {
  user_id: string;
  batches: number;
  active: number;
  requests: number;
  completed: number;
  failed: number;
  cost_usd: number;
  prompt_tokens: number;
  completion_tokens: number;
  unknown_cost_attempts: number;
  last_created_at: number | null;
}

export interface AdminBatchSummary {
  users: AdminBatchUserSummary[];
  totals: Omit<AdminBatchUserSummary, 'user_id' | 'last_created_at'>;
}

export async function getBatchSummary(): Promise<AdminBatchSummary> {
  const resp = await fetchWithAuth(API_BASE, '/admin/batch/summary');
  return jsonOrThrow<AdminBatchSummary>(resp);
}

export async function adminCancelBatch(batchId: string): Promise<ConsoleBatch> {
  const resp = await fetchWithAuth(
    API_BASE,
    `/admin/batch/jobs/${encodeURIComponent(batchId)}/cancel`,
    { method: 'POST' },
  );
  return jsonOrThrow<ConsoleBatch>(resp);
}

/** Statuses after which a batch never changes again (either protocol's spelling). */
export const FINAL_STATUSES = new Set(['completed', 'failed', 'expired', 'cancelled', 'ended']);

/** Whether a batch can still be cancelled. */
export function isCancellable(batch: ConsoleBatch): boolean {
  return ['validating', 'in_progress'].includes(batch.status) && batch.phase !== 'sealing';
}
