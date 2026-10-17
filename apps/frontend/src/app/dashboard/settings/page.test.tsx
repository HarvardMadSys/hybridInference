// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AuthUser } from '@/components/providers/AuthProvider';

let user: AuthUser;
vi.mock('@/components/providers', () => ({
  useAuth: () => ({ state: { loading: false, isAuthenticated: true, user }, refreshUser: vi.fn() }),
}));
vi.mock('@/components/features/auth/ProtectedRoute', () => ({
  ProtectedRoute: ({ children }: { children: React.ReactNode }) => children,
}));

import { SiteConfigProvider } from '@/components/providers/SiteConfigProvider';
import { buildTimeSiteConfig } from '@/config/site-config';
import SettingsPage from './page';

beforeEach(() => {
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
    expect(screen.getByText('user@example.test')).toBeInTheDocument();
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
