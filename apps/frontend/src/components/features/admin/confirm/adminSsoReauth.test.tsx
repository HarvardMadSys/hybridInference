// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { useRef, useState } from 'react';

import { AdminSsoReturn } from '@/components/features/admin/AdminSsoReturn';
import { APIError } from '@/lib/utils/errors';
import { ReauthPassword } from './ReauthPassword';
import { ADMIN_REAUTH_MAX_AGE_MS, useSpendAdminSso } from './adminSsoReauth';

/**
 * A single sign-on confirmation holds once on the gateway. Whatever spent it --
 * a change it accepted, or one it refused -- the page must offer the provider
 * again, not keep saying "confirmed" over a dead proof. These use the real
 * scope, helper and field, with a guarded write standing in for any of the
 * Admin's (settings, backup, restore, a role, an identity, an authenticator).
 */

const pathname = vi.hoisted(() => ({ current: '/dashboard/admin/settings' }));
vi.mock('next/navigation', () => ({ usePathname: () => pathname.current }));
vi.mock('@/components/providers', () => ({
  useAuth: () => ({
    state: {
      loading: false,
      isAuthenticated: true,
      user: { id: 'u-admin', is_admin: true, sso_reauth_provider: { id: 'okta', name: 'Okta' } },
    },
  }),
}));
const sso = vi.hoisted(() => ({ startSsoReauth: vi.fn() }));
vi.mock('@/lib/api/sso', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/sso')>()),
  startSsoReauth: sso.startSsoReauth,
}));
vi.mock('@/lib/utils/navigation', () => ({ navigateTo: vi.fn() }));

function GuardedAction({ write }: { write: () => Promise<void> }) {
  const password = useRef('');
  const spend = useSpendAdminSso();
  const [outcome, setOutcome] = useState('');
  return (
    <div>
      <ReauthPassword valueRef={password} label="Your password" />
      <button
        type="button"
        onClick={() =>
          void spend(write).then(
            () => setOutcome('saved'),
            (error: unknown) => setOutcome(error instanceof Error ? error.message : 'failed'),
          )
        }
      >
        Save
      </button>
      <p data-testid="outcome">{outcome}</p>
    </div>
  );
}

function renderBack(write: () => Promise<void>) {
  // The provider sent the browser back confirmed.
  window.history.replaceState(null, '', '/dashboard/admin/settings?reauth=admin_action');
  return render(
    <AdminSsoReturn userId="u-admin">
      <GuardedAction write={write} />
    </AdminSsoReturn>,
  );
}

const confirmed = () => screen.queryByText('Identity confirmed. You can continue.');
const offered = () => screen.queryByRole('button', { name: 'Confirm with Okta' });

beforeEach(() => {
  pathname.current = '/dashboard/admin/settings';
  sso.startSsoReauth.mockReset();
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  window.history.replaceState(null, '', '/');
});

it('offers the provider again after the confirmed change, for the next one', async () => {
  const write = vi.fn(async () => undefined);
  renderBack(write);
  expect(confirmed()).toBeInTheDocument();
  expect(offered()).not.toBeInTheDocument();

  fireEvent.click(screen.getByRole('button', { name: 'Save' }));

  await waitFor(() => expect(screen.getByTestId('outcome')).toHaveTextContent('saved'));
  expect(confirmed()).not.toBeInTheDocument();
  expect(offered()).toBeInTheDocument();
  // The banner the Admin pages show is gone too.
  expect(screen.queryByRole('status')).not.toBeInTheDocument();
});

it('offers the provider again after the gateway refused the change', async () => {
  const write = vi.fn(async () => {
    throw new APIError(
      'BAD_REQUEST',
      "Confirm your identity with your organization's sign-in first.",
      400,
    );
  });
  renderBack(write);

  fireEvent.click(screen.getByRole('button', { name: 'Save' }));

  await waitFor(() => expect(screen.getByTestId('outcome')).toHaveTextContent('organization'));
  expect(offered()).toBeInTheDocument();
});

it('keeps the confirmation when the request never reached the gateway', async () => {
  const write = vi.fn(async () => {
    throw new TypeError('Failed to fetch');
  });
  renderBack(write);

  fireEvent.click(screen.getByRole('button', { name: 'Save' }));

  await waitFor(() => expect(screen.getByTestId('outcome')).toHaveTextContent('Failed to fetch'));
  expect(confirmed()).toBeInTheDocument();
});

it('gives the confirmation up once the gateway would no longer honour it', () => {
  vi.useFakeTimers();
  renderBack(vi.fn(async () => undefined));
  expect(confirmed()).toBeInTheDocument();

  act(() => {
    vi.advanceTimersByTime(ADMIN_REAUTH_MAX_AGE_MS - 1000);
  });
  expect(confirmed()).toBeInTheDocument();
  act(() => {
    vi.advanceTimersByTime(1000);
  });

  expect(confirmed()).not.toBeInTheDocument();
  expect(offered()).toBeInTheDocument();
});

it('sends the provider back to the tab open now, not the one the layout first drew', async () => {
  pathname.current = '/dashboard/admin/users';
  window.history.replaceState(null, '', '/dashboard/admin/users');
  sso.startSsoReauth.mockResolvedValue('https://idp.example/authorize');
  const view = render(
    <AdminSsoReturn userId="u-admin">
      <GuardedAction write={vi.fn(async () => undefined)} />
    </AdminSsoReturn>,
  );

  // The administrator moves to Settings; the Admin layout stays mounted.
  pathname.current = '/dashboard/admin/settings';
  view.rerender(
    <AdminSsoReturn userId="u-admin">
      <GuardedAction write={vi.fn(async () => undefined)} />
    </AdminSsoReturn>,
  );
  fireEvent.click(screen.getByRole('button', { name: 'Confirm with Okta' }));

  await waitFor(() =>
    expect(sso.startSsoReauth).toHaveBeenCalledWith('admin_action', '/dashboard/admin/settings'),
  );
});
