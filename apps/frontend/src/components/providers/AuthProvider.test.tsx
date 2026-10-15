// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { buildTimeSiteConfig, withAgentsUrl } from '@/config/site-config';
import { ACCOUNT_CHANGED_EVENT, getAccessToken, setAccessToken } from '@/lib/api/client';
import { AuthProvider, useAuth } from './AuthProvider';
import { SiteConfigProvider } from './SiteConfigProvider';

const loginApi = vi.fn();
const loginPhoneApi = vi.fn();
const logoutApi = vi.fn();
const endAgentSession = vi.fn();
const getMe = vi.fn();

vi.mock('@/lib/api/auth', () => ({
  login: (...args: unknown[]) => loginApi(...args),
  loginPhone: (...args: unknown[]) => loginPhoneApi(...args),
  logout: (...args: unknown[]) => logoutApi(...args),
  endAgentSession: (...args: unknown[]) => endAgentSession(...args),
}));

vi.mock('@/lib/api/user', () => ({
  getMe: (...args: unknown[]) => getMe(...args),
}));

let controller: ReturnType<typeof useAuth>;
let queryClient: QueryClient;

function Probe() {
  controller = useAuth();
  return <span>{controller.state.loading ? 'loading' : 'ready'}</span>;
}

async function renderWithAgents(agentsUrl: string) {
  render(
    <SiteConfigProvider initialConfig={withAgentsUrl(buildTimeSiteConfig, agentsUrl)}>
      <QueryClientProvider client={queryClient}>
        <AuthProvider>
          <Probe />
        </AuthProvider>
      </QueryClientProvider>
    </SiteConfigProvider>,
  );
  await waitFor(() => expect(screen.getByText('ready')).toBeInTheDocument());
}

beforeEach(() => {
  queryClient = new QueryClient();
  loginApi.mockReset().mockResolvedValue(undefined);
  loginPhoneApi.mockReset().mockResolvedValue(undefined);
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

describe('AuthProvider cached queries', () => {
  // Query keys such as ['apiKeys'] name no account, so whatever one account
  // fetched would be served to the next one from the same cache.
  it('drops the previous account’s cached answers when another account signs in', async () => {
    await renderWithAgents('/agents');
    queryClient.setQueryData(['apiKeys'], [{ key_prefix: 'sk-previous' }]);

    await act(() => controller.login('next@example.test', 'pw'));

    expect(queryClient.getQueryData(['apiKeys'])).toBeUndefined();
  });

  it('drops them on logout, even when the gateway logout fails', async () => {
    logoutApi.mockRejectedValueOnce(new Error('network'));
    await renderWithAgents('/agents');
    queryClient.setQueryData(['user', 'models'], ['previous-model']);

    await act(() => controller.logout().catch(() => undefined));

    expect(queryClient.getQueryData(['user', 'models'])).toBeUndefined();
  });

  it('keeps them when the sign-in fails', async () => {
    loginApi.mockRejectedValueOnce(new Error('bad credentials'));
    await renderWithAgents('/agents');
    queryClient.setQueryData(['apiKeys'], [{ key_prefix: 'sk-current' }]);

    await act(() => controller.login('user@example.test', 'wrong').catch(() => undefined));

    expect(queryClient.getQueryData(['apiKeys'])).toEqual([{ key_prefix: 'sk-current' }]);
  });
});

describe('AuthProvider phone sign-in', () => {
  it('starts the session like any other sign-in', async () => {
    await renderWithAgents('/agents');
    queryClient.setQueryData(['apiKeys'], [{ key_prefix: 'sk-previous' }]);

    await act(() => controller.loginPhone('+8613800138000', '123456'));

    expect(loginPhoneApi).toHaveBeenCalledWith({ phone: '+8613800138000', code: '123456' });
    expect(endAgentSession).toHaveBeenCalledWith('/agents');
    expect(queryClient.getQueryData(['apiKeys'])).toBeUndefined();
  });
});

describe('AuthProvider account change in another tab', () => {
  it('covers the page with a reload prompt', async () => {
    await renderWithAgents('/agents');
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();

    act(() => {
      window.dispatchEvent(new Event(ACCOUNT_CHANGED_EVENT));
    });

    expect(screen.getByRole('alertdialog')).toHaveTextContent(
      'You switched accounts in another tab',
    );
    expect(screen.getByRole('button', { name: 'Reload' })).toHaveFocus();
  });
});
