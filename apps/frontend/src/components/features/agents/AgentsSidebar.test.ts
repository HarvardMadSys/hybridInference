import { describe, expect, it } from 'vitest';

import type { AgentJob } from './types';
import { groupJobsByConversation, groupJobsByProject } from './AgentsSidebar';

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
    const localTime = (hour: number, minute = 0) =>
      new Date(2026, 6, 29, hour, minute).toISOString();
    const sections = groupJobsByConversation(
      [
        job({
          id: 'turn-2',
          threadId: 'thread-1',
          turnNo: 2,
          title: 'Follow-up',
          state: 'running',
          createdAt: localTime(10, 5),
        }),
        job({
          id: 'turn-1',
          threadId: 'thread-1',
          turnNo: 1,
          title: 'Explain this repository',
          createdAt: localTime(10),
        }),
      ],
      new Date(2026, 6, 29, 12),
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
    const localTime = (year: number, month: number, day: number, hour = 1) =>
      new Date(year, month, day, hour).toISOString();
    const sections = groupJobsByConversation(
      [
        job({ id: 'today', createdAt: localTime(2026, 6, 29) }),
        job({ id: 'week', createdAt: localTime(2026, 6, 25) }),
        job({ id: 'old', createdAt: localTime(2026, 5, 1) }),
      ],
      new Date(2026, 6, 29, 12),
    );

    expect(sections.map((section) => section.label)).toEqual(['Today', 'Previous 7 days', 'Older']);
  });

  it('orders pinned conversations by pin time ahead of newer unpinned work', () => {
    const rows = groupJobsByConversation(
      [
        job({ id: 'new', createdAt: '2026-07-29T11:00:00Z' }),
        job({
          id: 'pin-old',
          createdAt: '2026-07-01T11:00:00Z',
          pinnedAt: '2026-07-29T10:00:00Z',
        }),
        job({
          id: 'pin-new',
          createdAt: '2026-06-01T11:00:00Z',
          pinnedAt: '2026-07-29T12:00:00Z',
        }),
      ],
      new Date('2026-07-29T13:00:00Z'),
    ).flatMap((section) => section.conversations);

    expect(rows.map((row) => row.job.id)).toEqual(['pin-new', 'pin-old', 'new']);
  });
});

describe('groupJobsByProject', () => {
  const at = (day: number, hour = 1) => new Date(2026, 6, day, hour).toISOString();

  it('orders projects by their most recent conversation and labels them by repo name', () => {
    const sections = groupJobsByProject([
      job({ id: 'a1', threadId: 'a', repo: 'murphy/awesome-mlsys', createdAt: at(20) }),
      job({ id: 'h1', threadId: 'h', repo: 'murphy/hybridInference', createdAt: at(29) }),
      job({ id: 'a2', threadId: 'a2', repo: 'murphy/awesome-mlsys', createdAt: at(24) }),
    ]);

    expect(sections.map((section) => section.label)).toEqual(['hybridInference', 'awesome-mlsys']);
    expect(sections[0].repo).toBe('murphy/hybridInference');
    expect(sections[1].conversations.map((row) => row.job.id)).toEqual(['a2', 'a1']);
  });

  it('keeps every turn of a conversation in one project folder', () => {
    const sections = groupJobsByProject([
      job({ id: 'turn-2', threadId: 't', turnNo: 2, title: 'Follow-up', createdAt: at(29, 10) }),
      job({ id: 'turn-1', threadId: 't', turnNo: 1, title: 'First ask', createdAt: at(29, 9) }),
    ]);

    expect(sections).toHaveLength(1);
    expect(sections[0].conversations).toHaveLength(1);
    expect(sections[0].conversations[0]).toMatchObject({
      job: { id: 'turn-2', title: 'First ask' },
      jobIds: ['turn-1', 'turn-2'],
    });
  });

  it('puts projects containing pinned conversations first', () => {
    const sections = groupJobsByProject([
      job({ id: 'new', repo: 'owner/new', createdAt: at(29) }),
      job({
        id: 'old-pinned',
        repo: 'owner/old',
        createdAt: at(1),
        pinnedAt: at(30),
      }),
    ]);

    expect(sections.map((section) => section.repo)).toEqual(['owner/old', 'owner/new']);
  });
});
