// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import toast from 'react-hot-toast';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AuthUser } from '@/components/providers/AuthProvider';
import type { SsoIdentity } from '@/lib/api/sso';
import { APIError } from '@/lib/utils/errors';

let user: AuthUser;
const refreshUser = vi.hoisted(() => vi.fn());
vi.mock('@/components/providers', () => ({
  useAuth: () => ({ state: { loading: false, isAuthenticated: true, user }, refreshUser }),
}));
const api = vi.hoisted(() => ({
  bindEmail: vi.fn(),
  sendPhoneCode: vi.fn(),
  updatePassword: vi.fn(),
}));
// The requests single sign-on makes; what reads a code or a refusal stays real.
const sso = vi.hoisted(() => ({
  listSsoIdentities: vi.fn(),
  startSsoReauth: vi.fn(),
  linkSsoIdentity: vi.fn(),
  unlinkSsoIdentity: vi.fn(),
}));
vi.mock('@/lib/api/sso', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/sso')>()),
  ...sso,
}));
// jsdom's location cannot be watched; the page leaves through this.
const navigateTo = vi.hoisted(() => vi.fn());
vi.mock('@/lib/utils/navigation', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/utils/navigation')>()),
  navigateTo,
}));
vi.mock('react-hot-toast', () => ({ default: { success: vi.fn(), error: vi.fn() } }));
vi.mock('@/lib/api/auth', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/auth')>()),
  bindEmail: api.bindEmail,
  sendPhoneCode: api.sendPhoneCode,
}));
vi.mock('@/lib/api/user', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/user')>()),
  updatePassword: api.updatePassword,
}));
vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), refresh: vi.fn() }),
}));
vi.mock('@/components/features/auth/ProtectedRoute', () => ({
  ProtectedRoute: ({ children }: { children: React.ReactNode }) => children,
}));

import { SiteConfigProvider } from '@/components/providers/SiteConfigProvider';
import { buildTimeSiteConfig } from '@/config/site-config';
import SettingsPage from './page';

beforeEach(() => {
  api.bindEmail.mockReset().mockResolvedValue({
    message: 'sent',
    email: 'new@e2e-mail.org',
    email_verified: false,
  });
  api.sendPhoneCode.mockReset().mockResolvedValue({ message: 'sent', retry_after: 60 });
  api.updatePassword.mockReset().mockResolvedValue({ message: 'Password changed successfully.' });
  sso.listSsoIdentities.mockReset().mockResolvedValue([]);
  sso.startSsoReauth
    .mockReset()
    .mockResolvedValue('https://idp.example.edu/authorize?prompt=login');
  sso.linkSsoIdentity.mockReset().mockResolvedValue('https://idp.example.edu/authorize?state=link');
  sso.unlinkSsoIdentity.mockReset().mockResolvedValue({ message: 'Single sign-on disconnected.' });
  refreshUser.mockReset().mockResolvedValue(undefined);
  navigateTo.mockReset();
  vi.mocked(toast.success).mockReset();
  vi.mocked(toast.error).mockReset();
  window.sessionStorage.clear();
  arriveAt('/dashboard/settings');
  user = {
    id: 'email-user',
    email: 'user@example.test',
    role: 'free',
    is_admin: false,
    has_password: true,
  };
});
afterEach(cleanup);

/** The address the page is loaded at: how a round trip to a provider comes back. */
function arriveAt(url: string) {
  window.history.replaceState(null, '', url);
}

function renderSettings(phoneAuth: boolean, ssoOffered = false) {
  return render(
    <SiteConfigProvider
      initialConfig={{
        ...buildTimeSiteConfig,
        features: { ...buildTimeSiteConfig.features, phoneAuth },
        sso: { enabled: ssoOffered },
      }}
    >
      <SettingsPage />
    </SiteConfigProvider>,
  );
}

describe('account phone binding', () => {
  it('keeps the existing account forms and hides binding when SMS is disabled', () => {
    const { container } = renderSettings(false);
    expect(screen.getByText('Nickname')).toBeInTheDocument();
    expect(container.querySelector('input[name="userName"]')).toHaveAttribute(
      'autocomplete',
      'nickname',
    );
    expect(
      screen.getByText(/Changing your nickname does not change how you log in/),
    ).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Change Password' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Bind mobile number' })).not.toBeInTheDocument();
  });

  it('offers binding for an existing email account when SMS is enabled', () => {
    renderSettings(true);
    expect(screen.getByDisplayValue('user@example.test')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Change Email' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Bind mobile number' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Change Password' })).toBeInTheDocument();
  });

  it('shows verified phone identity, offers email binding and password setup, and retains profile settings for a phone-only account', () => {
    user = {
      ...user,
      email: null,
      phone: '+8613812345678',
      phone_verified: true,
      has_password: false,
    };
    renderSettings(true);
    expect(screen.getByText('Verified mobile number: +8613812345678')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Send verification email' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Set Password' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Change Nickname' })).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Change Password' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Bind mobile number' })).not.toBeInTheDocument();
  });
});

describe('an account that signs in with a login name', () => {
  const setupAdmin = () => ({
    ...user,
    id: 'setup-admin',
    email: null,
    login_name: 'admin',
    role: 'admin',
    is_admin: true,
    has_password: true,
  });
  const emailSection = () =>
    screen.getByRole('heading', { name: 'Email Address' }).closest('section')!;

  it('binds a first email after confirming the current password', async () => {
    user = setupAdmin();
    renderSettings(false);
    const section = emailSection();
    fireEvent.change(section.querySelector('input[type=email]')!, {
      target: { value: 'admin@e2e-mail.org' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Send verification email' }));
    expect(
      await screen.findByText('Enter your current password to confirm this change.'),
    ).toBeInTheDocument();
    expect(api.bindEmail).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText(/^Current password/), { target: { value: 'Secret-1' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send verification email' }));
    await waitFor(() =>
      expect(api.bindEmail).toHaveBeenCalledWith({
        email: 'admin@e2e-mail.org',
        password: 'Secret-1',
      }),
    );
  });

  it('changes an email it has already bound', () => {
    user = { ...setupAdmin(), email: 'admin@e2e-mail.org' };
    renderSettings(false);
    expect(emailSection()).toContainElement(screen.getByDisplayValue('admin@e2e-mail.org'));
    expect(screen.getByRole('button', { name: 'Change Email' })).toBeInTheDocument();
  });

  it('confirms with its password, not a code, when it also has a verified phone', async () => {
    user = { ...setupAdmin(), phone: '+8613812345678', phone_verified: true };
    renderSettings(true);
    const section = emailSection();
    expect(section.querySelector('input[inputmode=numeric]')).toBeNull();
    fireEvent.change(section.querySelector('input[type=email]')!, {
      target: { value: 'admin@e2e-mail.org' },
    });
    fireEvent.change(screen.getByLabelText(/^Current password/), { target: { value: 'Secret-1' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send verification email' }));
    await waitFor(() =>
      expect(api.bindEmail).toHaveBeenCalledWith({
        email: 'admin@e2e-mail.org',
        password: 'Secret-1',
      }),
    );
    expect(api.sendPhoneCode).not.toHaveBeenCalled();
  });
});

describe('changes that need the owner', () => {
  const phoneOnly = () => ({
    ...user,
    email: null,
    phone: '+8613812345678',
    phone_verified: true,
    has_password: false,
  });

  it('asks a password account for its current password before changing the email', async () => {
    renderSettings(false);
    fireEvent.change(screen.getByDisplayValue('user@example.test'), {
      target: { value: 'new@e2e-mail.org' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Change Email' }));
    expect(
      await screen.findByText('Enter your current password to confirm this change.'),
    ).toBeInTheDocument();
    expect(api.bindEmail).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText(/^Current password/), { target: { value: 'Secret-1' } });
    fireEvent.click(screen.getByRole('button', { name: 'Change Email' }));
    await waitFor(() =>
      expect(api.bindEmail).toHaveBeenCalledWith({
        email: 'new@e2e-mail.org',
        password: 'Secret-1',
      }),
    );
  });

  it('confirms a phone-only account binding an email with a bind_email code', async () => {
    user = phoneOnly();
    renderSettings(true);
    const emailSection = screen.getByRole('heading', { name: 'Email Address' }).closest('section')!;
    const sendButton = [...emailSection.querySelectorAll('button')].find(
      (button) => button.textContent === 'Send verification code',
    )!;
    fireEvent.click(sendButton);
    await waitFor(() =>
      expect(api.sendPhoneCode).toHaveBeenCalledWith({
        phone: '+8613812345678',
        purpose: 'bind_email',
        turnstileToken: undefined,
      }),
    );
    fireEvent.change(emailSection.querySelector('input[type=email]')!, {
      target: { value: 'new@e2e-mail.org' },
    });
    fireEvent.change(emailSection.querySelector('input[inputmode=numeric]')!, {
      target: { value: '123456' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Send verification email' }));
    await waitFor(() =>
      expect(api.bindEmail).toHaveBeenCalledWith({
        email: 'new@e2e-mail.org',
        phone_code: '123456',
      }),
    );
  });

  it('confirms a first password with a set_password code', async () => {
    user = phoneOnly();
    renderSettings(true);
    const section = screen.getByRole('heading', { name: 'Set Password' }).closest('section')!;
    fireEvent.change(screen.getByLabelText(/^New Password/), { target: { value: 'Brand-New-1' } });
    fireEvent.change(screen.getByLabelText(/^Confirm New Password/), {
      target: { value: 'Brand-New-1' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Set Password' }));
    expect(
      await screen.findByText('Enter the verification code sent to your phone.'),
    ).toBeInTheDocument();
    expect(api.updatePassword).not.toHaveBeenCalled();

    fireEvent.change(section.querySelector('input[inputmode=numeric]')!, {
      target: { value: '654321' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Set Password' }));
    await waitFor(() =>
      expect(api.updatePassword).toHaveBeenCalledWith('', 'Brand-New-1', '654321'),
    );
  });
});

// An account created by single sign-on has no password and no phone. It
// confirms a sensitive action by signing in at its identity provider again.
describe('confirming through the identity provider', () => {
  const PROVIDER = { id: 'example-univ', name: 'Example University' };
  const ssoOnly = (): AuthUser => ({
    id: 'sso-user',
    email: 'carol@example.edu',
    role: 'free',
    is_admin: false,
    has_password: false,
    sso_reauth_provider: PROVIDER,
  });
  const section = (name: string) => screen.getByRole('heading', { name }).closest('section')!;
  const confirmButton = (within_: HTMLElement) =>
    within(within_).getByRole('button', { name: 'Confirm with Example University' });
  const draft = (purpose: string, value: string, userId = 'sso-user') =>
    window.sessionStorage.setItem(
      'sso-reauth-draft',
      JSON.stringify({ purpose, userId, value, savedAt: Date.now() }),
    );

  beforeEach(() => {
    user = ssoOnly();
  });

  it('offers the provider in place of a password or a code', () => {
    renderSettings(true, true);
    const email = section('Email Address');

    expect(confirmButton(email)).toBeInTheDocument();
    expect(within(email).queryByLabelText(/^Current password/)).not.toBeInTheDocument();
    expect(email.querySelector('input[inputmode=numeric]')).toBeNull();
    // A first password can be set: the provider confirms that too.
    expect(confirmButton(section('Set Password'))).toBeInTheDocument();
    expect(screen.queryByText('Identity confirmed. You can continue.')).not.toBeInTheDocument();
  });

  it('leaves for the provider, for this action, to come back to this page', async () => {
    renderSettings(false, true);
    const email = section('Email Address');
    fireEvent.change(email.querySelector('input[type=email]')!, {
      target: { value: 'new@e2e-mail.org' },
    });

    fireEvent.click(confirmButton(email));

    await waitFor(() =>
      expect(navigateTo).toHaveBeenCalledWith('https://idp.example.edu/authorize?prompt=login'),
    );
    expect(sso.startSsoReauth).toHaveBeenCalledExactlyOnceWith('bind_email', '/dashboard/settings');
    // What was typed waits in this tab for the page that comes back.
    expect(JSON.parse(window.sessionStorage.getItem('sso-reauth-draft')!)).toMatchObject({
      purpose: 'bind_email',
      userId: 'sso-user',
      value: 'new@e2e-mail.org',
    });
    expect(api.bindEmail).not.toHaveBeenCalled();
  });

  it('asks for the confirmation before sending anything', async () => {
    renderSettings(false, true);
    const email = section('Email Address');
    fireEvent.change(email.querySelector('input[type=email]')!, {
      target: { value: 'new@e2e-mail.org' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Change Email' }));

    expect(
      await screen.findByText('Confirm your identity with Example University to continue.'),
    ).toBeInTheDocument();
    expect(api.bindEmail).not.toHaveBeenCalled();
  });

  it('comes back confirmed, with what was typed, and sends no proof of its own', async () => {
    draft('bind_email', 'new@e2e-mail.org');
    arriveAt('/dashboard/settings?reauth=bind_email');
    renderSettings(false, true);
    const email = section('Email Address');

    expect(within(email).getByText('Identity confirmed. You can continue.')).toBeInTheDocument();
    expect(within(email).queryByRole('button', { name: /^Confirm with/ })).not.toBeInTheDocument();
    expect(email.querySelector('input[type=email]')).toHaveValue('new@e2e-mail.org');
    // Only this action is confirmed.
    expect(confirmButton(section('Set Password'))).toBeInTheDocument();
    // Read once: a reload must not confirm anything again.
    expect(window.location.search).toBe('');
    expect(window.sessionStorage.getItem('sso-reauth-draft')).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: 'Change Email' }));

    await waitFor(() => expect(api.bindEmail).toHaveBeenCalledWith({ email: 'new@e2e-mail.org' }));
    // It held once: the next change is confirmed again.
    expect(await within(email).findByRole('button', { name: /^Confirm with/ })).toBeInTheDocument();
  });

  it('asks again after a request the gateway refused, which used the confirmation up', async () => {
    arriveAt('/dashboard/settings?reauth=bind_email');
    api.bindEmail.mockRejectedValue(
      new APIError('UNKNOWN_ERROR', 'Confirm your identity with your organization first.', 400),
    );
    renderSettings(false, true);
    const email = section('Email Address');
    fireEvent.change(email.querySelector('input[type=email]')!, {
      target: { value: 'new@e2e-mail.org' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Change Email' }));

    expect(
      await screen.findByText('Confirm your identity with your organization first.'),
    ).toBeInTheDocument();
    expect(confirmButton(email)).toBeInTheDocument();
  });

  it('keeps the confirmation when the request never reached the gateway', async () => {
    arriveAt('/dashboard/settings?reauth=bind_email');
    api.bindEmail.mockRejectedValue(new APIError('NETWORK_ERROR', 'Failed to fetch'));
    renderSettings(false, true);
    const email = section('Email Address');
    fireEvent.change(email.querySelector('input[type=email]')!, {
      target: { value: 'new@e2e-mail.org' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Change Email' }));

    await waitFor(() => expect(api.bindEmail).toHaveBeenCalledTimes(1));
    expect(await screen.findByText(/Network error/)).toBeInTheDocument();
    expect(within(email).getByText('Identity confirmed. You can continue.')).toBeInTheDocument();
  });

  it('sets a first password after confirming, with neither a password nor a code', async () => {
    arriveAt('/dashboard/settings?reauth=set_password');
    renderSettings(true, true);
    const password = section('Set Password');
    expect(within(password).getByText('Identity confirmed. You can continue.')).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText(/^New Password/), { target: { value: 'Brand-New-1' } });
    fireEvent.change(screen.getByLabelText(/^Confirm New Password/), {
      target: { value: 'Brand-New-1' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Set Password' }));

    await waitFor(() =>
      expect(api.updatePassword).toHaveBeenCalledWith('', 'Brand-New-1', undefined),
    );
    expect(api.sendPhoneCode).not.toHaveBeenCalled();
  });

  it('does not set a first password before the provider has confirmed', async () => {
    renderSettings(false, true);
    fireEvent.change(screen.getByLabelText(/^New Password/), { target: { value: 'Brand-New-1' } });
    fireEvent.change(screen.getByLabelText(/^Confirm New Password/), {
      target: { value: 'Brand-New-1' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Set Password' }));

    expect(
      await screen.findByText('Confirm your identity with Example University to continue.'),
    ).toBeInTheDocument();
    expect(api.updatePassword).not.toHaveBeenCalled();
  });

  it('ignores a confirmation the address names for an action that does not exist', () => {
    arriveAt('/dashboard/settings?reauth=transfer_everything');
    renderSettings(false, true);

    expect(screen.queryByText('Identity confirmed. You can continue.')).not.toBeInTheDocument();
    expect(window.location.search).toBe('');
  });

  it('does not restore what another account typed in this tab', () => {
    draft('bind_email', 'someone-else@e2e-mail.org', 'another-user');
    arriveAt('/dashboard/settings?reauth=bind_email');
    renderSettings(false, true);

    expect(section('Email Address').querySelector('input[type=email]')).toHaveValue(
      'carol@example.edu',
    );
  });

  it.each([
    [
      409,
      'This account has no single sign-on to confirm with.',
      'This account has no single sign-on to confirm with.',
    ],
    [503, 'raw', 'Single sign-on is unavailable right now. Use another sign-in method.'],
    [429, 'raw', 'Too many sign-in attempts. Please try again later.'],
  ])('says why it could not leave for the provider: %i', async (status, raw, shown) => {
    sso.startSsoReauth.mockRejectedValue(new APIError('USER_ALREADY_EXISTS', raw, status));
    renderSettings(false, true);
    const email = section('Email Address');

    fireEvent.click(confirmButton(email));

    expect(await within(email).findByRole('alert')).toHaveTextContent(shown);
    expect(navigateTo).not.toHaveBeenCalled();
    expect(window.sessionStorage.getItem('sso-reauth-draft')).toBeNull();
    expect(confirmButton(email)).toBeEnabled();
  });

  it('reports a round trip that failed, once, and keeps what was typed', () => {
    draft('bind_email', 'new@e2e-mail.org');
    arriveAt('/dashboard/settings?sso_error=cancelled');
    renderSettings(false, true);

    const alert = screen.getByRole('alert');
    expect(alert).toHaveTextContent('Single sign-on was cancelled.');
    expect(window.location.search).toBe('');
    expect(section('Email Address').querySelector('input[type=email]')).toHaveValue(
      'new@e2e-mail.org',
    );
    expect(screen.queryByText('Identity confirmed. You can continue.')).not.toBeInTheDocument();

    fireEvent.click(within(alert).getByRole('button', { name: 'Dismiss' }));
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('keeps the rest of the address when it takes its own parameters out', () => {
    arriveAt('/dashboard/settings?tab=security&reauth=set_password&sso_error=expired#password');
    renderSettings(false, true);

    expect(`${window.location.pathname}${window.location.search}${window.location.hash}`).toBe(
      '/dashboard/settings?tab=security#password',
    );
  });
});

describe('the single sign-on section', () => {
  const identity = (extra: Partial<SsoIdentity> = {}): SsoIdentity => ({
    id: 'idn_1',
    provider_id: 'example-univ',
    provider_name: 'Example University',
    email: 'carol@example.edu',
    linked_at: '2026-11-01T09:00:00Z',
    last_login_at: null,
    available: true,
    ...extra,
  });
  const ssoSection = () =>
    screen.getByRole('heading', { name: 'Single sign-on' }).closest('section')!;
  const querySection = () => screen.queryByRole('heading', { name: 'Single sign-on' });

  it('is absent where single sign-on is not offered and the account has no identity', async () => {
    renderSettings(false, false);

    await waitFor(() => expect(sso.listSsoIdentities).toHaveBeenCalled());
    expect(querySection()).not.toBeInTheDocument();
  });

  it('says so when single sign-on is offered and nothing is connected', async () => {
    renderSettings(false, true);

    expect(
      await within(ssoSection()).findByText('No organization account is connected.'),
    ).toBeInTheDocument();
    expect(
      within(ssoSection()).getByText('Organization accounts that can sign in to this account.'),
    ).toBeInTheDocument();
    expect(
      within(ssoSection()).getByRole('button', { name: 'Connect an SSO account' }),
    ).toBeInTheDocument();
  });

  it('lists each identity with its provider, its email and when it was connected', async () => {
    sso.listSsoIdentities.mockResolvedValue([
      identity(),
      identity({ id: 'idn_2', provider_name: 'Example Labs', email: null }),
    ]);
    renderSettings(false, true);

    const first = (await screen.findByText('Example University')).closest('li')!;
    expect(within(first).getByText('carol@example.edu')).toBeInTheDocument();
    expect(within(first).getByText(/^Connected .*2026/)).toBeInTheDocument();
    expect(within(first).queryByText('No longer available')).not.toBeInTheDocument();
    expect(screen.getByText('Example Labs').closest('li')).toBeInTheDocument();
    expect(screen.queryByText('No organization account is connected.')).not.toBeInTheDocument();
  });

  it('still shows an identity whose provider is no longer offered, and marks it', async () => {
    // Single sign-on was turned off here; the identity stays on the account.
    sso.listSsoIdentities.mockResolvedValue([identity({ available: false })]);
    renderSettings(false, false);

    const row = (await screen.findByText('Example University')).closest('li')!;
    expect(within(row).getByText('No longer available')).toBeInTheDocument();
    expect(within(row).getByRole('button', { name: /^Disconnect/ })).toBeInTheDocument();
    // Nothing answers for a new one.
    expect(
      screen.queryByRole('button', { name: 'Connect an SSO account' }),
    ).not.toBeInTheDocument();
  });

  it('disconnects an identity after the owner’s password', async () => {
    sso.listSsoIdentities.mockResolvedValueOnce([identity()]).mockResolvedValue([]);
    renderSettings(false, true);

    fireEvent.click(await screen.findByRole('button', { name: 'Disconnect Example University' }));
    const row = screen.getByText('Example University').closest('li')!;
    const disconnect = within(row).getByRole('button', { name: 'Disconnect' });
    expect(disconnect).toBeDisabled();
    expect(sso.unlinkSsoIdentity).not.toHaveBeenCalled();

    fireEvent.change(within(row).getByLabelText(/^Current password/), {
      target: { value: 'Secret-1' },
    });
    fireEvent.click(disconnect);

    await waitFor(() =>
      expect(sso.unlinkSsoIdentity).toHaveBeenCalledExactlyOnceWith('idn_1', {
        password: 'Secret-1',
      }),
    );
    expect(await screen.findByText('No organization account is connected.')).toBeInTheDocument();
    expect(toast.success).toHaveBeenCalledWith('Single sign-on disconnected.');
    // How the account confirms its next action may have changed with it.
    expect(refreshUser).toHaveBeenCalled();
    expect(api.sendPhoneCode).not.toHaveBeenCalled();
  });

  it('can be backed out of', async () => {
    sso.listSsoIdentities.mockResolvedValue([identity()]);
    renderSettings(false, true);
    fireEvent.click(await screen.findByRole('button', { name: 'Disconnect Example University' }));

    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));

    expect(screen.queryByLabelText(/^Current password/, { selector: 'li input' })).toBeNull();
    expect(screen.getByRole('button', { name: 'Disconnect Example University' })).toBeEnabled();
    expect(sso.unlinkSsoIdentity).not.toHaveBeenCalled();
  });

  it('explains that the only way to sign in cannot be disconnected', async () => {
    sso.listSsoIdentities.mockResolvedValue([identity()]);
    sso.unlinkSsoIdentity.mockRejectedValue(
      new APIError('USER_ALREADY_EXISTS', 'Only sign-in method.', 409),
    );
    renderSettings(false, true);
    fireEvent.click(await screen.findByRole('button', { name: 'Disconnect Example University' }));
    const row = screen.getByText('Example University').closest('li')!;
    fireEvent.change(within(row).getByLabelText(/^Current password/), {
      target: { value: 'Secret-1' },
    });
    fireEvent.click(within(row).getByRole('button', { name: 'Disconnect' }));

    expect(await within(row).findByRole('alert')).toHaveTextContent(
      'This is the only way to sign in to this account. Set a password before disconnecting it.',
    );
    expect(screen.getByText('Example University')).toBeInTheDocument();
  });

  it('shows the gateway’s reason for any other refusal', async () => {
    sso.listSsoIdentities.mockResolvedValue([identity()]);
    sso.unlinkSsoIdentity.mockRejectedValue(
      new APIError('UNKNOWN_ERROR', 'Current password is incorrect.', 400),
    );
    renderSettings(false, true);
    fireEvent.click(await screen.findByRole('button', { name: 'Disconnect Example University' }));
    const row = screen.getByText('Example University').closest('li')!;
    fireEvent.change(within(row).getByLabelText(/^Current password/), { target: { value: 'no' } });
    fireEvent.click(within(row).getByRole('button', { name: 'Disconnect' }));

    expect(await within(row).findByRole('alert')).toHaveTextContent(
      'Current password is incorrect.',
    );
  });

  it('connects another identity: the email, the owner’s password, then the provider', async () => {
    renderSettings(false, true);
    fireEvent.click(await screen.findByRole('button', { name: 'Connect an SSO account' }));
    const section = ssoSection();
    fireEvent.change(within(section).getByLabelText('Work or school email'), {
      target: { value: ' carol@example.edu ' },
    });

    fireEvent.click(within(section).getByRole('button', { name: 'Continue' }));
    expect(
      await within(section).findByText('Enter your current password to confirm this change.'),
    ).toBeInTheDocument();
    expect(sso.linkSsoIdentity).not.toHaveBeenCalled();

    fireEvent.change(within(section).getByLabelText(/^Current password/), {
      target: { value: 'Secret-1' },
    });
    fireEvent.click(within(section).getByRole('button', { name: 'Continue' }));

    await waitFor(() =>
      expect(navigateTo).toHaveBeenCalledWith('https://idp.example.edu/authorize?state=link'),
    );
    expect(sso.linkSsoIdentity).toHaveBeenCalledExactlyOnceWith({
      email: 'carol@example.edu',
      returnTo: '/dashboard/settings',
      password: 'Secret-1',
    });
  });

  it('says so when no provider answers for the email', async () => {
    sso.linkSsoIdentity.mockResolvedValue(null);
    renderSettings(false, true);
    fireEvent.click(await screen.findByRole('button', { name: 'Connect an SSO account' }));
    const section = ssoSection();
    fireEvent.change(within(section).getByLabelText('Work or school email'), {
      target: { value: 'carol@nowhere.test' },
    });
    fireEvent.change(within(section).getByLabelText(/^Current password/), {
      target: { value: 'Secret-1' },
    });
    fireEvent.click(within(section).getByRole('button', { name: 'Continue' }));

    expect(await within(section).findByRole('alert')).toHaveTextContent(
      'Your organization has not set up single sign-on here. Use another sign-in method.',
    );
    expect(navigateTo).not.toHaveBeenCalled();
    expect(within(section).getByRole('button', { name: 'Continue' })).toBeEnabled();
  });

  it('asks for a real address before asking the gateway', async () => {
    renderSettings(false, true);
    fireEvent.click(await screen.findByRole('button', { name: 'Connect an SSO account' }));
    const section = ssoSection();
    fireEvent.change(within(section).getByLabelText('Work or school email'), {
      target: { value: 'carol' },
    });
    fireEvent.click(within(section).getByRole('button', { name: 'Continue' }));

    expect(
      await within(section).findByText('Please enter a valid email address'),
    ).toBeInTheDocument();
    expect(sso.linkSsoIdentity).not.toHaveBeenCalled();
  });

  it('confirms a newly connected identity when the provider sends the browser back', async () => {
    sso.listSsoIdentities.mockResolvedValue([identity()]);
    arriveAt('/dashboard/settings?sso_linked=1');
    renderSettings(false, true);

    expect(await screen.findByText('Example University')).toBeInTheDocument();
    expect(toast.success).toHaveBeenCalledExactlyOnceWith('Single sign-on connected.');
    expect(window.location.search).toBe('');
  });

  it('reports why connecting failed', async () => {
    arriveAt('/dashboard/settings?sso_error=identity_in_use');
    renderSettings(false, true);

    expect(screen.getByRole('alert')).toHaveTextContent(
      'This organization account is already connected to another account.',
    );
    expect(toast.success).not.toHaveBeenCalled();
  });

  // A verification code does not confirm these two actions.
  describe('for an account with a phone and no password', () => {
    beforeEach(() => {
      user = {
        ...user,
        email: null,
        phone: '+8613812345678',
        phone_verified: true,
        has_password: false,
        sso_reauth_provider: null,
      };
      sso.listSsoIdentities.mockResolvedValue([identity()]);
    });

    it('asks for a password first, and offers neither action', async () => {
      renderSettings(true, true);

      expect(await screen.findByText('Example University')).toBeInTheDocument();
      const section = ssoSection();
      expect(
        within(section).getByText('Set a password before changing single sign-on.'),
      ).toBeInTheDocument();
      expect(within(section).queryByRole('button', { name: /^Disconnect/ })).toBeNull();
      expect(within(section).queryByRole('button', { name: 'Connect an SSO account' })).toBeNull();
      // No code is asked for here, whatever the other sections do.
      expect(section.querySelector('input[inputmode=numeric]')).toBeNull();
      expect(within(section).queryByRole('button', { name: 'Send verification code' })).toBeNull();
    });
  });

  describe('for an account that signs in only through its provider', () => {
    const PROVIDER = { id: 'example-univ', name: 'Example University' };
    beforeEach(() => {
      user = {
        id: 'sso-user',
        email: 'carol@example.edu',
        role: 'free',
        is_admin: false,
        has_password: false,
        sso_reauth_provider: PROVIDER,
      };
      sso.listSsoIdentities.mockResolvedValue([
        identity(),
        identity({ id: 'idn_2', provider_name: 'Example Labs', email: 'carol@labs.example' }),
      ]);
    });

    it('leaves for the provider before disconnecting, remembering which identity', async () => {
      renderSettings(false, true);
      fireEvent.click(await screen.findByRole('button', { name: 'Disconnect Example Labs' }));
      const row = screen.getByText('Example Labs').closest('li')!;
      expect(within(row).queryByLabelText(/^Current password/)).not.toBeInTheDocument();
      expect(within(row).getByRole('button', { name: 'Disconnect' })).toBeDisabled();

      fireEvent.click(within(row).getByRole('button', { name: 'Confirm with Example University' }));

      await waitFor(() => expect(navigateTo).toHaveBeenCalled());
      expect(sso.startSsoReauth).toHaveBeenCalledExactlyOnceWith(
        'unlink_sso',
        '/dashboard/settings',
      );
      expect(JSON.parse(window.sessionStorage.getItem('sso-reauth-draft')!)).toMatchObject({
        purpose: 'unlink_sso',
        value: 'idn_2',
      });
      expect(sso.unlinkSsoIdentity).not.toHaveBeenCalled();
    });

    it('comes back to the same identity, confirmed, and disconnects it without a password', async () => {
      window.sessionStorage.setItem(
        'sso-reauth-draft',
        JSON.stringify({
          purpose: 'unlink_sso',
          userId: 'sso-user',
          value: 'idn_2',
          savedAt: Date.now(),
        }),
      );
      arriveAt('/dashboard/settings?reauth=unlink_sso');
      renderSettings(false, true);

      const row = (await screen.findByText('Example Labs')).closest('li')!;
      expect(within(row).getByText('Identity confirmed. You can continue.')).toBeInTheDocument();
      // Only the one it was left for is open.
      expect(
        screen.getByRole('button', { name: 'Disconnect Example University' }),
      ).toBeInTheDocument();

      fireEvent.click(within(row).getByRole('button', { name: 'Disconnect' }));

      await waitFor(() =>
        expect(sso.unlinkSsoIdentity).toHaveBeenCalledExactlyOnceWith('idn_2', {}),
      );
    });

    it('comes back to the connect form with the email it left, and sends no password', async () => {
      window.sessionStorage.setItem(
        'sso-reauth-draft',
        JSON.stringify({
          purpose: 'link_sso',
          userId: 'sso-user',
          value: 'carol@other.example',
          savedAt: Date.now(),
        }),
      );
      arriveAt('/dashboard/settings?reauth=link_sso');
      renderSettings(false, true);
      const section = ssoSection();

      expect(within(section).getByLabelText('Work or school email')).toHaveValue(
        'carol@other.example',
      );
      expect(
        within(section).getByText('Identity confirmed. You can continue.'),
      ).toBeInTheDocument();
      expect(within(section).queryByLabelText(/^Current password/)).not.toBeInTheDocument();

      fireEvent.click(within(section).getByRole('button', { name: 'Continue' }));

      await waitFor(() =>
        expect(sso.linkSsoIdentity).toHaveBeenCalledExactlyOnceWith({
          email: 'carol@other.example',
          returnTo: '/dashboard/settings',
        }),
      );
      await waitFor(() => expect(navigateTo).toHaveBeenCalled());
    });

    it('asks to confirm again when no provider answers: the confirmation was used', async () => {
      window.sessionStorage.setItem(
        'sso-reauth-draft',
        JSON.stringify({
          purpose: 'link_sso',
          userId: 'sso-user',
          value: 'carol@nowhere.test',
          savedAt: Date.now(),
        }),
      );
      arriveAt('/dashboard/settings?reauth=link_sso');
      sso.linkSsoIdentity.mockResolvedValue(null);
      renderSettings(false, true);
      const section = ssoSection();

      fireEvent.click(within(section).getByRole('button', { name: 'Continue' }));

      expect(await within(section).findByRole('alert')).toHaveTextContent(
        'Your organization has not set up single sign-on here. Use another sign-in method.',
      );
      expect(
        within(section).getByRole('button', { name: 'Confirm with Example University' }),
      ).toBeInTheDocument();
      expect(navigateTo).not.toHaveBeenCalled();
    });

    // A link someone was sent can name a confirmation; only the gateway holds one.
    it('opens nothing for an address that names a confirmation this tab did not leave for', async () => {
      arriveAt('/dashboard/settings?reauth=link_sso');
      renderSettings(false, true);
      const section = ssoSection();

      expect(within(section).queryByLabelText('Work or school email')).not.toBeInTheDocument();

      // Opened by hand it reads as confirmed: the gateway decides whether it holds.
      fireEvent.click(await screen.findByRole('button', { name: 'Connect an SSO account' }));
      expect(
        within(section).getByText('Identity confirmed. You can continue.'),
      ).toBeInTheDocument();
    });

    it('does not connect before the provider has confirmed', async () => {
      renderSettings(false, true);
      fireEvent.click(await screen.findByRole('button', { name: 'Connect an SSO account' }));
      const section = ssoSection();
      fireEvent.change(within(section).getByLabelText('Work or school email'), {
        target: { value: 'carol@other.example' },
      });
      fireEvent.click(within(section).getByRole('button', { name: 'Continue' }));

      expect(
        await within(section).findByText(
          'Confirm your identity with Example University to continue.',
        ),
      ).toBeInTheDocument();
      expect(sso.linkSsoIdentity).not.toHaveBeenCalled();
    });
  });
});
