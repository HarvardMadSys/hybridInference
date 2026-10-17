// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AuthUser } from '@/components/providers/AuthProvider';

let user: AuthUser;
vi.mock('@/components/providers', () => ({
  useAuth: () => ({ state: { loading: false, isAuthenticated: true, user }, refreshUser: vi.fn() }),
}));
const api = vi.hoisted(() => ({
  bindEmail: vi.fn(),
  sendPhoneCode: vi.fn(),
  updatePassword: vi.fn(),
}));
vi.mock('@/lib/api/auth', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/auth')>()),
  bindEmail: api.bindEmail,
  sendPhoneCode: api.sendPhoneCode,
}));
vi.mock('@/lib/api/user', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/user')>()),
  updatePassword: api.updatePassword,
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
  user = {
    id: 'email-user',
    email: 'user@example.test',
    role: 'free',
    is_admin: false,
    has_password: true,
  };
});
afterEach(cleanup);

function renderSettings(phoneAuth: boolean) {
  return render(
    <SiteConfigProvider
      initialConfig={{
        ...buildTimeSiteConfig,
        features: { ...buildTimeSiteConfig.features, phoneAuth },
      }}
    >
      <SettingsPage />
    </SiteConfigProvider>,
  );
}

describe('account phone binding', () => {
  it('keeps the existing account forms and hides binding when SMS is disabled', () => {
    renderSettings(false);
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
    expect(screen.getByRole('heading', { name: 'Change Username' })).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Change Password' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Bind mobile number' })).not.toBeInTheDocument();
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
