// Cloud agent sandbox API (issue #1041).
//
// Two things here are shaped by the backend's design rather than by
// convenience:
//
// 1. **The event stream is read with fetch + ReadableStream, not EventSource.**
//    EventSource cannot set an Authorization header, and these endpoints are
//    authenticated. This mirrors the streaming-chat client in `chat.ts`.
// 2. **Every frame carries a global event id, and reconnects resume from it.**
//    The gateway applies a finite cap to long-lived SSE, so a job that outlives
//    one connection is normal, not exceptional — resuming by cursor is how the
//    UI stays complete across that.

import { fetchWithAuth, jsonOrThrow } from './client';
import { config } from '@/config/env';

const API_BASE = config.apiBase;

/** Server-side job states. The UI maps these onto its own display states. */
export type AgentJobApiState =
  | 'waiting'
  | 'queued'
  | 'running'
  | 'publishing'
  | 'succeeded'
  | 'failed'
  | 'cancelled';

export interface AgentJobApi {
  id: string;
  repo: string;
  task_prompt: string;
  runtime: string;
  model: string;
  base_sha: string | null;
  state: AgentJobApiState;
  cancel_requested: boolean;
  current_attempt_id: number | null;
  published_pr_url: string | null;
  published_commit_sha?: string | null;
  detail: string | null;
  budget_usd: number | null;
  metadata: Record<string, unknown> | null;
  created_at: string | null;
  updated_at: string | null;
  // Read server-side from the billing ledger, never from the agent's own
  // report. Null means "no ledger configured", which is not the same as zero.
  spent_usd: number | null;
  tokens_in: number | null;
  tokens_out: number | null;
  model_calls: number | null;
  setup_egress_tier: string | null;
  agent_egress_tier: string | null;
  /** Conversation fields are optional so pre-thread deployments remain readable. */
  thread_id?: string | null;
  parent_job_id?: string | null;
  turn_no?: number;
}

export interface AgentThreadMessageApi {
  id: number;
  role: 'user' | 'assistant';
  content: string;
  job_id: string;
  created_at: string | null;
}

export interface AgentThreadApi {
  thread_id: string;
  title?: string;
  repo?: string;
  messages: AgentThreadMessageApi[];
  jobs: AgentJobApi[];
  created_at?: string | null;
  updated_at?: string | null;
}

export interface AgentJobEventApi {
  id: number;
  attempt_id: number;
  seq: number;
  event_type: string;
  payload: Record<string, unknown> | null;
  created_at: string | null;
}

export interface AgentConfigApi {
  repos: string[];
  runtimes: string[];
  /** Models an agent job can actually call — the create endpoint's own list. */
  models: string[];
  default_budget_usd: number;
  setup_egress_tier: string | null;
  agent_egress_tier: string | null;
  github_connected: boolean;
  github_install_url: string | null;
}

/** What this deployment will actually accept — the source for the pickers. */
export async function getAgentConfig(): Promise<AgentConfigApi> {
  return jsonOrThrow(await fetchWithAuth(API_BASE, '/v1/agent/config'));
}

export type AgentIntegrationProvider = 'github' | 'gitlab';

export interface AgentIntegrationAccountApi {
  id: string;
  label: string;
  web_url?: string | null;
}

export interface AgentIntegrationRepositoryApi {
  id: string;
  name: string;
  web_url?: string | null;
}

export interface AgentIntegrationProviderApi {
  provider: AgentIntegrationProvider;
  configured: boolean;
  connected: boolean;
  connect_url: string | null;
  capabilities: string[];
  accounts: AgentIntegrationAccountApi[];
  repositories: AgentIntegrationRepositoryApi[];
  error?: string | null;
}

export interface AgentIntegrationsApi {
  providers: AgentIntegrationProviderApi[];
}

export async function getAgentIntegrations(): Promise<AgentIntegrationsApi> {
  const resp = await fetchWithAuth(API_BASE, '/v1/agent/integrations');
  return jsonOrThrow(resp);
}

export interface GitHubConnectionApi {
  connections: Array<{ installation_id: number; account_login: string | null }>;
  repos: string[];
}

/**
 * Complete the GitHub connection with the code GitHub handed the browser.
 *
 * The platform validates the one-time state, exchanges the code for a token
 * that speaks as this user, and asks GitHub which installations they can reach.
 */
export async function connectGitHub(code: string, state: string): Promise<GitHubConnectionApi> {
  const resp = await fetchWithAuth(API_BASE, '/v1/agent/integrations/github/connect', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ code, state }),
  });
  return jsonOrThrow(resp);
}

export async function connectGitLab(
  code: string,
  state: string,
): Promise<AgentIntegrationProviderApi> {
  const resp = await fetchWithAuth(API_BASE, '/v1/agent/integrations/gitlab/connect', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ code, state }),
  });
  return jsonOrThrow(resp);
}

export async function disconnectAgentIntegration(
  provider: AgentIntegrationProvider,
  connectionId: string,
): Promise<void> {
  const resp = await fetchWithAuth(
    API_BASE,
    `/v1/agent/integrations/${provider}/connections/${encodeURIComponent(connectionId)}`,
    { method: 'DELETE' },
  );
  if (!resp.ok) await jsonOrThrow(resp);
}

/**
 * Model ids this gateway serves — the model picker's options.
 *
 * Sourced from the agent config, not /v1/models: that list answers for the
 * browsing user, and on staging it offered 15 models of which 13 failed the
 * job's first call. The config's list is the create endpoint's own predicate.
 */
export async function listAgentModels(): Promise<string[]> {
  const cfg = await getAgentConfig();
  return cfg.models ?? [];
}

export interface RepoBranchesApi {
  default: string | null;
  branches: string[];
}

/** Branches of one entitled repository, for the composer's branch picker. */
export async function listRepoBranches(repo: string): Promise<RepoBranchesApi> {
  const resp = await fetchWithAuth(API_BASE, `/v1/agent/branches?repo=${encodeURIComponent(repo)}`);
  return jsonOrThrow(resp);
}

export interface CreateAgentJobRequest {
  repo: string;
  task_prompt: string;
  model: string;
  runtime?: string;
  /** A branch. The platform pins it to a commit at creation. */
  base_ref?: string;
  base_sha?: string;
  budget_usd?: number;
}

export async function listAgentJobs(limit = 50, archived = false): Promise<AgentJobApi[]> {
  const archivedQuery = archived ? '&archived=true' : '';
  const resp = await fetchWithAuth(API_BASE, `/v1/agent/jobs?limit=${limit}${archivedQuery}`);
  const body = await jsonOrThrow<{ jobs: AgentJobApi[] }>(resp);
  return body.jobs;
}

export interface AgentThreadArchiveApi {
  thread_id: string;
  archived: boolean;
  archived_at: string | null;
}

/** Hide the entire conversation containing this job from active task history. */
export async function archiveAgentJob(jobId: string): Promise<AgentThreadArchiveApi> {
  const resp = await fetchWithAuth(
    API_BASE,
    `/v1/agent/jobs/${encodeURIComponent(jobId)}/archive`,
    {
      method: 'POST',
    },
  );
  return jsonOrThrow(resp);
}

/** Return an archived conversation to active task history. */
export async function restoreAgentJob(jobId: string): Promise<AgentThreadArchiveApi> {
  const resp = await fetchWithAuth(
    API_BASE,
    `/v1/agent/jobs/${encodeURIComponent(jobId)}/archive`,
    {
      method: 'DELETE',
    },
  );
  return jsonOrThrow(resp);
}

export async function getAgentJob(jobId: string): Promise<AgentJobApi> {
  const resp = await fetchWithAuth(API_BASE, `/v1/agent/jobs/${encodeURIComponent(jobId)}`);
  return jsonOrThrow<AgentJobApi>(resp);
}

/** Load the conversation containing a job. Old standalone jobs return null. */
export async function getAgentJobThread(jobId: string): Promise<AgentThreadApi | null> {
  const resp = await fetchWithAuth(API_BASE, `/v1/agent/jobs/${encodeURIComponent(jobId)}/thread`);
  if (resp.status === 404) return null;
  return jsonOrThrow<AgentThreadApi>(resp);
}

export async function createAgentJob(body: CreateAgentJobRequest): Promise<AgentJobApi> {
  const resp = await fetchWithAuth(API_BASE, '/v1/agent/jobs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  return jsonOrThrow<AgentJobApi>(resp);
}

export async function cancelAgentJob(
  jobId: string,
): Promise<{ id: string; state: string; cancel_requested: boolean }> {
  const resp = await fetchWithAuth(API_BASE, `/v1/agent/jobs/${encodeURIComponent(jobId)}/cancel`, {
    method: 'POST',
  });
  return jsonOrThrow(resp);
}

export interface FollowUpAgentJobRequest {
  prompt: string;
  /** Omitted values inherit the parent run's harness and model. */
  runtime?: string;
  model?: string;
}

/** Queue another turn in the same thread, after its currently active run. */
export async function followUpAgentJob(
  jobId: string,
  body: FollowUpAgentJobRequest,
): Promise<AgentJobApi> {
  const resp = await fetchWithAuth(
    API_BASE,
    `/v1/agent/jobs/${encodeURIComponent(jobId)}/follow-ups`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    },
  );
  return jsonOrThrow<AgentJobApi>(resp);
}

/** Page through the event log. Used to backfill before the stream attaches. */
export async function listAgentJobEvents(
  jobId: string,
  after = 0,
  limit = 500,
): Promise<{ events: AgentJobEventApi[]; next_cursor: number }> {
  const resp = await fetchWithAuth(
    API_BASE,
    `/v1/agent/jobs/${encodeURIComponent(jobId)}/events?after=${after}&limit=${limit}`,
  );
  return jsonOrThrow(resp);
}

export async function getAgentJobArtifact(
  jobId: string,
  kind: string,
): Promise<{ content: string; attempt_id: number } | null> {
  const resp = await fetchWithAuth(
    API_BASE,
    `/v1/agent/jobs/${encodeURIComponent(jobId)}/artifacts/${encodeURIComponent(kind)}`,
  );
  // A job that changed nothing legitimately has no patch; that is not an error.
  if (resp.status === 404) return null;
  return jsonOrThrow(resp);
}

export interface StreamAgentJobOptions {
  onEvent: (event: AgentJobEventApi) => void;
  onFinished?: (info: { state: string; published_pr_url: string | null }) => void;
  onError?: (error: unknown) => void;
  /** Resume point; frames with an id at or below this are not re-sent. */
  lastEventId?: number;
  signal?: AbortSignal;
}

interface ParsedFrame {
  event: string;
  data: string;
  id?: number;
}

function parseFrame(raw: string): ParsedFrame | null {
  let event = 'message';
  let id: number | undefined;
  const dataLines: string[] = [];
  for (const line of raw.split(/\r?\n/)) {
    // Comment frame (the server's keepalive) — carries no data by design.
    if (line.startsWith(':')) continue;
    if (line.startsWith('event:')) event = line.slice(6).trim();
    else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim());
    else if (line.startsWith('id:')) {
      const parsed = Number(line.slice(3).trim());
      if (Number.isFinite(parsed)) id = parsed;
    }
  }
  if (dataLines.length === 0) return null;
  return { event, data: dataLines.join('\n'), id };
}

/**
 * Stream a job's events, reconnecting from the last id until it finishes.
 *
 * Reconnection is expected behaviour rather than error handling: the gateway
 * caps long-lived streams, so a job running longer than that cap will drop a
 * connection through no fault of anyone's. The cursor makes that invisible.
 */
export async function streamAgentJob(jobId: string, opts: StreamAgentJobOptions): Promise<void> {
  let cursor = opts.lastEventId ?? 0;
  let finished = false;

  while (!finished) {
    if (opts.signal?.aborted) return;

    let resp: Response;
    try {
      resp = await fetchWithAuth(
        API_BASE,
        `/v1/agent/jobs/${encodeURIComponent(jobId)}/stream?after=${cursor}`,
        { headers: { Accept: 'text/event-stream' }, signal: opts.signal },
      );
    } catch (err) {
      if (opts.signal?.aborted) return;
      opts.onError?.(err);
      return;
    }

    if (!resp.ok || !resp.body) {
      await jsonOrThrow(resp).catch((err) => opts.onError?.(err));
      return;
    }

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        const boundary = /\r?\n\r?\n/;
        let match: RegExpExecArray | null;
        while ((match = boundary.exec(buffer))) {
          const raw = buffer.slice(0, match.index);
          buffer = buffer.slice(match.index + match[0].length);
          const frame = parseFrame(raw);
          if (!frame) continue;

          if (frame.event === 'job_finished') {
            finished = true;
            try {
              opts.onFinished?.(JSON.parse(frame.data));
            } catch {
              opts.onFinished?.({ state: 'unknown', published_pr_url: null });
            }
            return;
          }

          try {
            const parsed = JSON.parse(frame.data) as AgentJobEventApi;
            // Track the cursor from the frame id so a reconnect resumes
            // exactly here, with no gap and no duplicate.
            cursor = frame.id ?? parsed.id ?? cursor;
            opts.onEvent(parsed);
          } catch {
            // A frame we cannot parse is dropped rather than aborting the
            // stream: losing one row beats losing the rest of the job.
          }
        }
      }
    } catch (err) {
      if (opts.signal?.aborted) return;
      // A mid-stream drop is the expected shape when the server's stream cap
      // fires. Reconnect from the cursor instead of surfacing an error.
      opts.onError?.(err);
    } finally {
      reader.releaseLock();
    }
  }
}
