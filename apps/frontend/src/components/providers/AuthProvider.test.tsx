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
const logoutAllApi = vi.fn();
const switchAccountApi = vi.fn();
const removeAccountApi = vi.fn();
const listAccounts = vi.fn();
const agentSessionUser = vi.fn();
const endAgentSession = vi.fn();
const getMe = vi.fn();

vi.mock('@/lib/api/auth', () => ({
  login: (...args: unknown[]) => loginApi(...args),
  loginPhone: (...args: unknown[]) => loginPhoneApi(...args),
  logout: (...args: unknown[]) => logoutApi(...args),
  logoutAll: (...args: unknown[]) => logoutAllApi(...args),
  switchAccount: (...args: unknown[]) => switchAccountApi(...args),
  removeAccount: (...args: unknown[]) => removeAccountApi(...args),
  listAccounts: (...args: unknown[]) => listAccounts(...args),
  agentSessionUser: (...args: unknown[]) => agentSessionUser(...args),
  endAgentSession: (...args: unknown[]) => endAgentSession(...args),
}));

function account(id: string, extra: Record<string, unknown> = {}) {
  return {
    id,
    email: `${id}@example.test`,
    role: 'free',
    active: false,
    signed_in: true,
    ...extra,
  };
}

function me(id: string) {
  return { id, email: `${id}@example.test`, role: 'free', is_admin: false };
}

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
  loginApi.mockReset().mockResolvedValue({ user: { id: 'next-user' } });
  loginPhoneApi.mockReset().mockResolvedValue({ user: { id: 'phone-user' } });
  logoutApi.mockReset().mockResolvedValue(undefined);
  logoutAllApi.mockReset().mockResolvedValue(undefined);
  switchAccountApi.mockReset().mockResolvedValue({ user: { id: 'other-user' } });
  removeAccountApi.mockReset().mockResolvedValue({ accounts: [], max_accounts: 5 });
  listAccounts
    .mockReset()
    .mockResolvedValue({ active_user_id: null, accounts: [], max_accounts: 5 });
  agentSessionUser.mockReset().mockResolvedValue(undefined);
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

describe('AuthProvider with several accounts on the browser', () => {
  async function signedInAs(id: string, others: string[] = []) {
    getMe.mockResolvedValue(me(id));
    listAccounts.mockResolvedValue({
      active_user_id: id,
      accounts: [account(id, { active: true }), ...others.map((other) => account(other))],
      max_accounts: 5,
    });
    await renderWithAgents('/agents');
    await waitFor(() => expect(controller.accounts).toHaveLength(1 + others.length));
  }

  it('keeps the agent session when another account joins the browser', async () => {
    await signedInAs('first-user');

    await act(() => controller.login('second@example.test', 'pw'));

    expect(endAgentSession).not.toHaveBeenCalled();
  });

  it('switches without a password and drops the previous account’s cache', async () => {
    await signedInAs('first-user', ['other-user']);
    queryClient.setQueryData(['apiKeys'], [{ key_prefix: 'sk-first' }]);
    getMe.mockResolvedValue(me('other-user'));

    await act(() => controller.switchAccount('other-user'));

    expect(switchAccountApi).toHaveBeenCalledWith('other-user');
    expect(queryClient.getQueryData(['apiKeys'])).toBeUndefined();
    expect(controller.state.user?.id).toBe('other-user');
    expect(endAgentSession).not.toHaveBeenCalled();
  });

  it('ends the agent session on logout only when it is the departing account’s', async () => {
    await signedInAs('first-user', ['other-user']);
    agentSessionUser.mockResolvedValueOnce('other-user');

    await act(() => controller.logout());

    expect(endAgentSession).not.toHaveBeenCalled();
    expect(controller.state.isAuthenticated).toBe(false);
  });

  it('ends it on logout when it is that account’s, or when that cannot be told', async () => {
    await signedInAs('first-user', ['other-user']);
    agentSessionUser.mockResolvedValueOnce('first-user');
    await act(() => controller.logout());
    expect(endAgentSession).toHaveBeenCalledTimes(1);

    cleanup();
    endAgentSession.mockClear();
    await signedInAs('first-user', ['other-user']);
    agentSessionUser.mockResolvedValueOnce(undefined);
    await act(() => controller.logout());
    expect(endAgentSession).toHaveBeenCalledTimes(1);
  });

  it('removes another account without signing this one out', async () => {
    await signedInAs('first-user', ['other-user']);
    removeAccountApi.mockResolvedValueOnce({
      accounts: [account('first-user', { active: true })],
      max_accounts: 5,
    });
    agentSessionUser.mockResolvedValueOnce('other-user');

    await act(() => controller.removeAccount('other-user'));

    expect(removeAccountApi).toHaveBeenCalledWith('other-user');
    expect(controller.accounts.map((entry) => entry.id)).toEqual(['first-user']);
    expect(controller.state.user?.id).toBe('first-user');
    expect(endAgentSession).toHaveBeenCalledTimes(1);
  });

  it('signs every account out and ends the agent session', async () => {
    await signedInAs('first-user', ['other-user']);

    await act(() => controller.logoutAll());

    expect(logoutAllApi).toHaveBeenCalledTimes(1);
    expect(endAgentSession).toHaveBeenCalledWith('/agents');
    expect(controller.state.isAuthenticated).toBe(false);
  });

  it('names a sign-out in another tab differently from a switch', async () => {
    await renderWithAgents('/agents');

    act(() => {
      window.dispatchEvent(new CustomEvent(ACCOUNT_CHANGED_EVENT, { detail: 'signed-out' }));
    });

    expect(screen.getByRole('alertdialog')).toHaveTextContent('You signed out in another tab');
  });
});

describe('AuthProvider and the browser’s other tabs', () => {
  // Another tab of the same browser, as BroadcastChannel sees one.
  function otherTab() {
    return new BroadcastChannel('hybridinference:accounts');
  }

  it('stops this tab when another one switches the browser to a different account', async () => {
    getMe.mockResolvedValue(me('first-user'));
    await renderWithAgents('/agents');
    await waitFor(() => expect(controller.state.user?.id).toBe('first-user'));

    const tab = otherTab();
    tab.postMessage({ type: 'active', userId: 'other-user' });
    tab.close();

    await waitFor(() =>
      expect(screen.getByRole('alertdialog')).toHaveTextContent(
        'You switched accounts in another tab',
      ),
    );
  });

  it('says signed out when another tab signs this account out', async () => {
    getMe.mockResolvedValue(me('first-user'));
    await renderWithAgents('/agents');
    await waitFor(() => expect(controller.state.user?.id).toBe('first-user'));

    const tab = otherTab();
    tab.postMessage({ type: 'active', userId: null });
    tab.close();

    await waitFor(() =>
      expect(screen.getByRole('alertdialog')).toHaveTextContent('You signed out in another tab'),
    );
  });

  it('takes up an account signed in elsewhere when this tab had none', async () => {
    await renderWithAgents('/agents');
    getMe.mockResolvedValue(me('new-user'));

    const tab = otherTab();
    tab.postMessage({ type: 'active', userId: 'new-user' });
    tab.close();

    await waitFor(() => expect(controller.state.user?.id).toBe('new-user'));
    expect(screen.queryByRole('alertdialog')).toBeNull();
  });

  it('does not hear its own announcements', async () => {
    getMe.mockResolvedValue(me('first-user'));
    await renderWithAgents('/agents');
    getMe.mockResolvedValue(me('other-user'));

    await act(() => controller.switchAccount('other-user'));
    await new Promise((resolve) => setTimeout(resolve, 20));

    expect(screen.queryByRole('alertdialog')).toBeNull();
    expect(controller.state.user?.id).toBe('other-user');
  });
});
