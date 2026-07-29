// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { cancelAgentJob, followUpAgentJob } from '@/lib/api/agents';

import { JobDetail } from './JobDetail';
import type { AgentJob } from './types';

const navigation = vi.hoisted(() => ({ push: vi.fn() }));

vi.mock('next/navigation', () => ({
  useRouter: () => navigation,
}));

vi.mock('@/lib/api/agents', () => ({
  cancelAgentJob: vi.fn(),
  followUpAgentJob: vi.fn(),
}));

function makeJob(overrides: Partial<AgentJob> = {}): AgentJob {
  return {
    id: 'ajob_1',
    title: 'Current task',
    prompt: 'Current task\nwith all of its detail.',
    state: 'running',
    repo: 'owner/repository',
    baseSha: 'abcdef1',
    branch: 'agent/ajob_1',
    runtime: 'claude-code',
    model: 'qwen-test',
    spentUsd: 0.1,
    hasLedger: true,
    budgetUsd: 2,
    timeoutLabel: '',
    networkSetup: 'allowlist',
    networkAgent: 'gateway only',
    sandbox: 'container',
    attempts: [{ no: 1, status: 'live' }],
    events: [
      { kind: 'lifecycle', text: 'started', attemptNo: 1 },
      {
        kind: 'tool_use',
        tool: 'Bash',
        detail: 'pytest -q',
        output: ['2 passed'],
        attemptNo: 1,
      },
      { kind: 'message', text: 'I found the **cause**.', attemptNo: 1 },
    ],
    eventCount: 3,
    diffFiles: ['src/example.ts'],
    diffStat: { add: 1, del: 0 },
    diffLines: [{ marker: 'add', text: '+fixed' }],
    rawLines: ['{"event_type":"message"}'],
    gates: [],
    usage: { tokensIn: '1.2k', tokensOut: '80', cachePct: 0, turns: 2 },
    egressDenials: 0,
    ...overrides,
  };
}

describe('JobDetail', () => {
  beforeEach(() => {
    navigation.push.mockReset();
    vi.mocked(cancelAgentJob).mockReset();
    vi.mocked(followUpAgentJob).mockReset();
  });

  afterEach(() => cleanup());

  it('renders the conversation first, including history and the full current prompt', () => {
    render(
      <JobDetail
        job={makeJob({
          threadMessages: [
            {
              id: 1,
              role: 'user',
              content: 'Earlier question',
              jobId: 'ajob_0',
              createdAt: null,
            },
            {
              id: 2,
              role: 'assistant',
              content: 'Earlier answer',
              jobId: 'ajob_0',
              createdAt: null,
            },
          ],
        })}
      />,
    );

    expect(screen.getByText('Earlier question')).toBeInTheDocument();
    expect(screen.getByText('Earlier answer')).toBeInTheDocument();
    expect(screen.getByText(/with all of its detail/)).toBeInTheDocument();
    expect(screen.getByText('Setting up environment')).toBeInTheDocument();
    expect(screen.getByText('I found the', { exact: false })).toBeInTheDocument();
  });

  it('shows reasoning as a status without rendering raw thinking text', () => {
    render(
      <JobDetail
        job={makeJob({
          events: [{ kind: 'thinking', text: 'private chain of thought', attemptNo: 1 }],
        })}
      />,
    );

    expect(screen.getByLabelText('Agent reasoning')).toHaveTextContent('Agent is reasoning');
    expect(screen.queryByText('private chain of thought')).not.toBeInTheDocument();
  });

  it('keeps tool activity collapsed with its result available on demand', () => {
    render(<JobDetail job={makeJob()} />);

    const activity = screen.getByText('Bash').closest('details');
    expect(activity).not.toBeNull();
    expect(activity).not.toHaveAttribute('open');
    expect(activity).toHaveTextContent('2 passed');

    fireEvent.click(screen.getByText('Bash'));
    expect(activity).toHaveAttribute('open');
  });

  it('uses the real cancel endpoint and refreshes the job', async () => {
    const reload = vi.fn();
    vi.mocked(cancelAgentJob).mockResolvedValue({
      id: 'ajob_1',
      state: 'running',
      cancel_requested: true,
    });
    render(<JobDetail job={makeJob()} onReload={reload} />);

    fireEvent.click(screen.getByRole('button', { name: 'Stop' }));

    await waitFor(() => expect(cancelAgentJob).toHaveBeenCalledWith('ajob_1'));
    expect(reload).toHaveBeenCalledOnce();
  });

  it('queues a follow-up even while the current run is active', async () => {
    vi.mocked(followUpAgentJob).mockResolvedValue({ id: 'ajob_child' } as never);
    render(<JobDetail job={makeJob()} />);

    expect(screen.getByText(/queued after this run/i)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Add a follow-up'), {
      target: { value: 'Now add a regression test' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Send follow-up' }));

    await waitFor(() =>
      expect(followUpAgentJob).toHaveBeenCalledWith('ajob_1', {
        prompt: 'Now add a regression test',
      }),
    );
    expect(navigation.push).toHaveBeenCalledWith('/agents/ajob_child');
  });

  it('moves diff, raw events, usage, and sandbox data into a secondary drawer', () => {
    render(<JobDetail job={makeJob()} />);

    expect(screen.queryByText('Input tokens')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Details' }));
    expect(screen.getByRole('dialog', { name: 'Run details' })).toBeInTheDocument();
    expect(screen.getByText('Input tokens')).toBeInTheDocument();
    expect(screen.getByText('Sandbox')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /Raw events/ }));
    expect(screen.getByText('{"event_type":"message"}')).toBeInTheDocument();
  });
});
