// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AgentJobApi, AgentProjectApi } from '@/lib/api/agents';

const mocks = vi.hoisted(() => ({
  listAgentJobs: vi.fn(),
  listAgentProjects: vi.fn(),
}));

vi.mock('next/navigation', () => ({
  usePathname: () => '/agents',
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
}));

vi.mock('@/components/providers', () => ({
  useAuth: () => ({ state: { user: { user_name: 'Ada', email: 'ada@example.com' } } }),
}));

vi.mock('@/lib/api/agents', () => ({
  archiveAgentJob: vi.fn(),
  getAgentJob: vi.fn(),
  getAgentJobArtifact: vi.fn(),
  getAgentJobFiles: vi.fn(),
  getAgentJobThread: vi.fn(),
  listAgentJobEvents: vi.fn(),
  listAgentJobs: mocks.listAgentJobs,
  listAgentProjects: mocks.listAgentProjects,
  streamAgentJob: vi.fn(),
}));

import { AgentsSidebar, SIDEBAR_WIDTH_STORAGE_KEY } from './AgentsSidebar';

function apiJob(overrides: Partial<AgentJobApi> = {}): AgentJobApi {
  return {
    id: 'job-1',
    thread_id: 'thread-1',
    turn_no: 1,
    repo: 'murphy/hybridInference',
    task_prompt: 'Explain this repo',
    runtime: 'claude-code',
    model: 'glm-5.1',
    state: 'succeeded',
    created_at: '2026-07-29T10:00:00Z',
    ...overrides,
  } as AgentJobApi;
}

function apiProject(overrides: Partial<AgentProjectApi> = {}): AgentProjectApi {
  return {
    repo: 'murphy/hybridInference',
    task_count: 1,
    active_count: 0,
    last_activity_at: '2026-07-29T10:00:00Z',
    ...overrides,
  };
}

describe('AgentsSidebar layout', () => {
  beforeEach(() => {
    localStorage.clear();
    window.innerWidth = 1400;
    mocks.listAgentJobs.mockResolvedValue([]);
    mocks.listAgentProjects.mockResolvedValue([]);
  });

  afterEach(() => {
    cleanup();
    vi.resetAllMocks();
  });

  it('renders at the remembered width and resizes from the seam', async () => {
    render(<AgentsSidebar />);
    await waitFor(() => expect(screen.getByText('No jobs yet.')).toBeInTheDocument());

    const sidebar = document.getElementById('agents-sidebar');
    expect(sidebar).toHaveStyle({ width: '288px' });

    const seam = screen.getByRole('separator', { name: 'Resize task list' });
    expect(seam).toHaveAttribute('aria-controls', 'agents-sidebar');
    fireEvent.pointerDown(seam, { button: 0, clientX: 288 });
    fireEvent.pointerMove(window, { clientX: 360 });
    fireEvent.pointerUp(window, { clientX: 360 });

    expect(sidebar).toHaveStyle({ width: '360px' });
    expect(localStorage.getItem(SIDEBAR_WIDTH_STORAGE_KEY)).toBe('360');
  });

  it('renders neither the list nor its seam when collapsed', () => {
    render(<AgentsSidebar collapsed />);

    expect(document.getElementById('agents-sidebar')).not.toBeInTheDocument();
    expect(screen.queryByRole('separator', { name: 'Resize task list' })).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'New task' })).not.toBeInTheDocument();
  });
});

describe('AgentsSidebar project tree', () => {
  beforeEach(() => {
    localStorage.clear();
    window.innerWidth = 1400;
    mocks.listAgentProjects.mockResolvedValue([
      apiProject({ task_count: 2, active_count: 1 }),
      apiProject({
        repo: 'murphy/sglang',
        task_count: 1,
        last_activity_at: '2026-06-01T10:00:00Z',
      }),
    ]);
    // The shared page is newest-first across every project, so the quiet one
    // has nothing in it — exactly the case a folder must survive.
    mocks.listAgentJobs.mockImplementation(
      async (_limit?: number, _archived?: boolean, repo?: string) => {
        if (repo === 'murphy/sglang') {
          return [
            apiJob({
              id: 'sg-1',
              thread_id: 'sg',
              repo: 'murphy/sglang',
              task_prompt: 'Profile the scheduler',
              created_at: '2026-06-01T10:00:00Z',
            }),
          ];
        }
        if (repo) return [];
        return [
          apiJob({ id: 'hi-1', thread_id: 'hi-1', task_prompt: 'Explain this repo' }),
          apiJob({
            id: 'hi-2',
            thread_id: 'hi-2',
            task_prompt: 'Add Git terminal',
            state: 'running',
            created_at: '2026-07-29T11:00:00Z',
          }),
        ];
      },
    );
  });

  afterEach(() => {
    cleanup();
    vi.resetAllMocks();
  });

  it('groups tasks under project folders and opens the most recent one', async () => {
    render(<AgentsSidebar />);

    await waitFor(() =>
      expect(screen.getByRole('button', { name: /hybridInference/ })).toBeInTheDocument(),
    );
    const project = screen.getByRole('button', { name: /hybridInference/ });
    expect(project).toHaveAttribute('aria-expanded', 'true');
    expect(project).toHaveAttribute('title', 'murphy/hybridInference');
    expect(screen.getByText('Add Git terminal')).toBeInTheDocument();

    // The quiet project is a folder of its own, closed, with its rows unloaded.
    const quiet = screen.getByRole('button', { name: /sglang/ });
    expect(quiet).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByText('Profile the scheduler')).not.toBeInTheDocument();
  });

  it('offers a separate new-task link for each project', async () => {
    render(<AgentsSidebar />);

    const project = await screen.findByRole('button', { name: /hybridInference/ });
    const newTask = screen.getByRole('link', { name: 'New task in hybridInference' });

    expect(newTask).toHaveAttribute('href', '/agents?repo=murphy%2FhybridInference');
    expect(project).not.toContainElement(newTask);
    expect(project).toHaveAttribute('aria-expanded', 'true');
  });

  it('loads a quiet project only once its folder is opened, and remembers that', async () => {
    render(<AgentsSidebar />);
    await waitFor(() => expect(screen.getByRole('button', { name: /sglang/ })).toBeInTheDocument());
    expect(mocks.listAgentJobs).not.toHaveBeenCalledWith(
      expect.anything(),
      expect.anything(),
      'murphy/sglang',
    );

    fireEvent.click(screen.getByRole('button', { name: /sglang/ }));

    await waitFor(() => expect(screen.getByText('Profile the scheduler')).toBeInTheDocument());
    expect(mocks.listAgentJobs).toHaveBeenCalledWith(200, false, 'murphy/sglang');
    expect(JSON.parse(localStorage.getItem('agents.sidebar.projects') ?? '{}')).toMatchObject({
      'murphy/sglang': true,
    });
  });

  it('marks a closed folder that is hiding live work', async () => {
    render(<AgentsSidebar />);
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /hybridInference/ })).toBeInTheDocument(),
    );

    fireEvent.click(screen.getByRole('button', { name: /hybridInference/ }));

    await waitFor(() => expect(screen.getByLabelText('1 running or queued')).toBeInTheDocument());
    expect(screen.queryByText('Add Git terminal')).not.toBeInTheDocument();
  });
});
