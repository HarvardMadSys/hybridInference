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

/** Model ids this gateway serves — the model picker's options. */
export async function listAgentModels(): Promise<string[]> {
  const resp = await fetchWithAuth(API_BASE, '/v1/models');
  const body = await jsonOrThrow<{ data?: Array<{ id: string }> }>(resp);
  return (body.data ?? []).map((entry) => entry.id).filter(Boolean);
}

export interface CreateAgentJobRequest {
  repo: string;
  task_prompt: string;
  model: string;
  runtime?: string;
  base_sha?: string;
  budget_usd?: number;
}

export async function listAgentJobs(limit = 50): Promise<AgentJobApi[]> {
  const resp = await fetchWithAuth(API_BASE, `/v1/agent/jobs?limit=${limit}`);
  const body = await jsonOrThrow<{ jobs: AgentJobApi[] }>(resp);
  return body.jobs;
}

export async function getAgentJob(jobId: string): Promise<AgentJobApi> {
  const resp = await fetchWithAuth(API_BASE, `/v1/agent/jobs/${encodeURIComponent(jobId)}`);
  return jsonOrThrow<AgentJobApi>(resp);
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
