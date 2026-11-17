// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { RuntimeSettingItem } from '@/lib/api/admin';
import { APIError } from '@/lib/utils/errors';
import { RuntimeSettingsSection } from './RuntimeSettingsSection';

const api = vi.hoisted(() => ({
  updateRuntimeSetting: vi.fn(),
  updateRuntimeSettings: vi.fn(),
}));
vi.mock('@/lib/api/admin', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api/admin')>('@/lib/api/admin');
  return { ...actual, ...api };
});

/** How the signed-in administrator confirms: switched per test. */
const confirm = vi.hoisted(() => ({
  twoFactor: false,
  sso: null as null | {
    provider: { id: string; name: string };
    purpose: 'admin_action';
    confirmed: boolean;
    draft: string | null;
    settle: ReturnType<typeof vi.fn>;
    returnTo: string;
    userId: string;
  },
}));
vi.mock('@/components/providers/TwoFactorProvider', () => ({
  useTwoFactor: () => ({
    status: { enabled: confirm.twoFactor },
    isPending: false,
    refresh: vi.fn(),
  }),
}));
vi.mock('@/components/features/admin/confirm/adminSsoReauth', () => ({
  ADMIN_REAUTH_PURPOSE: 'admin_action',
  useAdminSsoReauth: () => confirm.sso,
  AdminSsoReauthScope: ({ children }: { children: unknown }) => children,
}));

function setting(key: string, fields: Partial<RuntimeSettingItem> = {}): RuntimeSettingItem {
  return {
    key,
    value: false,
    value_type: 'bool',
    default_value: false,
    description: `${key} description`,
    ...fields,
  };
}

const SETTINGS = [
  setting('signup_require_email_verification', { value: true, security_relevant: true }),
  setting('user_daily_quota_free', {
    value: 100,
    value_type: 'float',
    default_value: 100,
    min: 0,
    security_relevant: true,
  }),
  setting('analytics_growth_rate_enabled'),
];

function renderSection() {
  const onSaved = vi.fn();
  const onAfterWrite = vi.fn();
  const onToast = vi.fn();
  render(
    <RuntimeSettingsSection
      id="settings-test"
      title="Access"
      description="Who may sign in"
      settings={SETTINGS}
      loading={false}
      onSaved={onSaved}
      onAfterWrite={onAfterWrite}
      onToast={onToast}
    />,
  );
  return { onSaved, onAfterWrite, onToast };
}

const toggle = (label: string) => fireEvent.click(screen.getByRole('switch', { name: label }));
const save = () => fireEvent.click(screen.getByRole('button', { name: /^Save/ }));

beforeEach(() => {
  confirm.twoFactor = false;
  confirm.sso = null;
  api.updateRuntimeSetting.mockReset();
  api.updateRuntimeSettings.mockReset();
});
afterEach(cleanup);

describe('RuntimeSettingsSection, a guarded setting', () => {
  it('saves a display switch at once, with no confirmation', async () => {
    api.updateRuntimeSetting.mockResolvedValue(
      setting('analytics_growth_rate_enabled', { value: true }),
    );
    const { onToast } = renderSection();

    toggle('Toggle Growth Rate');
    save();

    await waitFor(() =>
      expect(api.updateRuntimeSetting).toHaveBeenCalledWith('analytics_growth_rate_enabled', true),
    );
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(api.updateRuntimeSettings).not.toHaveBeenCalled();
    expect(onToast).toHaveBeenCalledWith('analytics_growth_rate_enabled enabled');
  });

  it('asks for the password, then writes the changes together', async () => {
    api.updateRuntimeSettings.mockResolvedValue([
      setting('signup_require_email_verification', { value: false, security_relevant: true }),
      setting('analytics_growth_rate_enabled', { value: true }),
    ]);
    const { onSaved, onAfterWrite } = renderSection();

    toggle('Toggle signup require email verification');
    toggle('Toggle Growth Rate');
    save();

    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText('signup_require_email_verification')).toBeInTheDocument();
    expect(api.updateRuntimeSettings).not.toHaveBeenCalled();

    // Confirmed with no password: refused here, nothing sent.
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save changes' }));
    expect(
      await within(dialog).findByText('Enter your password to save these settings.'),
    ).toBeInTheDocument();
    expect(api.updateRuntimeSettings).not.toHaveBeenCalled();

    fireEvent.change(within(dialog).getByLabelText('Your password'), {
      target: { value: 'admin-password' },
    });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save changes' }));

    await waitFor(() =>
      expect(api.updateRuntimeSettings).toHaveBeenCalledWith(
        { signup_require_email_verification: false, analytics_growth_rate_enabled: true },
        'admin-password',
      ),
    );
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(onSaved).toHaveBeenCalledTimes(2);
    expect(onAfterWrite).toHaveBeenCalledTimes(1);
    expect(api.updateRuntimeSetting).not.toHaveBeenCalled();
  });

  it('keeps a wrong password inside the dialog, with the drafts', async () => {
    api.updateRuntimeSettings.mockRejectedValue(
      new APIError('BAD_REQUEST', 'Current password is incorrect.', 400),
    );
    const { onSaved } = renderSection();

    fireEvent.change(screen.getByRole('spinbutton', { name: 'user_daily_quota_free value' }), {
      target: { value: '5' },
    });
    save();
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText('Your password'), {
      target: { value: 'guess' },
    });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save changes' }));

    expect(await within(dialog).findByText('Current password is incorrect.')).toBeInTheDocument();
    expect(onSaved).not.toHaveBeenCalled();
    expect(screen.getByRole('spinbutton', { name: 'user_daily_quota_free value' })).toHaveValue(5);
  });

  it('asks an administrator with an authenticator for no password', async () => {
    confirm.twoFactor = true;
    api.updateRuntimeSettings.mockResolvedValue([
      setting('signup_require_email_verification', { value: false, security_relevant: true }),
    ]);
    renderSection();

    toggle('Toggle signup require email verification');
    save();
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).queryByLabelText('Your password')).not.toBeInTheDocument();
    expect(within(dialog).getByText(/code from your authenticator app/)).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save changes' }));

    await waitFor(() =>
      expect(api.updateRuntimeSettings).toHaveBeenCalledWith(
        { signup_require_email_verification: false },
        undefined,
      ),
    );
  });

  it('offers a single sign-on administrator the provider, and spends its confirmation once', async () => {
    const settle = vi.fn();
    confirm.sso = {
      provider: { id: 'okta', name: 'Okta' },
      purpose: 'admin_action',
      confirmed: false,
      draft: null,
      settle,
      returnTo: '/dashboard/admin/settings',
      userId: 'u-admin',
    };
    renderSection();

    toggle('Toggle signup require email verification');
    save();
    let dialog = await screen.findByRole('dialog');
    expect(within(dialog).queryByLabelText('Your password')).not.toBeInTheDocument();
    expect(within(dialog).getByRole('button', { name: 'Confirm with Okta' })).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    cleanup();

    // Back from the provider, confirmed: the change made again needs no password.
    confirm.sso = { ...confirm.sso, confirmed: true };
    api.updateRuntimeSettings.mockResolvedValue([
      setting('signup_require_email_verification', { value: false, security_relevant: true }),
    ]);
    renderSection();
    toggle('Toggle signup require email verification');
    save();
    dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText('Identity confirmed. You can continue.')).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save changes' }));

    await waitFor(() =>
      expect(api.updateRuntimeSettings).toHaveBeenCalledWith(
        { signup_require_email_verification: false },
        undefined,
      ),
    );
    await waitFor(() => expect(settle).toHaveBeenCalledWith());
  });

  it('gives the single sign-on confirmation up when the gateway refuses the change', async () => {
    const settle = vi.fn();
    confirm.sso = {
      provider: { id: 'okta', name: 'Okta' },
      purpose: 'admin_action',
      confirmed: true,
      draft: null,
      settle,
      returnTo: '/dashboard/admin/settings',
      userId: 'u-admin',
    };
    const refusal = new APIError(
      'BAD_REQUEST',
      "Confirm your identity with your organization's sign-in first.",
      400,
    );
    api.updateRuntimeSettings.mockRejectedValue(refusal);
    renderSection();

    toggle('Toggle signup require email verification');
    save();
    const dialog = await screen.findByRole('dialog');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save changes' }));

    expect(
      await within(dialog).findByText(
        "Confirm your identity with your organization's sign-in first.",
      ),
    ).toBeInTheDocument();
    expect(settle).toHaveBeenCalledWith(refusal);
  });
});
