// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { StrictMode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import AgentsConnectedPage from './page';

const navigation = vi.hoisted(() => ({ search: '', replace: vi.fn() }));

vi.mock('next/navigation', () => ({
  useRouter: () => ({ replace: navigation.replace }),
  useSearchParams: () => new URLSearchParams(navigation.search),
}));

vi.mock('@/lib/api/agents', () => ({
  connectGitHub: vi.fn(),
  connectGitLab: vi.fn(),
}));

import { connectGitHub, connectGitLab } from '@/lib/api/agents';

const mockedConnectGitHub = connectGitHub as unknown as ReturnType<typeof vi.fn>;
const mockedConnectGitLab = connectGitLab as unknown as ReturnType<typeof vi.fn>;

describe('AgentsConnectedPage', () => {
  beforeEach(() => {
    navigation.search = '';
    navigation.replace.mockReset();
    mockedConnectGitHub.mockReset();
    mockedConnectGitLab.mockReset();
  });

  afterEach(() => {
    cleanup();
  });

  it('completes GitLab OAuth with the returned state', async () => {
    navigation.search = 'provider=gitlab&code=gitlab-code&state=signed-state';
    mockedConnectGitLab.mockResolvedValue({ provider: 'gitlab', connected: true });

    render(<AgentsConnectedPage />);

    await waitFor(() =>
      expect(mockedConnectGitLab).toHaveBeenCalledWith('gitlab-code', 'signed-state'),
    );
    expect(navigation.replace).toHaveBeenCalledWith('/agents/integrations?connected=gitlab');
  });

  it('defaults legacy callbacks without a provider to GitHub', async () => {
    navigation.search = 'code=github-code&state=signed-state';
    mockedConnectGitHub.mockResolvedValue({ connections: [{}], repos: [], install_url: null });

    render(<AgentsConnectedPage />);

    await waitFor(() =>
      expect(mockedConnectGitHub).toHaveBeenCalledWith('github-code', 'signed-state'),
    );
    expect(navigation.replace).toHaveBeenCalledWith('/agents/integrations?connected=github');
  });

  it('submits a single-use OAuth state only once in React Strict Mode', async () => {
    navigation.search = 'provider=github&code=github-code&state=signed-state';
    mockedConnectGitHub.mockResolvedValue({ connections: [{}], repos: [], install_url: null });

    render(
      <StrictMode>
        <AgentsConnectedPage />
      </StrictMode>,
    );

    await waitFor(() => expect(navigation.replace).toHaveBeenCalled());
    expect(mockedConnectGitHub).toHaveBeenCalledTimes(1);
  });

  it('offers App installation after OAuth finds no accessible installation', async () => {
    navigation.search = 'provider=github&code=github-code&state=signed-state';
    mockedConnectGitHub.mockResolvedValue({
      connections: [],
      repos: [],
      install_url: 'https://github.com/apps/freeinference/installations/new?state=fresh-state',
    });

    render(<AgentsConnectedPage />);

    const install = await screen.findByRole('link', { name: 'Install GitHub App' });
    expect(install).toHaveAttribute(
      'href',
      'https://github.com/apps/freeinference/installations/new?state=fresh-state',
    );
    expect(navigation.replace).not.toHaveBeenCalled();
  });

  it('does not exchange a code without connection state', async () => {
    navigation.search = 'provider=gitlab&code=gitlab-code';

    render(<AgentsConnectedPage />);

    expect(
      await screen.findByText('GitLab did not return the connection state.'),
    ).toBeInTheDocument();
    expect(mockedConnectGitLab).not.toHaveBeenCalled();
  });
});
