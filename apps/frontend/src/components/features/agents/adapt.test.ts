// Tests for the API → UI adapter.
//
// The cases that matter are the ones where a wrong mapping would mislead a
// user about their own job: a finished-but-unpublished job looking done, a
// superseded attempt looking live, or an event kind silently vanishing.

import { describe, expect, it } from 'vitest';

import type { AgentJobApi, AgentJobEventApi } from '@/lib/api/agents';

import { toDiffFiles, toDiffLines, toDisplayEvent, toDisplayJob, toDisplayState } from './adapt';

const JOB: AgentJobApi = {
  id: 'ajob_1',
  repo: 'o/n',
  task_prompt: 'fix the flaky test\nmore detail',
  runtime: 'claude-code',
  model: 'glm-5.1',
  base_sha: 'abcdef1234567890',
  state: 'running',
  cancel_requested: false,
  current_attempt_id: 1,
  published_pr_url: null,
  detail: null,
  budget_usd: 5,
  metadata: null,
  created_at: null,
  updated_at: null,
  spent_usd: null,
  tokens_in: null,
  tokens_out: null,
  model_calls: null,
  setup_egress_tier: null,
  agent_egress_tier: null,
};

function event(
  id: number,
  event_type: string,
  payload: Record<string, unknown> = {},
  attempt_id = 1,
): AgentJobEventApi {
  return { id, attempt_id, seq: id, event_type, payload, created_at: null };
}

describe('toDisplayState', () => {
  it('separates published from merely finished', () => {
    // Both are `succeeded` on the wire, but only one is done to a user.
    expect(toDisplayState({ ...JOB, state: 'succeeded', published_pr_url: 'u' })).toBe('done');
    expect(toDisplayState({ ...JOB, state: 'succeeded', published_pr_url: null })).toBe(
      'needs_review',
    );
  });

  it('shows publishing as still running', () => {
    expect(toDisplayState({ ...JOB, state: 'publishing' })).toBe('running');
  });

  it.each([
    ['queued', 'queued'],
    ['running', 'running'],
    ['failed', 'failed'],
    ['cancelled', 'cancelled'],
  ] as const)('maps %s', (wire, display) => {
    expect(toDisplayState({ ...JOB, state: wire })).toBe(display);
  });
});

describe('toDisplayEvent', () => {
  it('promotes an egress denial out of the generic error kind', () => {
    // "the sandbox tried to reach X" is a different story from "it failed".
    expect(toDisplayEvent(event(1, 'error', { host: 'evil.example', attempts: 3 }))).toEqual({
      kind: 'egress_denied',
      host: 'evil.example',
      attempts: 3,
    });
  });

  it('keeps a plain error as a lifecycle row', () => {
    expect(toDisplayEvent(event(1, 'error', { detail: 'boom' }))).toEqual({
      kind: 'lifecycle',
      text: 'boom',
    });
  });

  it('renders a supersede with its reason', () => {
    const row = toDisplayEvent(event(1, 'attempt_superseded', { reason: 'lease_expired' }));
    expect(row).toEqual({ kind: 'lifecycle', text: 'attempt superseded (lease_expired)' });
  });

  it('falls back to a Bash row for an unfamiliar tool', () => {
    // An unknown tool must still appear rather than disappear from the log.
    expect(toDisplayEvent(event(1, 'tool_use', { name: 'WebFetch', detail: 'x' }))).toEqual({
      kind: 'tool_use',
      tool: 'Bash',
      detail: 'x',
    });
  });

  it('hides the rows the UI folds elsewhere', () => {
    expect(toDisplayEvent(event(1, 'tool_result'))).toBeNull();
    expect(toDisplayEvent(event(1, 'diff'))).toBeNull();
  });

  it('surfaces an unknown event kind instead of dropping it', () => {
    expect(toDisplayEvent(event(1, 'something_new'))).toEqual({
      kind: 'lifecycle',
      text: 'something_new',
    });
  });
});

describe('toDisplayJob', () => {
  it('states the branch the publisher will use', () => {
    expect(toDisplayJob(JOB).branch).toBe('agent/ajob_1');
  });

  it('marks a superseded attempt rather than showing it live', () => {
    const events = [
      event(1, 'message', {}, 10),
      event(2, 'attempt_superseded', { reason: 'lease_expired' }, 10),
      event(3, 'message', {}, 11),
    ];
    const job = toDisplayJob(JOB, { events });
    expect(job.attempts).toHaveLength(2);
    expect(job.attempts[0].status).toBe('superseded');
    expect(job.attempts[0].note).toContain('lease expired');
    expect(job.attempts[1].status).toBe('live');
  });

  it('counts every stored event, including the ones not rendered as rows', () => {
    const events = [event(1, 'message'), event(2, 'tool_result')];
    const job = toDisplayJob(JOB, { events });
    expect(job.eventCount).toBe(2);
    expect(job.events).toHaveLength(1);
  });

  it('leaves unknown fields empty rather than inventing them', () => {
    // The API does not report these yet; a fabricated value would be worse
    // than a blank one.
    const job = toDisplayJob(JOB);
    expect(job.gates).toEqual([]);
    expect(job.rawLines).toEqual([]);
    expect(job.spentUsd).toBe(0);
  });

  it('carries the budget and the PR label through', () => {
    const job = toDisplayJob({
      ...JOB,
      state: 'succeeded',
      published_pr_url: 'https://github.com/o/n/pull/44',
    });
    expect(job.budgetUsd).toBe(5);
    expect(job.prLabel).toBe('draft PR 44');
  });
});

describe('patch parsing', () => {
  const PATCH = [
    'diff --git a/src/x.py b/src/x.py',
    'index 111..222 100644',
    '--- a/src/x.py',
    '+++ b/src/x.py',
    '@@ -1,3 +1,3 @@',
    ' context',
    '-old',
    '+new',
  ].join('\n');

  it('lists changed files', () => {
    expect(toDiffFiles(PATCH)).toEqual(['src/x.py']);
  });

  it('tags lines by marker and drops git noise', () => {
    const lines = toDiffLines(PATCH);
    expect(lines.some((l) => l.text.startsWith('diff --git'))).toBe(false);
    expect(lines.find((l) => l.text === '+new')?.marker).toBe('add');
    expect(lines.find((l) => l.text === '-old')?.marker).toBe('del');
    expect(lines.find((l) => l.text.startsWith('@@'))?.marker).toBe('hunk');
    // File headers are context, not additions — otherwise every diff reads +1.
    expect(lines.find((l) => l.text.startsWith('+++'))?.marker).toBe('ctx');
  });

  it('counts added and removed lines without the headers', () => {
    expect(toDisplayJob(JOB, { patch: PATCH }).diffStat).toEqual({ add: 1, del: 1 });
  });

  it('handles a job that produced no patch', () => {
    const job = toDisplayJob(JOB, { patch: null });
    expect(job.diffFiles).toEqual([]);
    expect(job.diffLines).toEqual([]);
    expect(job.diffStat).toBeUndefined();
  });
});

describe('ledger-sourced fields', () => {
  it('reports spend and usage from the API rather than inventing them', () => {
    const job = toDisplayJob({
      ...JOB,
      spent_usd: 0.0075,
      tokens_in: 74523,
      tokens_out: 512,
      model_calls: 3,
      agent_egress_tier: 'platform_only',
    });

    expect(job.spentUsd).toBe(0.0075);
    expect(job.hasLedger).toBe(true);
    expect(job.usage.tokensIn).toBe('74.5k');
    expect(job.usage.turns).toBe(3);
    // The tier is rendered for a reader who does not know the tier names.
    expect(job.networkAgent).toBe('gateway only');
  });

  it('distinguishes "no ledger" from "spent nothing"', () => {
    // A deployment with no billing ledger must not be shown as a free job.
    const job = toDisplayJob(JOB);

    expect(job.spentUsd).toBe(0);
    expect(job.hasLedger).toBe(false);
    expect(job.usage.tokensIn).toBe('');
  });
});
