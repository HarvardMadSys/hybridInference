// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { DangerZone, confirmationMatches, deletionProof } from './DeleteAccount';

const deleteAccount = vi.fn();
const sendPhoneCode = vi.fn();

type TestUser = {
  id: string;
  email: string | null;
  login_name?: string | null;
  phone?: string | null;
  phone_verified?: boolean;
  has_password?: boolean;
  role: string;
  is_admin: boolean;
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
