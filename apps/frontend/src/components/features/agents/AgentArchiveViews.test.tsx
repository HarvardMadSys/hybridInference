// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AgentJob } from './types';

const mocks = vi.hoisted(() => ({
  archive: vi.fn(),
  restore: vi.fn(),
  reload: vi.fn(),
  reloadProjects: vi.fn(),
  loadRepo: vi.fn(),
  replace: vi.fn(),
  useAgentJobList: vi.fn(),
  useAgentProjects: vi.fn(),
  useProjectJobs: vi.fn(),
}));

vi.mock('next/navigation', () => ({
  usePathname: () => '/agents/job-1',
  useRouter: () => ({ replace: mocks.replace }),
}));

vi.mock('next/link', () => ({
  default: ({ href, children, ...props }: React.ComponentProps<'a'>) => (
    <a href={typeof href === 'string' ? href : ''} {...props}>
      {children}
    </a>
  ),
}));

vi.mock('@/components/providers', () => ({
  useAuth: () => ({ state: { user: { user_name: 'Test User', email: 'test@example.com' } } }),
}));

vi.mock('@/lib/api/agents', () => ({
  archiveAgentJob: mocks.archive,
  restoreAgentJob: mocks.restore,
}));

vi.mock('./useAgentJobs', () => ({
  useAgentJobList: mocks.useAgentJobList,
  useAgentProjects: mocks.useAgentProjects,
  useProjectJobs: mocks.useProjectJobs,
}));

import { ArchivedTasksView } from './ArchivedTasksView';
import { AgentsSidebar } from './AgentsSidebar';

function job(overrides: Partial<AgentJob> = {}): AgentJob {
  return {
    id: 'job-1',
    threadId: 'thread-1',
    turnNo: 1,
    createdAt: new Date().toISOString(),
    title: 'Development environment setup',
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

beforeEach(() => {
  mocks.useAgentProjects.mockReturnValue({
    projects: [],
    loading: false,
    error: null,
    reload: mocks.reloadProjects,
  });
  mocks.useProjectJobs.mockReturnValue({
    jobsByRepo: new Map(),
    loadingRepos: new Set(),
    errorRepos: new Map(),
    load: mocks.loadRepo,
  });
});

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

describe('agent task archiving', () => {
  it('archives a conversation from its sidebar row and leaves an active task', async () => {
    mocks.useAgentJobList.mockReturnValue({
      jobs: [job()],
      loading: false,
      error: null,
      reload: mocks.reload,
    });
    mocks.archive.mockResolvedValue({
      thread_id: 'thread-1',
      archived: true,
      archived_at: new Date().toISOString(),
    });

    render(<AgentsSidebar />);
    fireEvent.click(screen.getByRole('button', { name: 'Archive Development environment setup' }));

    await waitFor(() => expect(mocks.archive).toHaveBeenCalledWith('job-1'));
    expect(mocks.reload).toHaveBeenCalledOnce();
    expect(mocks.replace).toHaveBeenCalledWith('/agents');
    expect(screen.queryByText('Development environment setup')).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Archived' })).toHaveAttribute(
      'href',
      '/agents/archived',
    );
  });

  it('loads archived conversations separately and restores them', async () => {
    mocks.useAgentJobList.mockReturnValue({
      jobs: [job({ state: 'running' })],
      loading: false,
      error: null,
      reload: mocks.reload,
    });
    mocks.restore.mockResolvedValue({
      thread_id: 'thread-1',
      archived: false,
      archived_at: null,
    });

    render(<ArchivedTasksView />);

    expect(mocks.useAgentJobList).toHaveBeenCalledWith(10_000, true);
    expect(screen.getByText('Running')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Restore' }));

    await waitFor(() => expect(mocks.restore).toHaveBeenCalledWith('job-1'));
    expect(mocks.reload).toHaveBeenCalledOnce();
    expect(screen.queryByText('Development environment setup')).not.toBeInTheDocument();
  });
});
