// Adapts the agent-jobs API shape to what the UI renders.
//
// The UI was designed against a richer mock than the API currently returns.
// This module makes that gap explicit rather than papering over it: fields the
// backend genuinely provides are mapped, and fields it does not are left empty
// so the UI renders an honest blank instead of an invented value.

import type { AgentJobApi, AgentJobEventApi } from '@/lib/api/agents';

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
  if (job.state === 'queued') return 'queued';
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

/** Map one stored event onto a UI row, or null when it has nothing to show. */
export function toDisplayEvent(event: AgentJobEventApi): AgentEvent | null {
  const payload = event.payload ?? {};
  const type = event.event_type;

  if (type === 'thinking') return { kind: 'thinking', text: asText(payload, 'text') };
  if (type === 'message') return { kind: 'message', text: asText(payload, 'text') };
  if (type === 'usage') return { kind: 'usage', text: asText(payload, 'text') };

  if (type === 'tool_use') {
    const name = asText(payload, 'name', 'tool') || 'Bash';
    // The UI's tool union is narrow; anything else renders as a Bash row so an
    // unfamiliar tool still appears rather than vanishing.
    const tool = name === 'Read' || name === 'Edit' ? name : 'Bash';
    return { kind: 'tool_use', tool, detail: asText(payload, 'detail', 'input', 'text') };
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

  // tool_result is folded into its tool_use row by design, and diff is
  // rendered from the patch artifact rather than as a stream row.
  if (type === 'tool_result' || type === 'diff') return null;

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

export interface AdaptOptions {
  events?: AgentJobEventApi[];
  patch?: string | null;
}

/** Build the UI job model from the API job plus whatever else we have. */
export function toDisplayJob(job: AgentJobApi, options: AdaptOptions = {}): AgentJob {
  const events = options.events ?? [];
  const patch = options.patch ?? '';
  const displayEvents = events
    .map(toDisplayEvent)
    .filter((event): event is AgentEvent => event !== null);

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

  const egressDenials = displayEvents.filter((event) => event.kind === 'egress_denied').length;

  return {
    id: job.id,
    title: job.task_prompt.split('\n')[0].slice(0, 80),
    state: toDisplayState(job),
    stateNote: job.detail ?? undefined,
    repo: job.repo,
    baseSha: (job.base_sha ?? '').slice(0, 7),
    // The publisher only ever writes this one branch, so the UI can state it
    // rather than wait to be told.
    branch: `agent/${job.id}`,
    runtime: job.runtime,
    model: job.model,
    // Spend is summed server-side from api_logs; until the job exposes it the
    // UI shows 0 rather than guessing from token counts.
    spentUsd: 0,
    budgetUsd: job.budget_usd ?? 0,
    timeoutLabel: '',
    networkSetup: '',
    networkAgent: '',
    sandbox: '',
    attempts,
    events: displayEvents,
    eventCount: events.length,
    diffFiles: toDiffFiles(patch),
    diffStat: diffStat(patch),
    diffLines: toDiffLines(patch),
    rawLines: [],
    gates: [],
    usage: { tokensIn: '', tokensOut: '', cachePct: 0, turns: 0 },
    egressDenials,
    prLabel: job.published_pr_url ? `draft PR ${job.published_pr_url.split('/').pop()}` : undefined,
  };
}
