import { describe, expect, it } from 'vitest';
import { AGENT_JOBS, getAgentJob } from './mock';

describe('agent job fixtures', () => {
  it('resolves jobs by id', () => {
    expect(getAgentJob('job-7f3a')?.state).toBe('running');
    expect(getAgentJob('nope')).toBeUndefined();
  });

  it('keeps output branches on the agent/<job-id> convention', () => {
    for (const job of AGENT_JOBS) {
      expect(job.branch).toBe(`agent/${job.id}`);
    }
  });

  it('never spends past the budget cap', () => {
    for (const job of AGENT_JOBS) {
      expect(job.spentUsd).toBeLessThanOrEqual(job.budgetUsd);
    }
  });

  it('marks at most one live attempt, always the last', () => {
    for (const job of AGENT_JOBS) {
      const live = job.attempts.filter((attempt) => attempt.status === 'live');
      expect(live.length).toBeLessThanOrEqual(1);
      if (live.length === 1) {
        expect(job.attempts[job.attempts.length - 1].status).toBe('live');
      }
    }
  });
});
