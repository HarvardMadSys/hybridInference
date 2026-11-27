// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { DangerZone, confirmationMatches, deletionProof } from './DeleteAccount';
import { SsoReauthProvider } from './SsoReauth';
import { APIError } from '@/lib/utils/errors';

const deleteAccount = vi.fn();
const sendPhoneCode = vi.fn();
const startSsoReauth = vi.hoisted(() => vi.fn());
const navigateTo = vi.hoisted(() => vi.fn());

type TestUser = {
  id: string;
  email: string | null;
  login_name?: string | null;
  phone?: string | null;
  phone_verified?: boolean;
  has_password?: boolean;
  role: string;
  is_admin: boolean;
  sso_reauth_provider?: { id: string; name: string } | null;
};

let user: TestUser;
let features: { phoneAuth?: boolean } = {};

vi.mock('react-hot-toast', () => ({ default: { success: vi.fn(), error: vi.fn() } }));

vi.mock('@/components/providers', () => ({
  useAuth: () => ({ state: { isAuthenticated: true, loading: false, user }, deleteAccount }),
}));

vi.mock('@/components/providers/SiteConfigProvider', () => ({
  useSiteConfig: () => ({ branding: { contactEmail: 'help@example.test' }, features }),
}));

vi.mock('@/components/providers/useT', () => ({
  useT: () => (_key: string, fallback: string) => fallback,
}));

vi.mock('@/lib/api/auth', () => ({
  sendPhoneCode: (...args: unknown[]) => sendPhoneCode(...args),
}));

vi.mock('@/lib/api/sso', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/sso')>()),
  startSsoReauth,
}));

// jsdom's location cannot be watched; the page leaves through this.
vi.mock('@/lib/utils/navigation', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/utils/navigation')>()),
  navigateTo,
}));

function passwordUser(extra: Partial<TestUser> = {}): TestUser {
  return {
    id: 'owner',
    email: 'owner@example.test',
    has_password: true,
    role: 'free',
    is_admin: false,
    ...extra,
  };
}

function phoneUser(): TestUser {
  return passwordUser({
    email: null,
    has_password: false,
    phone: '+8613800138000',
    phone_verified: true,
  });
}

/** An account created by single sign-on: no password, and no phone to send a code to. */
function ssoUser(): TestUser {
  return passwordUser({
    email: 'carol@example.edu',
    has_password: false,
    sso_reauth_provider: { id: 'example-univ', name: 'Example University' },
  });
}

function openDialog() {
  render(<DangerZone />);
  fireEvent.click(screen.getByRole('button', { name: 'Delete account' }));
  return screen.getByRole('dialog');
}

const confirmBox = () => screen.getByLabelText(/To confirm, type/);
const deleteButton = () => screen.getByRole('button', { name: 'Delete this account' });

beforeEach(() => {
  user = passwordUser();
  features = {};
  deleteAccount.mockReset().mockResolvedValue(undefined);
  sendPhoneCode.mockReset().mockResolvedValue({ message: 'sent', retry_after: 60 });
  startSsoReauth.mockReset().mockResolvedValue('https://idp.example.edu/authorize?prompt=login');
  navigateTo.mockReset();
  window.sessionStorage.clear();
});

afterEach(cleanup);

describe('deletionProof', () => {
  it('asks a password account for its password', () => {
    expect(deletionProof(passwordUser() as never, false)).toBe('password');
  });

  it('asks a phone-only account for a code only while phone sign-in is on', () => {
    expect(deletionProof(phoneUser() as never, true)).toBe('phone');
    expect(deletionProof(phoneUser() as never, false)).toBe('unavailable');
  });

  it('refuses administrators', () => {
    expect(deletionProof(passwordUser({ role: 'admin', is_admin: true }) as never, true)).toBe(
      'admin',
    );
  });

  it('sends an account with neither a password nor a phone to its identity provider', () => {
    expect(deletionProof(ssoUser() as never, false)).toBe('sso');
    expect(deletionProof(ssoUser() as never, true)).toBe('sso');
    // Without a provider to confirm with there is nothing it can prove here.
    expect(
      deletionProof(
        passwordUser({ has_password: false, sso_reauth_provider: null }) as never,
        true,
      ),
    ).toBe('unavailable');
  });

  it('sends a member of an organization to its identity provider, not to its phone', () => {
    // The gateway names the provider for an account that signs in through
    // single sign-on only, also when it has a verified phone.
    const member = { ...phoneUser(), sso_reauth_provider: { id: 'example-univ', name: 'Example' } };

    expect(deletionProof(member as never, true)).toBe('sso');
    expect(deletionProof(member as never, false)).toBe('sso');
  });
});

describe('confirmationMatches', () => {
  it('ignores case and surrounding space, as the gateway does', () => {
    expect(confirmationMatches('  Owner@Example.TEST ', 'owner@example.test')).toBe(true);
    expect(confirmationMatches('owner@example', 'owner@example.test')).toBe(false);
    expect(confirmationMatches('', '')).toBe(false);
  });
});

describe('DangerZone', () => {
  it('keeps the delete button disabled until the identifier and password are entered', async () => {
    openDialog();
    expect(deleteButton()).toBeDisabled();

    fireEvent.change(confirmBox(), { target: { value: 'owner@example.test' } });
    expect(deleteButton()).toBeDisabled();

    fireEvent.change(screen.getByLabelText('Your password'), { target: { value: 'secret' } });
    expect(deleteButton()).toBeEnabled();

    fireEvent.click(deleteButton());
    await waitFor(() =>
      expect(deleteAccount).toHaveBeenCalledWith({
        confirm: 'owner@example.test',
        password: 'secret',
      }),
    );
  });

  it('accepts a pasted identifier', () => {
    openDialog();
    const box = confirmBox();
    fireEvent.paste(box);
    fireEvent.change(box, { target: { value: 'owner@example.test' } });
    expect(box).toHaveValue('owner@example.test');
  });

  it('keeps the dialog open with the error when the gateway refuses', async () => {
    deleteAccount.mockRejectedValue(new Error('Current password is incorrect.'));
    openDialog();
    fireEvent.change(confirmBox(), { target: { value: 'owner@example.test' } });
    fireEvent.change(screen.getByLabelText('Your password'), { target: { value: 'wrong' } });
    fireEvent.click(deleteButton());
    expect(await screen.findByRole('alert')).toHaveTextContent('Current password is incorrect.');
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(deleteButton()).toBeEnabled();
  });

  it('confirms a phone-only account with a code sent to its own number', async () => {
    user = phoneUser();
    features = { phoneAuth: true };
    openDialog();
    expect(screen.queryByLabelText('Your password')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Send verification code' }));
    await waitFor(() =>
      expect(sendPhoneCode).toHaveBeenCalledWith({
        phone: '+8613800138000',
        purpose: 'delete_account',
        turnstileToken: undefined,
      }),
    );

    fireEvent.change(confirmBox(), { target: { value: '+8613800138000' } });
    fireEvent.change(screen.getByLabelText('Verification code'), { target: { value: '123456' } });
    fireEvent.click(deleteButton());
    await waitFor(() =>
      expect(deleteAccount).toHaveBeenCalledWith({
        confirm: '+8613800138000',
        phoneCode: '123456',
      }),
    );
  });

  it('does not offer deletion to an administrator', () => {
    user = passwordUser({ role: 'admin', is_admin: true });
    render(<DangerZone />);
    expect(screen.getByRole('button', { name: 'Delete account' })).toBeDisabled();
    expect(screen.getByText(/cannot delete itself/)).toBeInTheDocument();
  });

  it('points a phone-only account at support while phone sign-in is off', () => {
    user = phoneUser();
    render(<DangerZone />);
    expect(screen.getByRole('button', { name: 'Delete account' })).toBeDisabled();
    expect(screen.getByText(/contact help@example\.test/)).toBeInTheDocument();
  });

  it('closes on Escape', () => {
    openDialog();
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });
});

// The account has no password and no phone: it confirms by signing in at its
// identity provider again, and the page that comes back finishes the deletion.
describe('DangerZone, confirming through the identity provider', () => {
  const spend = vi.fn();

  /**
   * The settings page as it is when the provider has sent the browser back.
   * `draft` is what this tab typed before it left; `null` when it never left.
   */
  function renderConfirmed(draft: string | null) {
    return render(
      <SsoReauthProvider
        value={{
          confirmed: 'delete_account',
          draft: draft === null ? null : { purpose: 'delete_account', value: draft },
          spend,
          returnTo: '/dashboard/settings',
        }}
      >
        <DangerZone />
      </SsoReauthProvider>,
    );
  }

  beforeEach(() => {
    user = ssoUser();
    spend.mockReset();
  });

  it('offers the provider in place of a password, and deletes nothing before it confirms', () => {
    openDialog();

    expect(screen.queryByLabelText('Your password')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Verification code')).not.toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: 'Confirm with Example University' }),
    ).toBeInTheDocument();

    fireEvent.change(confirmBox(), { target: { value: 'carol@example.edu' } });
    expect(deleteButton()).toBeDisabled();
  });

  it('leaves for the provider for this action, keeping what was typed', async () => {
    window.history.replaceState(null, '', '/dashboard/settings');
    openDialog();
    fireEvent.change(confirmBox(), { target: { value: 'carol@example.edu' } });

    fireEvent.click(screen.getByRole('button', { name: 'Confirm with Example University' }));

    await waitFor(() =>
      expect(navigateTo).toHaveBeenCalledWith('https://idp.example.edu/authorize?prompt=login'),
    );
    expect(startSsoReauth).toHaveBeenCalledExactlyOnceWith('delete_account', '/dashboard/settings');
    expect(JSON.parse(window.sessionStorage.getItem('sso-reauth-draft')!)).toMatchObject({
      purpose: 'delete_account',
      userId: 'owner',
      value: 'carol@example.edu',
    });
    expect(deleteAccount).not.toHaveBeenCalled();
  });

  it('opens the dialog again when the provider sends the browser back, confirmed', async () => {
    renderConfirmed('carol@example.edu');

    // No click: the dialog that was left is open, with what was typed in it.
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(screen.getByText('Identity confirmed. You can continue.')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^Confirm with/ })).not.toBeInTheDocument();
    expect(confirmBox()).toHaveValue('carol@example.edu');
    expect(deleteButton()).toBeEnabled();

    fireEvent.click(deleteButton());

    // Neither a password nor a code: the provider's confirmation is the proof.
    await waitFor(() =>
      expect(deleteAccount).toHaveBeenCalledExactlyOnceWith({ confirm: 'carol@example.edu' }),
    );
  });

  it('still asks for the identifier when nothing was typed before leaving', () => {
    renderConfirmed('');

    expect(confirmBox()).toHaveValue('');
    expect(deleteButton()).toBeDisabled();
    fireEvent.change(confirmBox(), { target: { value: 'carol@example.edu' } });
    expect(deleteButton()).toBeEnabled();
  });

  // A link someone was sent can name a confirmation; only the gateway holds one.
  it('opens nothing for an address that names a confirmation this tab did not leave for', () => {
    renderConfirmed(null);

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();

    // Opened by hand, it is the confirmed dialog: the gateway decides if it holds.
    fireEvent.click(screen.getByRole('button', { name: 'Delete account' }));
    expect(screen.getByText('Identity confirmed. You can continue.')).toBeInTheDocument();
  });

  it('gives the confirmation up when the gateway refuses, since it holds only once', async () => {
    deleteAccount.mockRejectedValue(
      new APIError('UNKNOWN_ERROR', 'Confirm your identity with your organization first.', 400),
    );
    renderConfirmed('carol@example.edu');

    fireEvent.click(deleteButton());

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Confirm your identity with your organization first.',
    );
    expect(spend).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('dialog')).toBeInTheDocument();
  });

  it('stays closed on an ordinary visit', () => {
    render(<DangerZone />);

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Delete account' })).toBeEnabled();
  });
});
