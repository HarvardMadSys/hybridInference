// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { buildTimeSiteConfig, withAgentsUrl } from '@/config/site-config';
import { getAccessToken, setAccessToken } from '@/lib/api/client';
import { AuthProvider, useAuth } from './AuthProvider';
import { SiteConfigProvider } from './SiteConfigProvider';

const loginApi = vi.fn();
const logoutApi = vi.fn();
const endAgentSession = vi.fn();
const getMe = vi.fn();

vi.mock('@/lib/api/auth', () => ({
  login: (...args: unknown[]) => loginApi(...args),
  logout: (...args: unknown[]) => logoutApi(...args),
  endAgentSession: (...args: unknown[]) => endAgentSession(...args),
}));

vi.mock('@/lib/api/user', () => ({
  getMe: (...args: unknown[]) => getMe(...args),
}));

let controller: ReturnType<typeof useAuth>;

function Probe() {
  controller = useAuth();
  return <span>{controller.state.loading ? 'loading' : 'ready'}</span>;
}

async function renderWithAgents(agentsUrl: string) {
  render(
    <SiteConfigProvider initialConfig={withAgentsUrl(buildTimeSiteConfig, agentsUrl)}>
      <AuthProvider>
        <Probe />
      </AuthProvider>
    </SiteConfigProvider>,
  );
  await waitFor(() => expect(screen.getByText('ready')).toBeInTheDocument());
}

beforeEach(() => {
  loginApi.mockReset().mockResolvedValue(undefined);
  logoutApi.mockReset().mockResolvedValue(undefined);
  endAgentSession.mockReset().mockResolvedValue(undefined);
  getMe.mockReset().mockRejectedValue(new Error('signed out'));
});

afterEach(() => {
  cleanup();
});

describe('AuthProvider agent session', () => {
  it('ends the agent session on logout', async () => {
    await renderWithAgents('/agents');

    await act(() => controller.logout());

    expect(logoutApi).toHaveBeenCalledTimes(1);
    expect(endAgentSession).toHaveBeenCalledWith('/agents');
  });

  it('ends the agent session even when the gateway logout fails', async () => {
    logoutApi.mockRejectedValueOnce(new Error('network'));
    await renderWithAgents('/agents');

    await act(() => controller.logout().catch(() => undefined));

    expect(endAgentSession).toHaveBeenCalledWith('/agents');
  });

  it('ends the agent session after a sign-in, so a switched user never inherits it', async () => {
    await renderWithAgents('/agents');

    await act(() => controller.login('user@example.test', 'pw'));

    expect(loginApi).toHaveBeenCalledTimes(1);
    expect(endAgentSession).toHaveBeenCalledWith('/agents');
  });

  it('keeps the agent session when the sign-in fails', async () => {
    loginApi.mockRejectedValueOnce(new Error('bad credentials'));
    await renderWithAgents('/agents');

    await act(() => controller.login('user@example.test', 'wrong').catch(() => undefined));

    expect(endAgentSession).not.toHaveBeenCalled();
  });
});

describe('AuthProvider adopted session', () => {
  it('signs in with a token the backend issued outside login(), as first-run setup does', async () => {
    await renderWithAgents('/agents');
    getMe.mockResolvedValueOnce({
      id: 'admin-1',
      email: null,
      login_name: 'admin',
      user_name: 'admin',
      role: 'admin',
      is_admin: true,
    });

    await act(() => controller.adoptSession('setup-token'));

    expect(getAccessToken()).toBe('setup-token');
    expect(endAgentSession).toHaveBeenCalledWith('/agents');
    expect(loginApi).not.toHaveBeenCalled();
    expect(controller.state).toMatchObject({
      isAuthenticated: true,
      user: { id: 'admin-1', email: null, login_name: 'admin', is_admin: true },
    });
    setAccessToken(null);
  });
});
