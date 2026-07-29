// Adapts the agent-jobs API shape to what the UI renders.
//
// The UI was designed against a richer mock than the API currently returns.
// This module makes that gap explicit rather than papering over it: fields the
// backend genuinely provides are mapped, and fields it does not are left empty
// so the UI renders an honest blank instead of an invented value.

import type { AgentJobApi, AgentJobEventApi, AgentThreadApi } from '@/lib/api/agents';

import type { AgentEvent, AgentJob, AgentJobState, DiffLine } from './types';

/**
 * Map a server state onto a display state.
 *
 * `succeeded` splits in two, because those are different things to a user: a
 * job whose patch is already a draft PR is done, while one that finished
 * without a PR is waiting on the publish step (or produced no changes) and
 * still wants a human.
 */
export function toDisplayState(job: AgentJobApi): AgentJobState {
  if (job.state === 'waiting' || job.state === 'queued') return 'queued';
  if (job.state === 'running' || job.state === 'publishing') return 'running';
  // `succeeded` splits in two because those are different things to a user: a
  // job whose patch is already a draft PR is done, while one that finished
  // without a PR is waiting on the publish step (or produced no changes).
  if (job.state === 'succeeded') return job.published_pr_url ? 'done' : 'needs_review';
  if (job.state === 'failed') return 'failed';
  if (job.state === 'cancelled') return 'cancelled';
  return 'queued';
}

function asText(payload: Record<string, unknown> | null, ...keys: string[]): string {
  for (const key of keys) {
    const value = payload?.[key];
    if (typeof value === 'string' && value) return value;
  }
  return '';
}

function readableValue(value: unknown): string {
  if (typeof value === 'string') return value;
  if (value === null || value === undefined) return '';
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

/** Map one stored event onto a UI row, or null when it has nothing to show. */
export function toDisplayEvent(event: AgentJobEventApi): AgentEvent | null {
  const payload = event.payload ?? {};
  const type = event.event_type;

  // Never carry chain-of-thought into the display model. The UI represents
  // this as a compact reasoning status, not as model-authored hidden text.
  if (type === 'thinking') return { kind: 'thinking', text: '' };
  if (type === 'message') return { kind: 'message', text: asText(payload, 'text') };
  if (type === 'usage') return { kind: 'usage', text: asText(payload, 'text') };

  if (type === 'tool_use') {
    const name = asText(payload, 'name', 'tool') || 'Bash';
    return {
      kind: 'tool_use',
      tool: name,
      id: asText(payload, 'id') || undefined,
      detail: asText(payload, 'detail', 'text') || readableValue(payload.input) || 'Running tool',
    };
  }

  if (type === 'tool_result') {
    return {
      kind: 'tool_result',
      text: readableValue(payload.content) || asText(payload, 'text', 'detail'),
      toolUseId: asText(payload, 'tool_use_id') || undefined,
      isError: payload.is_error === true,
    };
  }

  if (type === 'error') {
    // Egress denials arrive as an error subtype but are a first-class row:
    // "the sandbox tried to reach X" is a different story from "it failed".
    const host = asText(payload, 'host');
    if (host) {
      const attempts = typeof payload.attempts === 'number' ? payload.attempts : 1;
      return { kind: 'egress_denied', host, attempts };
    }
    return { kind: 'lifecycle', text: asText(payload, 'detail', 'text') || 'error' };
  }

  if (type === 'attempt_superseded') {
    const reason = asText(payload, 'reason') || 'lease expired';
    return { kind: 'lifecycle', text: `attempt superseded (${reason})` };
  }
  if (type === 'lifecycle') {
    return { kind: 'lifecycle', text: asText(payload, 'phase', 'text') || 'lifecycle' };
  }

  // Diff is rendered from the complete patch artifact rather than a stream
  // marker that only says whether an artifact was stored.
  if (type === 'diff') return null;

  // An unrecognized kind still appears, rather than silently disappearing.
  return { kind: 'lifecycle', text: type };
}

/** Split a unified diff into the marker-tagged lines the diff view renders. */
export function toDiffLines(patch: string): DiffLine[] {
  if (!patch) return [];
  return patch
    .split('\n')
    .filter((line) => !line.startsWith('diff --git') && !line.startsWith('index '))
    .map((text): DiffLine => {
      if (text.startsWith('@@')) return { marker: 'hunk', text };
      if (text.startsWith('+++') || text.startsWith('---')) return { marker: 'ctx', text };
      if (text.startsWith('+')) return { marker: 'add', text };
      if (text.startsWith('-')) return { marker: 'del', text };
      return { marker: 'ctx', text };
    });
}

/** Extract the file list a patch touches, for the diff header. */
export function toDiffFiles(patch: string): string[] {
  const files = new Set<string>();
  for (const line of patch.split('\n')) {
    const match = /^diff --git a\/(.+?) b\/(.+)$/.exec(line);
    if (match) files.add(match[2]);
  }
  return [...files];
}

function diffStat(patch: string): { add: number; del: number } | undefined {
  if (!patch) return undefined;
  let add = 0;
  let del = 0;
  for (const line of patch.split('\n')) {
    if (line.startsWith('+') && !line.startsWith('+++')) add += 1;
    else if (line.startsWith('-') && !line.startsWith('---')) del += 1;
  }
  return { add, del };
}

const TIER_LABELS: Record<string, string> = {
  platform_only: 'gateway only',
  trusted: 'allowlist',
  custom: 'custom',
  full: 'open',
};

/** Render an egress tier for a reader who does not know the tier names. */
function tierLabel(tier: string | null): string {
  if (!tier) return '';
  return TIER_LABELS[tier] ?? tier;
}

/** Render a token count compactly, or empty when there is no ledger. */
function formatTokens(value: number | null): string {
  if (value === null || value === undefined) return '';
  if (value >= 1000) return `${(value / 1000).toFixed(1)}k`;
  return String(value);
}

function rawEventLine(event: AgentJobEventApi): string {
  if (event.event_type !== 'thinking') return JSON.stringify(event);
  return JSON.stringify({
    ...event,
    payload: { ...(event.payload ?? {}), text: '[reasoning hidden]' },
  });
}

export interface AdaptOptions {
  events?: AgentJobEventApi[];
  patch?: string | null;
  thread?: AgentThreadApi | null;
}

/** Preserve attempt boundaries and attach each result to its tool activity. */
function displayEvents(events: AgentJobEventApi[]): AgentEvent[] {
  const attemptIds = [...new Set(events.map((event) => event.attempt_id))].sort((a, b) => a - b);
  const attemptNoById = new Map(attemptIds.map((id, index) => [id, index + 1]));
  const rows: AgentEvent[] = [];
  const toolsById = new Map<string, number>();
  const lastToolByAttempt = new Map<number, number>();

  for (const event of events) {
    const mapped = toDisplayEvent(event);
    if (!mapped) continue;
    const attemptNo = attemptNoById.get(event.attempt_id);
    const row = { ...mapped, attemptNo } as AgentEvent;

    if (row.kind === 'tool_use') {
      const rowIndex = rows.push(row) - 1;
      if (row.id) toolsById.set(`${attemptNo}:${row.id}`, rowIndex);
      if (attemptNo !== undefined) lastToolByAttempt.set(attemptNo, rowIndex);
      continue;
    }

    if (row.kind === 'tool_result') {
      const explicit = row.toolUseId ? toolsById.get(`${attemptNo}:${row.toolUseId}`) : undefined;
      const targetIndex =
        explicit ?? (attemptNo === undefined ? undefined : lastToolByAttempt.get(attemptNo));
      const target = targetIndex === undefined ? undefined : rows[targetIndex];
      if (targetIndex !== undefined && target?.kind === 'tool_use' && !target.output) {
        rows[targetIndex] = {
          ...target,
          output: row.text ? row.text.split('\n') : ['Command completed with no output.'],
          outputIsError: row.isError,
        };
        continue;
      }
    }

    rows.push(row);
  }

  return rows;
}

/** Build the UI job model from the API job plus whatever else we have. */
export function toDisplayJob(job: AgentJobApi, options: AdaptOptions = {}): AgentJob {
  const events = options.events ?? [];
  const patch = options.patch ?? '';
  const renderedEvents = displayEvents(events);

  // Attempts are inferred from the event log rather than fetched: an attempt
  // exists precisely because it wrote events, and a superseded control event
  // is what marks the takeover.
  const attemptIds = [...new Set(events.map((event) => event.attempt_id))].sort((a, b) => a - b);
  const supersededIds = new Set(
    events
      .filter((event) => event.event_type === 'attempt_superseded')
      .map((event) => event.attempt_id),
  );
  const attempts = attemptIds.map((id, index) => ({
    no: index + 1,
    status: supersededIds.has(id)
      ? ('superseded' as const)
      : job.state === 'running' || job.state === 'publishing'
        ? ('live' as const)
        : ('finished' as const),
    note: supersededIds.has(id) ? 'lease expired; a new attempt took over' : undefined,
  }));

  const egressDenials = renderedEvents.filter((event) => event.kind === 'egress_denied').length;
  const currentTurn =
    job.turn_no ?? options.thread?.jobs.find((threadJob) => threadJob.id === job.id)?.turn_no;
  let priorJobIds: Set<string> | null = null;
  if (currentTurn !== undefined && options.thread?.jobs.length) {
    priorJobIds = new Set(
      options.thread.jobs
        .filter((threadJob) => (threadJob.turn_no ?? 1) < currentTurn)
        .map((threadJob) => threadJob.id),
    );
  }
  const priorMessages = (options.thread?.messages ?? [])
    .filter((message) =>
      priorJobIds ? priorJobIds.has(message.job_id) : message.job_id !== job.id,
    )
    .map((message) => ({
      id: message.id,
      role: message.role,
      content: message.content,
      jobId: message.job_id,
      createdAt: message.created_at,
    }));
  const threadPrUrl = [...(options.thread?.jobs ?? [])]
    .reverse()
    .find((threadJob) => threadJob.published_pr_url)?.published_pr_url;
  const publishedPrUrl = job.published_pr_url ?? threadPrUrl ?? null;

  return {
    id: job.id,
    createdAt: job.created_at,
    title: (options.thread?.title ?? job.task_prompt.split('\n')[0]).slice(0, 80),
    prompt: job.task_prompt,
    state: toDisplayState(job),
    stateNote: job.detail ?? (job.state === 'waiting' ? 'Queued after the current run' : undefined),
    repo: job.repo,
    baseSha: (job.base_sha ?? '').slice(0, 7),
    // Every turn in a conversation publishes to the same branch/PR.
    branch: `agent/${job.thread_id ?? options.thread?.thread_id ?? job.id}`,
    runtime: job.runtime,
    model: job.model,
    // Summed server-side from api_logs. Null there means no ledger is
    // configured; 0 is the honest display for that, but the panel below reads
    // `hasLedger` so it can say so rather than imply the job was free.
    spentUsd: job.spent_usd ?? 0,
    budgetUsd: job.budget_usd ?? 0,
    hasLedger: job.spent_usd !== null,
    timeoutLabel: '',
    networkSetup: tierLabel(job.setup_egress_tier),
    networkAgent: tierLabel(job.agent_egress_tier),
    sandbox: '',
    attempts,
    events: renderedEvents,
    eventCount: events.length,
    diffFiles: toDiffFiles(patch),
    diffStat: diffStat(patch),
    diffLines: toDiffLines(patch),
    rawLines: events.map(rawEventLine),
    gates: [],
    usage: {
      tokensIn: formatTokens(job.tokens_in),
      tokensOut: formatTokens(job.tokens_out),
      cachePct: 0,
      turns: job.model_calls ?? 0,
    },
    egressDenials,
    prLabel: publishedPrUrl ? `draft PR ${publishedPrUrl.split('/').pop()}` : undefined,
    prUrl: publishedPrUrl ?? undefined,
    threadId: job.thread_id ?? options.thread?.thread_id ?? undefined,
    parentJobId: job.parent_job_id ?? undefined,
    turnNo: job.turn_no,
    threadMessages: priorMessages,
  };
}
