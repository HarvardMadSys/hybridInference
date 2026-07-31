import { describe, expect, it } from 'vitest';

import type { AgentJobApi, AgentJobEventApi } from '@/lib/api/agents';

import { applyAgentLifecycleEvent } from './useAgentJobs';

const JOB = {
  state: 'queued',
  current_attempt_id: 10,
} as AgentJobApi;

function lifecycle(attemptId: number, phase: string): AgentJobEventApi {
  return {
    id: attemptId,
    attempt_id: attemptId,
    seq: 1,
    event_type: 'lifecycle',
    payload: { phase },
    created_at: null,
  };
}

describe('applyAgentLifecycleEvent', () => {
  it('moves the cached job to the replacement attempt before workspace readiness', () => {
    const started = applyAgentLifecycleEvent(JOB, lifecycle(11, 'started'));

    expect(started.state).toBe('running');
    expect(started.current_attempt_id).toBe(11);

    const ready = applyAgentLifecycleEvent(started, lifecycle(11, 'workspace_ready'));
    expect(ready.current_attempt_id).toBe(11);

    const finalizing = applyAgentLifecycleEvent(ready, lifecycle(11, 'workspace_finalizing'));
    expect(finalizing.state).toBe('running');
    expect(finalizing.current_attempt_id).toBe(11);
  });
});
