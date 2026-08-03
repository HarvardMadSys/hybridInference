// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { createAgentJob, getAgentConfig, listRepoBranches } from '@/lib/api/agents';
import type { AgentConfigApi } from '@/lib/api/agents';

import { TaskComposer } from './TaskComposer';

const navigation = vi.hoisted(() => ({ push: vi.fn() }));

vi.mock('next/navigation', () => ({
  useRouter: () => navigation,
}));

vi.mock('@/lib/api/agents', () => ({
  createAgentJob: vi.fn(),
  getAgentConfig: vi.fn(),
  listRepoBranches: vi.fn(),
}));

function config(overrides: Partial<AgentConfigApi> = {}): AgentConfigApi {
  return {
    repos: ['owner/repository'],
    runtimes: ['claude-code'],
    models: ['model-a'],
    default_budget_usd: 2,
    setup_egress_tier: 'trusted',
    agent_egress_tier: 'platform_only',
    github_connected: true,
    github_install_url: null,
    ...overrides,
  };
}

describe('TaskComposer source-control onboarding', () => {
  beforeEach(() => {
    navigation.push.mockReset();
    vi.mocked(createAgentJob).mockReset();
    vi.mocked(getAgentConfig).mockReset();
    vi.mocked(listRepoBranches).mockReset();
    vi.mocked(listRepoBranches).mockResolvedValue({ default: 'dev', branches: ['dev', 'main'] });
  });

  afterEach(() => cleanup());

  it('shows inline GitHub onboarding and a disabled composer when installation is available', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(
      config({
        github_connected: false,
        github_install_url: 'https://github.com/apps/example/installations/new',
      }),
    );

    render(<TaskComposer />);

    expect(await screen.findByText('Connect GitHub to start')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'What should the agent do?' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /Connect GitHub/ })).toHaveAttribute(
      'href',
      '/agents/integrations',
    );
    expect(screen.getByRole('textbox')).toBeDisabled();
    expect(screen.getByRole('button', { name: /Run/ })).toBeDisabled();
    expect(screen.queryByLabelText('Repository')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Agent')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Model')).not.toBeInTheDocument();
    expect(listRepoBranches).not.toHaveBeenCalled();
  });

  it('uses friendly administrator copy when GitHub installation is unavailable', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(
      config({ github_connected: false, github_install_url: null }),
    );

    render(<TaskComposer />);

    expect(await screen.findByText('GitHub setup is not available yet')).toBeInTheDocument();
    expect(screen.getByText(/Contact your administrator/)).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /Connect GitHub/ })).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: /View integrations/ })).toHaveAttribute(
      'href',
      '/agents/integrations',
    );
    expect(screen.queryByText(/AGENT_/)).not.toBeInTheDocument();
    expect(screen.getByRole('textbox')).toBeDisabled();
    expect(screen.getByRole('button', { name: /Run/ })).toBeDisabled();
  });

  it('keeps the connected composer interactive and submits its selected values', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(config());
    vi.mocked(createAgentJob).mockResolvedValue({ id: 'ajob_1' } as never);

    render(<TaskComposer />);

    expect(await screen.findByLabelText('Repository')).toHaveValue('owner/repository');
    expect(await screen.findByLabelText('Branch')).toHaveValue('dev');
    expect(screen.getByLabelText('Agent')).toHaveValue('claude-code');
    expect(screen.getByLabelText('Model')).toHaveValue('model-a');
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
    expect(screen.getByRole('textbox')).not.toBeDisabled();

    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Fix the flaky test' } });
    fireEvent.click(screen.getByRole('button', { name: /Run/ }));

    await waitFor(() =>
      expect(createAgentJob).toHaveBeenCalledWith({
        repo: 'owner/repository',
        task_prompt: 'Fix the flaky test',
        runtime: 'claude-code',
        model: 'model-a',
        base_ref: 'dev',
      }),
    );
    expect(navigation.push).toHaveBeenCalledWith('/agents/ajob_1');
  });

  it('preselects a requested repository that is available in backend config', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(
      config({ repos: ['owner/default', 'owner/requested'] }),
    );

    render(<TaskComposer initialRepo="owner/requested" />);

    expect(await screen.findByLabelText('Repository')).toHaveValue('owner/requested');
    expect(listRepoBranches).toHaveBeenCalledWith('owner/requested');
  });

  it('falls back to the first configured repository for a stale request', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(
      config({ repos: ['owner/default', 'owner/another'] }),
    );

    render(<TaskComposer initialRepo="owner/deleted" />);

    expect(await screen.findByLabelText('Repository')).toHaveValue('owner/default');
    expect(listRepoBranches).toHaveBeenCalledWith('owner/default');
  });
});
