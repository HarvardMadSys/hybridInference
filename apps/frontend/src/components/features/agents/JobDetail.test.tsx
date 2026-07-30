// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  cancelAgentJob,
  followUpAgentJob,
  forkAgentJob,
  getAgentJobFiles,
  getAgentJobGit,
  runAgentTerminalCommand,
  writeAgentJobFile,
  type AgentJobFilesApi,
} from '@/lib/api/agents';

import { JobDetail, WORKSPACE_WIDTH_STORAGE_KEY } from './JobDetail';
import type { AgentJob } from './types';

const navigation = vi.hoisted(() => ({ push: vi.fn() }));

vi.mock('next/navigation', () => ({
  useRouter: () => navigation,
}));

vi.mock('@/lib/api/agents', () => ({
  cancelAgentJob: vi.fn(),
  followUpAgentJob: vi.fn(),
  forkAgentJob: vi.fn(),
  getAgentJobFiles: vi.fn(),
  getAgentJobGit: vi.fn(),
  runAgentTerminalCommand: vi.fn(),
  writeAgentJobFile: vi.fn(),
}));

function makeJob(overrides: Partial<AgentJob> = {}): AgentJob {
  return {
    id: 'ajob_1',
    title: 'Current task',
    prompt: 'Current task\nwith all of its detail.',
    state: 'running',
    repo: 'owner/repository',
    baseRef: 'dev',
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
    diffFileDetails: [
      {
        path: 'src/example.ts',
        add: 1,
        del: 0,
        lines: [{ marker: 'add', text: '+fixed' }],
      },
    ],
    diffStat: { add: 1, del: 0 },
    diffLines: [{ marker: 'add', text: '+fixed' }],
    rawLines: ['{"event_type":"message"}'],
    gates: [],
    usage: { tokensIn: '1.2k', tokensOut: '80', cachePct: 0, turns: 2 },
    egressDenials: 0,
    ...overrides,
  };
}

function openWorkspace() {
  fireEvent.click(screen.getByRole('button', { name: 'Open workspace' }));
}

const HISTORY = [
  { id: 1, role: 'user' as const, content: 'Earlier question', jobId: 'ajob_0', createdAt: null },
  {
    id: 2,
    role: 'assistant' as const,
    content: 'Earlier answer',
    jobId: 'ajob_0',
    createdAt: null,
  },
];

describe('JobDetail', () => {
  beforeEach(() => {
    navigation.push.mockReset();
    vi.mocked(cancelAgentJob).mockReset();
    vi.mocked(followUpAgentJob).mockReset();
    vi.mocked(forkAgentJob).mockReset();
    vi.mocked(getAgentJobFiles).mockReset();
    vi.mocked(getAgentJobGit).mockReset();
    vi.mocked(getAgentJobGit).mockResolvedValue({
      available: false,
      branch: '',
      changes: [],
      patch: '',
      commits: [],
    });
    vi.mocked(runAgentTerminalCommand).mockReset();
    vi.mocked(writeAgentJobFile).mockReset();
    sessionStorage.clear();
    localStorage.clear();
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
    expect(screen.queryByRole('button', { name: 'Details' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Run details' }));
    expect(screen.getByRole('dialog', { name: 'Run details' })).toBeInTheDocument();
    expect(screen.getByText('Input tokens')).toBeInTheDocument();
    expect(screen.getByText('Sandbox')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /Raw events/ }));
    expect(screen.getByText('{"event_type":"message"}')).toBeInTheDocument();
  });

  it('keeps the task as the default view behind a labeled, accessible workspace toggle', () => {
    render(<JobDetail job={makeJob()} />);

    const workspaceButton = screen.getByRole('button', { name: 'Open workspace' });
    expect(workspaceButton).toHaveTextContent('Workspace');
    expect(workspaceButton).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByRole('region', { name: 'Job workspace' })).not.toBeInTheDocument();
    expect(screen.queryByRole('tab', { name: 'Activity' })).not.toBeInTheDocument();
    expect(screen.queryByRole('tab', { name: 'Environment' })).not.toBeInTheDocument();
    expect(screen.getByText(/with all of its detail/)).toBeInTheDocument();
    expect(screen.getByLabelText('Add a follow-up')).toBeInTheDocument();
  });

  it('opens and closes from a stable control while preserving the selected tab', () => {
    render(<JobDetail job={makeJob()} />);

    const task = screen.getByRole('region', { name: 'Task progress' });
    openWorkspace();

    const closeButton = screen.getByRole('button', { name: 'Close workspace' });
    expect(closeButton).toHaveTextContent('Close workspace');
    expect(closeButton).toHaveAttribute('aria-expanded', 'true');
    expect(closeButton).toHaveAttribute('aria-controls', 'job-workspace-pane');
    expect(closeButton).toHaveClass('bg-gray-900', 'text-white');
    expect(screen.getByRole('region', { name: 'Job workspace' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /Git/ })).toHaveAttribute('aria-selected', 'true');
    expect(task).toBeInTheDocument();
    expect(task).toHaveClass('hidden', 'lg:flex', 'overflow-y-auto');
    expect(screen.getByRole('region', { name: 'Job workspace' })).toHaveClass(
      'overflow-y-auto',
      'lg:w-[var(--job-workspace-width)]',
    );

    const terminalTab = screen.getByRole('tab', { name: 'Terminal' });
    fireEvent.click(terminalTab);
    expect(terminalTab).toHaveAttribute('aria-selected', 'true');
    fireEvent.click(terminalTab);
    expect(terminalTab).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByRole('region', { name: 'Job workspace' })).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Close workspace' }));
    const reopenButton = screen.getByRole('button', { name: 'Open workspace' });
    expect(reopenButton).toHaveTextContent('Workspace');
    expect(reopenButton).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByRole('region', { name: 'Job workspace' })).not.toBeInTheDocument();

    openWorkspace();
    expect(screen.getByRole('tab', { name: 'Terminal' })).toHaveAttribute('aria-selected', 'true');
  });

  it('resizes the workspace pane by dragging the seam, and remembers the width', () => {
    render(<JobDetail job={makeJob()} />);

    expect(screen.queryByRole('separator', { name: 'Resize workspace' })).not.toBeInTheDocument();
    openWorkspace();

    const pane = screen.getByRole('region', { name: 'Job workspace' });
    const seam = screen.getByRole('separator', { name: 'Resize workspace' });
    expect(seam).toHaveAttribute('aria-controls', 'job-workspace-pane');
    expect(pane.style.getPropertyValue('--job-workspace-width')).toBe('560px');

    // Dragging the seam left widens the workspace and narrows the transcript.
    fireEvent.pointerDown(seam, { button: 0, clientX: 800 });
    fireEvent.pointerMove(window, { clientX: 700 });
    fireEvent.pointerUp(window, { clientX: 700 });

    expect(pane.style.getPropertyValue('--job-workspace-width')).toBe('660px');
    expect(window.localStorage.getItem(WORKSPACE_WIDTH_STORAGE_KEY)).toBe('660');

    cleanup();
    render(<JobDetail job={makeJob()} />);
    openWorkspace();

    expect(
      screen
        .getByRole('region', { name: 'Job workspace' })
        .style.getPropertyValue('--job-workspace-width'),
    ).toBe('660px');
  });

  it('shows a selectable diff-first Git workspace with review and commit tabs', async () => {
    render(
      <JobDetail
        job={makeJob({
          state: 'done',
          prLabel: 'draft PR 44',
          prUrl: 'https://github.com/owner/repository/pull/44',
        })}
      />,
    );

    openWorkspace();

    await waitFor(() =>
      expect(screen.getByLabelText('Git workspace')).toHaveTextContent('Archived result'),
    );
    expect(screen.getByLabelText('Git workspace')).toHaveTextContent('src/example.ts');
    fireEvent.click(screen.getByRole('tab', { name: 'review' }));
    expect(screen.getByRole('link', { name: 'Open pull request' })).toHaveAttribute(
      'href',
      'https://github.com/owner/repository/pull/44',
    );
    fireEvent.click(screen.getByRole('tab', { name: 'diff' }));
    expect(screen.getByText('+fixed')).toBeInTheDocument();
    await waitFor(() => expect(getAgentJobGit).toHaveBeenCalledWith('ajob_1'));
  });

  it('keeps the agent transcript and executes commands in the workspace terminal', async () => {
    vi.mocked(runAgentTerminalCommand).mockResolvedValue({
      output: 'README.md\n',
      stderr: '',
      exit_code: 0,
      cwd: '/workspace',
    });
    vi.mocked(getAgentJobFiles).mockResolvedValue({
      path: '',
      kind: 'directory',
      entries: [],
    });
    render(<JobDetail job={makeJob()} />);

    openWorkspace();
    fireEvent.click(screen.getByRole('tab', { name: 'Terminal' }));

    const terminal = screen.getByLabelText('Workspace terminal');
    expect(terminal).toHaveTextContent('$ pytest -q');
    expect(terminal).toHaveTextContent('2 passed');
    const input = within(terminal).getByRole('textbox', { name: 'Terminal command' });
    fireEvent.change(input, { target: { value: 'ls' } });
    fireEvent.submit(input.closest('form')!);

    await waitFor(() =>
      expect(runAgentTerminalCommand).toHaveBeenCalledWith('ajob_1', 'ls', '/workspace'),
    );
    expect(await within(terminal).findByText('README.md')).toBeInTheDocument();
    expect(terminal).not.toHaveTextContent('Read only');

    fireEvent.click(screen.getByRole('tab', { name: 'Files' }));
    fireEvent.click(screen.getByRole('tab', { name: 'Terminal' }));
    expect(screen.getByLabelText('Workspace terminal')).toHaveTextContent('README.md');

    fireEvent.click(screen.getByRole('button', { name: 'Close workspace' }));
    openWorkspace();
    expect(screen.getByLabelText('Workspace terminal')).toHaveTextContent('README.md');
  });

  it('loads Files lazily and safely refuses to preview a symlink', async () => {
    vi.mocked(getAgentJobFiles)
      .mockResolvedValueOnce({
        path: '',
        kind: 'directory',
        entries: [{ name: 'latest', path: 'latest', kind: 'symlink', size: null, status: null }],
      })
      .mockResolvedValueOnce({ path: 'latest', kind: 'symlink' });
    render(<JobDetail job={makeJob()} />);

    expect(getAgentJobFiles).not.toHaveBeenCalled();
    openWorkspace();
    fireEvent.click(screen.getByRole('tab', { name: 'Files' }));

    await waitFor(() => expect(getAgentJobFiles).toHaveBeenCalledWith('ajob_1', ''));
    fireEvent.click(await screen.findByRole('button', { name: /latest/ }));
    await waitFor(() => expect(getAgentJobFiles).toHaveBeenCalledWith('ajob_1', 'latest'));
    expect(await screen.findByText('Symlinks are not previewed.')).toBeInTheDocument();
  });

  it('opens a changed text file and handles nullable file metadata', async () => {
    vi.mocked(getAgentJobFiles)
      .mockResolvedValueOnce({
        path: '',
        kind: 'directory',
        entries: [
          {
            name: 'README.md',
            path: 'README.md',
            kind: 'file',
            size: null,
            status: 'modified',
          },
        ],
      })
      .mockResolvedValueOnce({
        path: 'README.md',
        kind: 'file',
        content: '# Updated',
        size: null,
        binary: false,
        truncated: false,
        status: 'modified',
      });
    render(<JobDetail job={makeJob()} />);
    openWorkspace();
    fireEvent.click(screen.getByRole('tab', { name: 'Files' }));

    fireEvent.click(await screen.findByRole('button', { name: /README\.md/ }));

    expect(await screen.findByText('# Updated')).toBeInTheDocument();
    expect(screen.getByLabelText('Workspace files')).not.toHaveTextContent('null B');
  });

  it('edits and saves a file from the live worktree', async () => {
    vi.mocked(getAgentJobFiles)
      .mockResolvedValueOnce({
        path: '',
        kind: 'directory',
        entries: [{ name: 'README.md', path: 'README.md', kind: 'file' }],
        writable: true,
        source: 'workspace',
      })
      .mockResolvedValue({
        path: 'README.md',
        kind: 'file',
        content: '# Before',
        size: 8,
        binary: false,
        truncated: false,
        writable: true,
        source: 'workspace',
      });
    vi.mocked(writeAgentJobFile).mockResolvedValue({
      path: 'README.md',
      kind: 'file',
      content: '# After',
      size: 7,
      binary: false,
      truncated: false,
      writable: true,
      source: 'workspace',
    });
    render(<JobDetail job={makeJob()} />);
    openWorkspace();
    fireEvent.click(screen.getByRole('tab', { name: 'Files' }));
    fireEvent.click(await screen.findByRole('button', { name: /README\.md/ }));
    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }));

    const editor = await screen.findByRole('textbox', { name: 'Edit README.md' });
    fireEvent.change(editor, { target: { value: '# After' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() =>
      expect(writeAgentJobFile).toHaveBeenCalledWith('ajob_1', 'README.md', '# After'),
    );
    expect(screen.getByLabelText('Workspace files')).toHaveTextContent('Live worktree');
  });

  it('expands a directory in place and keeps the tree around the open file', async () => {
    const files = async (_jobId: string, path = ''): Promise<AgentJobFilesApi> => {
      if (path === '')
        return {
          path: '',
          kind: 'directory',
          entries: [
            { name: 'README.md', path: 'README.md', kind: 'file', size: 12 },
            { name: 'src', path: 'src', kind: 'directory' },
          ],
        };
      if (path === 'src')
        return {
          path: 'src',
          kind: 'directory',
          entries: [
            { name: 'example.ts', path: 'src/example.ts', kind: 'file', status: 'modified' },
          ],
        };
      return {
        path: 'src/example.ts',
        kind: 'file',
        content: 'export const value = 1;',
        size: 24,
        binary: false,
        truncated: false,
      };
    };
    vi.mocked(getAgentJobFiles).mockImplementation(files);
    render(<JobDetail job={makeJob()} />);
    openWorkspace();
    fireEvent.click(screen.getByRole('tab', { name: 'Files' }));

    // A directory expands beneath itself rather than replacing the listing.
    fireEvent.click(await screen.findByRole('button', { name: /src/ }));
    await waitFor(() => expect(getAgentJobFiles).toHaveBeenCalledWith('ajob_1', 'src'));
    expect(await screen.findByRole('button', { name: /example\.ts/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /README\.md/ })).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /example\.ts/ }));

    // The viewer highlights the source, so assert on the pane's whole text.
    await waitFor(() =>
      expect(screen.getByLabelText('Workspace files')).toHaveTextContent('export const value = 1;'),
    );
    // The sibling entries survive opening a file — the tree is not replaced.
    expect(screen.getByRole('button', { name: /README\.md/ })).toBeInTheDocument();
  });

  it('opens a changed file from the Changes list', async () => {
    vi.mocked(getAgentJobFiles)
      .mockResolvedValueOnce({ path: '', kind: 'directory', entries: [] })
      .mockResolvedValueOnce({
        path: 'src/example.ts',
        kind: 'file',
        content: 'const fixed = true;',
        size: 20,
        binary: false,
        truncated: false,
        status: 'modified',
      });
    render(<JobDetail job={makeJob()} />);
    openWorkspace();
    fireEvent.click(screen.getByRole('tab', { name: 'Files' }));
    fireEvent.click(await screen.findByRole('tab', { name: /Changes/ }));

    fireEvent.click(screen.getByRole('button', { name: /example\.ts/ }));

    await waitFor(() => expect(getAgentJobFiles).toHaveBeenCalledWith('ajob_1', 'src/example.ts'));
    expect(await screen.findByText('src')).toBeInTheDocument();
    expect(screen.getByLabelText('Workspace files')).toHaveTextContent('modified');
  });

  it('refreshes an open Files workspace once a running job finishes', async () => {
    vi.mocked(getAgentJobFiles).mockResolvedValue({ path: '', kind: 'directory', entries: [] });
    const { rerender } = render(<JobDetail job={makeJob({ state: 'running' })} />);
    openWorkspace();
    fireEvent.click(screen.getByRole('tab', { name: 'Files' }));
    await waitFor(() => expect(getAgentJobFiles).toHaveBeenCalledTimes(1));

    rerender(<JobDetail job={makeJob({ state: 'done' })} />);

    await waitFor(() => expect(getAgentJobFiles).toHaveBeenCalledTimes(2));
  });

  it('copies a message to the clipboard', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
    render(<JobDetail job={makeJob({ threadMessages: HISTORY })} />);

    fireEvent.click(screen.getAllByRole('button', { name: 'Copy message' })[0]);

    await waitFor(() => expect(writeText).toHaveBeenCalledWith('Earlier question'));
  });

  it('forks the conversation from an earlier assistant message', async () => {
    vi.mocked(forkAgentJob).mockResolvedValue({ id: 'ajob_fork' } as never);
    render(<JobDetail job={makeJob({ threadMessages: HISTORY })} />);

    fireEvent.click(screen.getByRole('button', { name: 'Fork from here' }));

    await waitFor(() => expect(forkAgentJob).toHaveBeenCalledWith('ajob_0'));
    expect(navigation.push).toHaveBeenCalledWith('/agents/ajob_fork');
  });

  it('edit-and-rewind forks the prior turn and leaves the prompt as a draft', async () => {
    vi.mocked(forkAgentJob).mockResolvedValue({ id: 'ajob_fork' } as never);
    render(<JobDetail job={makeJob({ threadMessages: HISTORY })} />);

    fireEvent.click(screen.getByRole('button', { name: 'Edit & rewind' }));

    await waitFor(() => expect(forkAgentJob).toHaveBeenCalledWith('ajob_0'));
    expect(sessionStorage.getItem('agent-rewind-draft-ajob_fork')).toBe(
      'Current task\nwith all of its detail.',
    );
    expect(navigation.push).toHaveBeenCalledWith('/agents/ajob_fork');
  });

  it('prefills the composer from a rewind draft exactly once', () => {
    sessionStorage.setItem('agent-rewind-draft-ajob_1', 'edited prompt');
    render(<JobDetail job={makeJob()} />);

    expect(screen.getByLabelText('Add a follow-up')).toHaveValue('edited prompt');
    expect(sessionStorage.getItem('agent-rewind-draft-ajob_1')).toBeNull();
  });
});
