import { describe, expect, it } from 'vitest';

import type { AgentJob } from './types';
import { groupJobsByConversation } from './AgentsSidebar';

function job(overrides: Partial<AgentJob>): AgentJob {
  return {
    id: 'job-1',
    title: 'Initial request',
    state: 'done',
    repo: 'owner/repo',
    baseSha: 'abcdef1',
    branch: 'agent/thread-1',
    runtime: 'claude-code',
    model: 'model',
    spentUsd: 0,
    budgetUsd: 1,
    timeoutLabel: '',
    networkSetup: '',
    networkAgent: '',
    sandbox: '',
    attempts: [],
    events: [],
    eventCount: 0,
    diffFiles: [],
    diffLines: [],
    rawLines: [],
    gates: [],
    usage: { tokensIn: '', tokensOut: '', cachePct: 0, turns: 0 },
    egressDenials: 0,
    ...overrides,
  };
}

describe('groupJobsByConversation', () => {
  it('shows one row per thread, with the first prompt and latest run state', () => {
    const sections = groupJobsByConversation(
      [
        job({
          id: 'turn-2',
          threadId: 'thread-1',
          turnNo: 2,
          title: 'Follow-up',
          state: 'running',
          createdAt: '2026-07-29T10:05:00Z',
        }),
        job({
          id: 'turn-1',
          threadId: 'thread-1',
          turnNo: 1,
          title: 'Explain this repository',
          createdAt: '2026-07-29T10:00:00Z',
        }),
      ],
      new Date('2026-07-29T12:00:00Z'),
    );

    expect(sections).toHaveLength(1);
    expect(sections[0].label).toBe('Today');
    expect(sections[0].conversations).toHaveLength(1);
    expect(sections[0].conversations[0]).toMatchObject({
      job: { id: 'turn-2', title: 'Explain this repository', state: 'running' },
      jobIds: ['turn-1', 'turn-2'],
    });
  });

  it('separates recent and older conversations', () => {
    const sections = groupJobsByConversation(
      [
        job({ id: 'today', createdAt: '2026-07-29T01:00:00Z' }),
        job({ id: 'week', createdAt: '2026-07-25T01:00:00Z' }),
        job({ id: 'old', createdAt: '2026-06-01T01:00:00Z' }),
      ],
      new Date('2026-07-29T12:00:00Z'),
    );

    expect(sections.map((section) => section.label)).toEqual(['Today', 'Previous 7 days', 'Older']);
  });
});
