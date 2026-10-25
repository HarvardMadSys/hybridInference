// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import toast from 'react-hot-toast';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { ProviderKeysTab } from '@/app/dashboard/admin/ProviderKeysTab';
import { buildTimeSiteConfig, withAgentsUrl } from '@/config/site-config';
import { ACCOUNT_CHANGED_EVENT, AUTH_EXPIRED_EVENT } from '@/lib/api/client';
import { useAccountToast } from '@/lib/hooks/useAccountToast';
import { AuthProvider, useAuth } from './AuthProvider';
import { SiteConfigProvider } from './SiteConfigProvider';
import { chooseValue } from '@/test-utils/select';
import { ToastProvider } from './ToastProvider';

const api = vi.hoisted(() => ({
  getMe: vi.fn(),
  setProviderKeyMinRole: vi.fn(),
  switchAccount: vi.fn(),
}));
vi.mock('@/lib/api/user', () => ({ getMe: api.getMe }));
vi.mock('@/lib/api/auth', () => ({
  login: vi.fn().mockResolvedValue({ user: { id: 'admin-user' } }),
  loginPhone: vi.fn(),
  logout: vi.fn(),
  logoutAll: vi.fn(),
  switchAccount: api.switchAccount,
  removeAccount: vi.fn(),
  agentSessionUser: vi.fn(),
  endAgentSession: vi.fn(),
  listAccounts: vi.fn().mockResolvedValue({ accounts: [], max_accounts: 5 }),
}));
vi.mock('@/lib/api/admin', () => ({
  addProviderKey: vi.fn(),
  deleteProviderKey: vi.fn(),
  disableProviderEnvKey: vi.fn(),
  enableProviderEnvKey: vi.fn(),
  setProviderEnvKeyMinRole: vi.fn(),
  setProviderKeyStatus: vi.fn(),
  verifyProviderKey: vi.fn(),
  listProviderKeyProviders: vi.fn().mockResolvedValue({ providers: ['zai'] }),
  listProviderKeys: vi.fn().mockResolvedValue({
    provider: 'zai',
    keys: [
      {
        id: 'key-1',
        provider: 'zai',
        key_prefix: 'fixture-key-prefix',
        label: 'test',
        source: 'db',
        status: 'active',
        created_at: null,
        min_role: 'pro',
      },
    ],
  }),
  setProviderKeyMinRole: api.setProviderKeyMinRole,
}));

const admin = { id: 'admin-user', email: 'admin@example.test', role: 'admin', is_admin: true };
const normal = { id: 'normal-user', email: 'normal@example.test', role: 'free', is_admin: false };
const SUCCESS = 'Key reserved for internal and above';
const FAILURE = 'Tier change failed: old administrator request failed';

let controller: ReturnType<typeof useAuth>;
function Page() {
  controller = useAuth();
  const accountToast = useAccountToast();
  return (
    <>
      <span data-testid="current-account">{controller.state.user?.id}</span>
      <button onClick={() => accountToast.success('Current account action')}>Notify</button>
      {controller.state.user?.is_admin ? <ProviderKeysTab initialProvider="zai" /> : null}
    </>
  );
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return { promise, resolve, reject };
}

beforeEach(() => {
  toast.remove();
  vi.stubGlobal('matchMedia', () => ({
    matches: true,
    addEventListener() {},
    removeEventListener() {},
  }));
  api.getMe.mockReset().mockResolvedValue(admin);
  api.switchAccount.mockReset().mockResolvedValue({ user: { id: normal.id } });
  api.setProviderKeyMinRole.mockReset();
});
afterEach(() => {
  cleanup();
  toast.remove();
  vi.unstubAllGlobals();
});

async function startAdminMutation() {
  const pending = deferred<{ pools_updated: number }>();
  api.setProviderKeyMinRole.mockReturnValueOnce(pending.promise);
  render(
    <SiteConfigProvider initialConfig={withAgentsUrl(buildTimeSiteConfig, '')}>
      <QueryClientProvider client={new QueryClient()}>
        <AuthProvider>
          <Page />
          <ToastProvider />
        </AuthProvider>
      </QueryClientProvider>
    </SiteConfigProvider>,
  );
  const selector = await screen.findByLabelText('Reserved tier for key fixture-key-prefix');
  chooseValue(selector, 'internal');
  expect(api.setProviderKeyMinRole).toHaveBeenCalledWith('key-1', 'internal');
  return pending;
}

async function switchTo(user: typeof admin) {
  api.getMe.mockResolvedValue(user);
  await act(async () => {
    await controller.switchAccount(user.id);
    toast.success(`Switched to ${user.id}`);
  });
  expect(screen.getByTestId('current-account')).toHaveTextContent(user.id);
}

async function finish(
  pending: ReturnType<typeof deferred<{ pools_updated: number }>>,
  result: string,
) {
  await act(async () => {
    if (result === 'success') pending.resolve({ pools_updated: 1 });
    else pending.reject(new Error('old administrator request failed'));
  });
}

function expectNoOldNotification() {
  expect(screen.queryByText(SUCCESS)).toBeNull();
  expect(screen.queryByText(FAILURE)).toBeNull();
}

it.each(['success', 'error'])(
  'ignores a late %s after switching to a normal user',
  async (result) => {
    const pending = await startAdminMutation();
    await switchTo(normal);
    expect(screen.queryByLabelText('Reserved tier for key fixture-key-prefix')).toBeNull();
    await finish(pending, result);
    expectNoOldNotification();
    expect(screen.getByText('Switched to normal-user')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Notify' }));
    expect(screen.getByText('Current account action')).toBeInTheDocument();
  },
);

it.each(['logout', 'logoutAll', 'other-tab', 'expired'])(
  'ignores late results after %s',
  async (transition) => {
    const pending = await startAdminMutation();
    await act(async () => {
      if (transition === 'logout') await controller.logout();
      else if (transition === 'logoutAll') await controller.logoutAll();
      else if (transition === 'expired') window.dispatchEvent(new Event(AUTH_EXPIRED_EVENT));
      else window.dispatchEvent(new CustomEvent(ACCOUNT_CHANGED_EVENT, { detail: 'switched' }));
    });
    await finish(pending, 'success');
    expectNoOldNotification();
    if (transition === 'other-tab') expect(screen.getByRole('alertdialog')).toBeInTheDocument();
  },
);

it('invalidates the old scope before waiting for the new account to load', async () => {
  const pending = await startAdminMutation();
  const profile = deferred<typeof normal>();
  api.getMe.mockReturnValueOnce(profile.promise);
  let switching!: Promise<void>;
  await act(async () => {
    switching = controller.switchAccount(normal.id);
  });
  // The old account is still rendered while the new profile is in flight.
  expect(screen.getByTestId('current-account')).toHaveTextContent(admin.id);
  await finish(pending, 'success');
  expectNoOldNotification();
  await act(async () => {
    profile.resolve(normal);
    await switching;
  });
  expect(screen.getByTestId('current-account')).toHaveTextContent(normal.id);
});

it('does not revive old work after switching back to the same account', async () => {
  const pending = await startAdminMutation();
  await switchTo(normal);
  await switchTo(admin);
  await finish(pending, 'success');
  expectNoOldNotification();
  fireEvent.click(screen.getByRole('button', { name: 'Notify' }));
  expect(screen.getByText('Current account action')).toBeInTheDocument();
});

it('keeps the current account result when the switch fails', async () => {
  const pending = await startAdminMutation();
  api.switchAccount.mockRejectedValueOnce(new Error('session ended'));
  await act(() => controller.switchAccount(normal.id).catch(() => undefined));
  await finish(pending, 'success');
  expect(screen.getByTestId('current-account')).toHaveTextContent(admin.id);
  expect(screen.getByText(SUCCESS)).toBeInTheDocument();
});

it('keeps in-flight notifications on an ordinary same-account profile refresh', async () => {
  const pending = await startAdminMutation();
  await act(() => controller.refreshUser());
  await finish(pending, 'success');
  expect(screen.getByText(SUCCESS)).toBeInTheDocument();
});

it('starts a fresh scope on a new login even when the user id is unchanged', async () => {
  const pending = await startAdminMutation();
  await act(async () => {
    await controller.login(admin.email, 'fixture-password');
    toast.success('Login successful!');
  });
  await finish(pending, 'success');
  expectNoOldNotification();
  expect(screen.getByText('Login successful!')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Notify' }));
  expect(screen.getByText('Current account action')).toBeInTheDocument();
});

it('does not reactivate an old scope when a profile refresh returns after another tab switches', async () => {
  const pending = await startAdminMutation();
  const profile = deferred<typeof admin>();
  api.getMe.mockReturnValueOnce(profile.promise);
  let refreshing!: Promise<void>;
  await act(async () => {
    refreshing = controller.refreshUser();
  });
  act(() => {
    window.dispatchEvent(new CustomEvent(ACCOUNT_CHANGED_EVENT, { detail: 'switched' }));
  });
  await act(async () => {
    profile.resolve(admin);
    await refreshing;
  });
  await finish(pending, 'success');
  expectNoOldNotification();
  // Even newly invoked handlers on the covered page retain the invalid scope.
  fireEvent.click(screen.getByRole('button', { name: 'Notify' }));
  expect(screen.queryByText('Current account action')).toBeNull();
  expect(screen.getByRole('alertdialog')).toBeInTheDocument();
});
