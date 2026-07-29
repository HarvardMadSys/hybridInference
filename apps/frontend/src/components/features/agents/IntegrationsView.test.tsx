// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { IntegrationsView } from './IntegrationsView';
import type { AgentIntegrationsApi } from '@/lib/api/agents';

vi.mock('@/lib/api/agents', () => ({
  getAgentIntegrations: vi.fn(),
  disconnectAgentIntegration: vi.fn(),
}));

import { disconnectAgentIntegration, getAgentIntegrations } from '@/lib/api/agents';

const mockedGetIntegrations = getAgentIntegrations as unknown as ReturnType<typeof vi.fn>;
const mockedDisconnect = disconnectAgentIntegration as unknown as ReturnType<typeof vi.fn>;

const integrations: AgentIntegrationsApi = {
  providers: [
    {
      provider: 'github',
      configured: true,
      connected: true,
      connect_url: 'https://github.com/apps/example/installations/new?state=signed',
      capabilities: ['repository:read', 'pull_request:write'],
      accounts: [{ id: 'gh-42', label: 'HarvardMadSys' }],
      repositories: [
        { id: 'one', name: 'HarvardMadSys/one' },
        { id: 'two', name: 'HarvardMadSys/two' },
      ],
    },
    {
      provider: 'gitlab',
      configured: true,
      connected: false,
      connect_url: 'https://gitlab.com/oauth/authorize?state=signed',
      capabilities: ['repository:read'],
      accounts: [],
      repositories: [],
    },
  ],
};

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

describe('IntegrationsView', () => {
  it('shows connected accounts and a same-window connect link', async () => {
    mockedGetIntegrations.mockResolvedValue(integrations);

    render(<IntegrationsView connectedProvider="github" />);

    expect(
      await screen.findByText('Connected as HarvardMadSys · 2 repositories'),
    ).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent('GitHub connected.');
    expect(screen.getByRole('link', { name: /Connect/ })).toHaveAttribute(
      'href',
      'https://gitlab.com/oauth/authorize?state=signed',
    );
    expect(screen.getByText('Custom MCP servers')).toBeInTheDocument();
  });

  it('marks providers that need operator configuration', async () => {
    mockedGetIntegrations.mockResolvedValue({ providers: [] });

    render(<IntegrationsView />);

    expect(await screen.findAllByText('Admin setup required')).toHaveLength(2);
  });

  it('does not trust the callback query string as connection status', async () => {
    mockedGetIntegrations.mockResolvedValue({
      providers: integrations.providers.map((provider) => ({
        ...provider,
        connected: false,
        accounts: [],
        repositories: [],
      })),
    });

    render(<IntegrationsView connectedProvider="github" />);

    await screen.findAllByRole('link', { name: /Connect/ });
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });

  it('still lets a user disconnect a stored account after provider setup is removed', async () => {
    mockedGetIntegrations.mockResolvedValue({
      providers: [{ ...integrations.providers[0], configured: false, connect_url: null }],
    });

    render(<IntegrationsView />);

    await screen.findByText('Connected as HarvardMadSys · 2 repositories');
    expect(screen.getByText('Manage')).toBeInTheDocument();
    expect(screen.getAllByText('Admin setup required')).toHaveLength(1);
  });

  it('disconnects an account and refreshes provider state', async () => {
    mockedGetIntegrations.mockResolvedValueOnce(integrations).mockResolvedValueOnce({
      providers: [
        { ...integrations.providers[0], connected: false, accounts: [], repositories: [] },
        integrations.providers[1],
      ],
    });
    mockedDisconnect.mockResolvedValue(undefined);

    render(<IntegrationsView />);
    await screen.findByText('Connected as HarvardMadSys · 2 repositories');
    fireEvent.click(screen.getByText('Manage'));
    fireEvent.click(screen.getByRole('button', { name: 'Disconnect HarvardMadSys' }));

    await waitFor(() => expect(mockedDisconnect).toHaveBeenCalledWith('github', 'gh-42'));
    expect(await screen.findByRole('status')).toHaveTextContent('HarvardMadSys disconnected.');
    expect(mockedGetIntegrations).toHaveBeenCalledTimes(2);
  });
});
