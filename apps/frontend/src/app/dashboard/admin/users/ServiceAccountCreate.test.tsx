// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ServiceAccountCreate } from './ServiceAccountCreate';

/**
 * The New service account dialog confirms the key it issues the way every
 * other guarded change is confirmed: a password only when the administrator
 * has neither an authenticator app nor a provider-only sign-in.
 */

// The signed-in administrator's own second factor, as the provider reports it.
const twoFactor = vi.hoisted(() => ({ status: undefined as { enabled: boolean } | undefined }));
vi.mock('@/components/providers/TwoFactorProvider', () => ({
  useTwoFactor: () => ({ status: twoFactor.status, isPending: false, refresh: async () => {} }),
}));

/** The administrator's single sign-on confirmation; none unless a test sets one. */
const adminSso = vi.hoisted(() => ({
  current: null as null | {
    provider: { id: string; name: string };
    purpose: 'admin_action';
    confirmed: boolean;
    draft: string | null;
    settle: (error?: unknown) => void;
    returnTo: string;
    userId: string;
  },
}));
vi.mock('@/components/features/admin/confirm/adminSsoReauth', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/components/features/admin/confirm/adminSsoReauth')>()),
  useAdminSsoReauth: () => adminSso.current,
  useSpendAdminSso:
    () =>
    async <T,>(write: () => Promise<T>): Promise<T> => {
      try {
        const result = await write();
        adminSso.current?.settle();
        return result;
      } catch (error) {
        adminSso.current?.settle(error);
        throw error;
      }
    },
}));

vi.mock('@/lib/api/admin', () => ({ createServiceAccount: vi.fn() }));
import { createServiceAccount } from '@/lib/api/admin';

const created = {
  user_id: 'u-bot',
  login_name: 'nightly-bot',
  name: 'Nightly bot',
  role: 'free',
  api_key: 'hyi-secret-key',
  key_prefix: 'hyi-secret-k',
  warning: 'save it',
};

function open() {
  const onCreated = vi.fn();
  render(<ServiceAccountCreate onClose={() => {}} onCreated={onCreated} />);
  fireEvent.change(screen.getByPlaceholderText('Nightly eval bot'), {
    target: { value: 'Nightly bot' },
  });
  return onCreated;
}

function submit() {
  fireEvent.click(screen.getByRole('button', { name: 'Create account + key' }));
}

describe('ServiceAccountCreate', () => {
  beforeEach(() => {
    twoFactor.status = { enabled: false };
    adminSso.current = null;
    vi.mocked(createServiceAccount).mockReset();
    vi.mocked(createServiceAccount).mockResolvedValue(created);
  });
  afterEach(() => cleanup());

  it('asks an administrator with only a password for it, and sends it', async () => {
    const onCreated = open();
    submit();
    expect(await screen.findByText('Enter your password to create this account.')).toBeVisible();
    expect(createServiceAccount).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText('Your admin password'), {
      target: { value: 'hunter2' },
    });
    submit();
    await waitFor(() => expect(onCreated).toHaveBeenCalled());
    expect(createServiceAccount).toHaveBeenCalledWith({
      name: 'Nightly bot',
      login_name: undefined,
      role: 'free',
      password: 'hunter2',
    });
    expect(await screen.findByText('hyi-secret-key')).toBeVisible();
  });

  it('asks an administrator who has an authenticator app for no password', async () => {
    twoFactor.status = { enabled: true };
    const onCreated = open();
    expect(screen.queryByLabelText('Your admin password')).toBeNull();
    expect(screen.getByText(/A code from your authenticator app confirms this/)).toBeVisible();

    submit();
    await waitFor(() => expect(onCreated).toHaveBeenCalled());
    expect(createServiceAccount).toHaveBeenCalledWith(
      expect.objectContaining({ password: undefined }),
    );
  });

  it('lets an administrator who signs in only through single sign-on confirm there', async () => {
    const settle = vi.fn();
    adminSso.current = {
      provider: { id: 'okta', name: 'Okta' },
      purpose: 'admin_action',
      confirmed: true,
      draft: null,
      settle,
      returnTo: '/dashboard/admin/users',
      userId: 'u-admin',
    };
    const onCreated = open();
    expect(screen.queryByLabelText('Your admin password')).toBeNull();
    expect(screen.getByRole('status')).toHaveTextContent(/confirmed/i);

    submit();
    await waitFor(() => expect(onCreated).toHaveBeenCalled());
    expect(createServiceAccount).toHaveBeenCalledWith(
      expect.objectContaining({ password: undefined }),
    );
    // The confirmation is spent on this write.
    expect(settle).toHaveBeenCalledTimes(1);
  });

  it('spends the single sign-on confirmation when the gateway refuses, and shows why', async () => {
    const settle = vi.fn();
    adminSso.current = {
      provider: { id: 'okta', name: 'Okta' },
      purpose: 'admin_action',
      confirmed: true,
      draft: null,
      settle,
      returnTo: '/dashboard/admin/users',
      userId: 'u-admin',
    };
    const refusal = new Error("Login name 'nightly-bot' is already in use");
    vi.mocked(createServiceAccount).mockRejectedValue(refusal);
    const onCreated = open();
    submit();
    expect(await screen.findByText("Login name 'nightly-bot' is already in use")).toBeVisible();
    expect(settle).toHaveBeenCalledWith(refusal);
    expect(onCreated).not.toHaveBeenCalled();
  });
});
