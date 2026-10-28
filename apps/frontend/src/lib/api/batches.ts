import { fetchWithAuth, jsonOrThrow, uploadWithAuth, type UploadProgress } from './client';
import { config } from '@/config/env';

const API_BASE = config.apiBase;

/** The statuses a batch shows, the same for every target (those of `/v1/batches`). */
export type BatchStatus =
  | 'validating'
  | 'in_progress'
  | 'finalizing'
  | 'completed'
  | 'failed'
  | 'expired'
  | 'cancelling'
  | 'cancelled';

/**
 * How the console groups a batch, from the status it shows and (completed)
 * its counts -- computed by the gateway, so the list, the filter and the
 * details agree: `processing` (validating, running, finalizing results or
 * cancelling), `completed` (every request succeeded), `issues` (failed,
 * execution window ran out, or completed with an unsuccessful request) and
 * `cancelled` (the cancel has finished).
 */
export type BatchCategory = 'processing' | 'completed' | 'issues' | 'cancelled';
export const BATCH_CATEGORIES: BatchCategory[] = ['processing', 'completed', 'issues', 'cancelled'];

/** What a batch runs: the endpoint each of its requests targets. */
export type BatchTarget = 'chat' | 'embeddings' | 'messages' | 'responses';

/** How a batch's requests arrived: directly in the create call, or as a JSONL file. */
export type BatchInput = 'inline' | 'file';

/**
 * One batch as the console shows it. Every batch has the same shape and status
 * words, whatever its target or how its requests arrived -- including those
 * the retired Message Batches API stored.
 */
export interface ConsoleBatch {
  id: string;
  user_id?: string | null;
  /** The stored input kind, for diagnosis only; use `input` to show it. */
  api_surface: string;
  input: BatchInput;
  target: BatchTarget | null;
  endpoint: string | null;
  status: BatchStatus;
  /**
   * The console's group of `status` (see `BatchCategory`); what the page shows
   * first. Absent from a gateway older than the categories (during a rolling
   * upgrade): use `batchCategory`, which falls back to the same rule.
   */
  category?: BatchCategory;
  phase: string | null;
  models: string[];
  /** Requests in the batch, and how many have completed or failed (cancelled ones apart). */
  request_counts: { total: number; completed: number; failed: number };
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
  /** The name and description given in the console (null for a batch the API created). */
  name?: string | null;
  description?: string | null;
  /** `console` or `api`: where the batch was created. */
  created_via?: 'console' | 'api';
  /** Set while the batch is paused: until when, and why (`quota`, `role`, or null if unknown). */
  paused_until?: number | null;
  paused_reason?: 'quota' | 'role' | string | null;
}

/** A source file's state for its owner. */
export interface BatchSourceState {
  id: string | null;
  state: 'available' | 'deleted' | 'expired' | 'missing';
  filename?: string | null;
  sha256?: string | null;
  bytes?: number;
  expires_at?: number | null;
  derived?: boolean;
}

/** How a console batch was configured. */
export interface BatchCreation {
  source_file_id: string | null;
  source_filename: string | null;
  source_sha256: string | null;
  endpoint: string | null;
  type_source: 'url' | 'structure' | 'choice' | null;
  overrides: BatchOverrides;
  confirmed_model: { requested: string; canonical: string } | null;
  credential_key_prefix: string | null;
  copied_from: string | null;
  stores_copy: boolean;
}

/**
 * Whether a batch can be copied into a new one: `ready` (its source is kept),
 * `reupload` (its type can be created but its source is gone: upload it
 * again, the settings carry over) or `unavailable` (`inline_input`: no input
 * file; `unsupported_target`: a type the console does not create yet).
 */
export interface BatchCopy {
  state: 'ready' | 'reupload' | 'unavailable';
  reason: 'inline_input' | 'unsupported_target' | 'source_unavailable' | null;
}

export interface BatchDetail extends ConsoleBatch {
  creation: BatchCreation | null;
  source: BatchSourceState | null;
  copy: BatchCopy;
}

export interface ConsoleBatchPage {
  object: 'list';
  data: ConsoleBatch[];
  has_more: boolean;
  last_id: string | null;
  /** Present (true) from a gateway that filters by category; absent from an older one. */
  category_filter?: boolean;
  /** The category this page was filtered by, as the gateway applied it (null: none). */
  category?: BatchCategory | null;
}

/**
 * The gateway that answered did not filter by the category asked for: one
 * older than the categories, which ignores the parameter and would return
 * every batch. Its page must not be shown as filtered.
 */
export class BatchCategoryFilterUnavailable extends Error {
  constructor() {
    super('This gateway cannot filter batches by category yet.');
    this.name = 'BatchCategoryFilterUnavailable';
  }
}

/**
 * What the console offers the signed-in user for Batch.
 *
 * `status` is `disabled` when `BATCH_ENABLED` is off and `unavailable` when it
 * is on but the gateway's batch store is not ready. `role_may_submit` says only
 * whether the user's role reaches `min_role`, not whether a submission would
 * pass quotas. `has_history` is null when it could not be looked up (and when
 * Batch is not enabled); `show_entry` then still holds for a possible history.
 */
export interface BatchCapabilities {
  user_id: string;
  status: 'enabled' | 'disabled' | 'unavailable';
  enabled: boolean;
  min_role: string;
  role_may_submit: boolean;
  has_history: boolean | null;
  history_error: boolean;
  show_entry: boolean;
}

export async function getMyBatchCapabilities(): Promise<BatchCapabilities> {
  const resp = await fetchWithAuth(API_BASE, '/user/batch-capabilities');
  return jsonOrThrow<BatchCapabilities>(resp);
}

/** Filters of the batch list: a category (or exact status), and a name or exact id. */
export interface MyBatchFilters {
  category?: BatchCategory | '';
  status?: BatchStatus | '';
  q?: string;
}

/** The signed-in user's batches, newest first. */
export async function listMyBatches(
  after?: string | null,
  limit = 20,
  filters: MyBatchFilters = {},
): Promise<ConsoleBatchPage> {
  const params = new URLSearchParams({ limit: String(limit) });
  if (after) params.set('after', after);
  if (filters.category) params.set('category', filters.category);
  if (filters.status) params.set('status', filters.status);
  if (filters.q?.trim()) params.set('q', filters.q.trim());
  const resp = await fetchWithAuth(API_BASE, `/user/batches?${params.toString()}`);
  const page = await jsonOrThrow<ConsoleBatchPage>(resp);
  // Checked on every page: during an upgrade another gateway may answer the next one.
  if (filters.category && (page.category_filter !== true || page.category !== filters.category)) {
    throw new BatchCategoryFilterUnavailable();
  }
  return page;
}

/** One of the signed-in user's batches, with how it was created and whether it can be copied. */
export async function getMyBatch(batchId: string): Promise<BatchDetail> {
  const resp = await fetchWithAuth(API_BASE, `/user/batches/${encodeURIComponent(batchId)}`);
  return jsonOrThrow<BatchDetail>(resp);
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

/** Fetch a JSONL download and hand it to the browser to save as `filename`. */
async function saveDownload(path: string, filename: string): Promise<void> {
  const resp = await fetchWithAuth(API_BASE, path);
  if (!resp.ok) {
    await jsonOrThrow<never>(resp);
    return;
  }
  const blob = await resp.blob();
  if (typeof URL.createObjectURL !== 'function') return;
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

/** Download a finished batch's results as a JSONL file the browser saves. */
export async function downloadMyResults(batchId: string, kind: ResultKind = 'all'): Promise<void> {
  const params = new URLSearchParams({ kind });
  await saveDownload(
    `/user/batches/${encodeURIComponent(batchId)}/results?${params.toString()}`,
    `${batchId}_${kind}.jsonl`,
  );
}

/** Download a batch's stored requests (`custom_id, method, url, body` lines). */
export async function downloadStoredRequests(batchId: string): Promise<void> {
  await saveDownload(
    `/user/batches/${encodeURIComponent(batchId)}/requests`,
    `${batchId}_requests.jsonl`,
  );
}

/** Download a finished Chat or Embeddings batch's failed, expired and cancelled requests. */
export async function downloadUnfinishedRequests(batchId: string): Promise<void> {
  await saveDownload(
    `/user/batches/${encodeURIComponent(batchId)}/unfinished-requests`,
    `${batchId}_unfinished.jsonl`,
  );
}

/** Download one of the user's uploaded files as stored. */
export async function downloadSourceFile(fileId: string, filename: string): Promise<void> {
  await saveDownload(`/user/batch-files/${encodeURIComponent(fileId)}/content`, filename);
}

// -- creating a batch in the console -------------------------------------------------

/** The two types the console creates. */
export type ConsoleTarget = 'chat' | 'embeddings';
export const CONSOLE_ENDPOINTS: Record<ConsoleTarget, string> = {
  chat: '/v1/chat/completions',
  embeddings: '/v1/embeddings',
};

export interface CatalogModel {
  id: string;
  name: string;
  max_output_length: number;
  supported_sampling_parameters: string[];
}

/** What the create page shows: limits, usage, the run credential and the models. */
export interface BatchCreateContext {
  user_id: string;
  role_may_submit: boolean;
  min_role: string;
  submission_enabled: boolean;
  limits: {
    max_file_bytes: number;
    max_line_bytes: number;
    max_requests: number;
    uploads_per_hour: number;
    files_max_total_bytes: number;
    max_active_batches: number;
    file_retention_days: number;
    result_retention_days: number;
    completion_window_hours: number;
    /** How long the gateway is certain to recognise a submission key again. */
    submission_replay_seconds: number;
  };
  usage: { stored_bytes: number; uploads_last_hour: number; active_batches: number };
  /** The active API key a batch runs under -- its prefix and an opaque revision only. */
  credential: { key_id: string; key_prefix: string; revision: string } | null;
  quota: { daily_usd: number | null; spent_today_usd: number | null } | null;
  models: Record<ConsoleTarget, CatalogModel[]>;
}

export async function getCreateContext(): Promise<BatchCreateContext> {
  const resp = await fetchWithAuth(API_BASE, '/user/batch-create/context');
  return jsonOrThrow<BatchCreateContext>(resp);
}

/** An uploaded source file. */
export interface BatchSourceFile {
  id: string;
  bytes: number;
  created_at: number;
  expires_at: number | null;
  filename: string | null;
  sha256: string | null;
  line_count: number | null;
}

export interface BatchSourceFileList {
  data: BatchSourceFile[];
  has_more: boolean;
  usage: { stored_bytes: number; uploads_last_hour: number };
  files_max_total_bytes: number;
}

export async function listSourceFiles(): Promise<BatchSourceFileList> {
  const resp = await fetchWithAuth(API_BASE, '/user/batch-files');
  return jsonOrThrow<BatchSourceFileList>(resp);
}

export async function deleteSourceFile(fileId: string): Promise<{ id: string; deleted: boolean }> {
  const resp = await fetchWithAuth(API_BASE, `/user/batch-files/${encodeURIComponent(fileId)}`, {
    method: 'DELETE',
  });
  return jsonOrThrow<{ id: string; deleted: boolean }>(resp);
}

/** Upload a JSONL source file, with progress; cancelled through `signal`. */
export async function uploadSourceFile(
  file: File,
  options: {
    /** The account the page shows; an upload never lands in, or answers, another. */
    expectedUserId: string;
    onProgress?: (progress: UploadProgress) => void;
    signal?: AbortSignal;
  },
): Promise<BatchSourceFile> {
  const form = new FormData();
  form.append('purpose', 'batch');
  form.append('file', file, file.name);
  const resp = await uploadWithAuth(API_BASE, '/user/batch-files', form, options);
  return jsonOrThrow<BatchSourceFile>(resp);
}

export interface BatchOverrides {
  model?: string;
  temperature?: number;
  max_tokens?: number;
}

/** One problem the preflight found: where, and what to change -- never the request itself. */
export interface PreflightError {
  line: number | null;
  custom_id: string | null;
  field: string | null;
  code: string;
  message: string;
}

export interface ParamDistribution {
  values: Record<string, number>;
  other: number;
  unset: number;
}

export interface PreflightReport {
  source: {
    id: string;
    filename: string | null;
    sha256: string | null;
    bytes: number;
    lines: number;
    expires_at?: number | null;
  };
  detected: {
    type: ConsoleTarget | null;
    endpoint: string | null;
    source: 'url' | 'structure' | 'choice' | null;
    needs_type_choice: boolean;
    rows: { chat: number; embeddings: number; undetermined: number };
    url_missing_rows: number;
    models: { name: string; rows: number; available: boolean; canonical: string | null }[];
    models_other_rows: number;
    model_unset_rows: number;
  };
  params: Partial<Record<'temperature' | 'max_tokens', ParamDistribution>>;
  other_params: Record<string, number>;
  model_resolution: { requested: string; canonical: string } | null;
  valid: boolean;
  request_count: number;
  stores_copy: boolean;
  error_count: number;
  errors_truncated: boolean;
  errors: PreflightError[];
  preview: {
    line: number;
    custom_id: string;
    filled: string[];
    truncated: boolean;
    request: Record<string, unknown>;
  }[];
}

/** An error the create page acts on, with the gateway's reason code when it gave one. */
export class BatchConsoleError extends Error {
  constructor(
    public status: number,
    public code: string | null,
    message: string,
    public report: PreflightReport | null = null,
  ) {
    super(message);
    this.name = 'BatchConsoleError';
  }
}

async function consoleJson<T>(resp: Response): Promise<T> {
  if (resp.ok) return (await resp.json()) as T;
  let data: Record<string, unknown> | null = null;
  try {
    data = (await resp.json()) as Record<string, unknown>;
  } catch {
    // Not JSON: an edge proxy's page. The status still says what happened.
  }
  const error = (data?.error ?? null) as Record<string, unknown> | null;
  const detail = data?.detail;
  const message =
    (typeof error?.message === 'string' && error.message) ||
    (typeof detail === 'string' && detail) ||
    `HTTP ${resp.status}`;
  const code = typeof error?.code === 'string' ? error.code : null;
  const report = (error?.report as PreflightReport | undefined) ?? null;
  throw new BatchConsoleError(resp.status, code, message, report);
}

export async function preflightSource(request: {
  source_file_id: string;
  phase: 'identify' | 'validate';
  endpoint?: string | null;
  overrides?: BatchOverrides;
}): Promise<PreflightReport> {
  const resp = await fetchWithAuth(API_BASE, '/user/batch-preflight', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(request),
  });
  return consoleJson<PreflightReport>(resp);
}

/** What a confirmed submission sends: the source, type, settings, model and credential. */
export interface ConsoleSubmission {
  source_file_id: string;
  source_sha256: string;
  endpoint: string;
  overrides: BatchOverrides;
  confirmed_model: { requested: string; canonical: string };
  credential: { key_id: string; revision: string };
  name: string;
  description: string;
  copied_from: string | null;
}

/** Submit under `key`; the same key and body always name the same batch. */
export async function createConsoleBatch(
  submission: ConsoleSubmission,
  key: string,
): Promise<BatchDetail> {
  const resp = await fetchWithAuth(API_BASE, '/user/batches', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Idempotency-Key': key },
    body: JSON.stringify(submission),
  });
  return consoleJson<BatchDetail>(resp);
}

/** What a submission key is known to have created, read only. */
export interface SubmissionLookup {
  state: 'created' | 'deleted' | 'unknown';
  batch: BatchDetail | null;
}

export async function findSubmission(key: string): Promise<SubmissionLookup> {
  const resp = await fetchWithAuth(API_BASE, `/user/batch-submissions/${encodeURIComponent(key)}`);
  return consoleJson<SubmissionLookup>(resp);
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
  /** A status as the list shows it (a file batch being sealed is `finalizing`). */
  status?: BatchStatus | '';
  after?: string | null;
  limit?: number;
}

/** Every user's batches, newest first. */
export async function listAllBatches(filters: AdminBatchFilters = {}): Promise<ConsoleBatchPage> {
  const params = new URLSearchParams({ limit: String(filters.limit ?? 50) });
  if (filters.userId) params.set('user_id', filters.userId);
  if (filters.status) params.set('status', filters.status);
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

/** Statuses after which a batch never changes again. */
export const FINAL_STATUSES = new Set<BatchStatus>(['completed', 'failed', 'expired', 'cancelled']);

/** Whether a batch can still be cancelled. */
export function isCancellable(batch: ConsoleBatch): boolean {
  return ['validating', 'in_progress'].includes(batch.status) && batch.phase !== 'sealing';
}
