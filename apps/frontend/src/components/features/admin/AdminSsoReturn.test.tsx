// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';

import { useAdminSsoReauth } from '@/components/features/admin/confirm/adminSsoReauth';
import { AdminSsoReturn } from './AdminSsoReturn';

const auth = vi.hoisted(() => ({
  user: {
    id: 'u-admin',
    is_admin: true,
    sso_reauth_provider: { id: 'okta', name: 'Okta' } as { id: string; name: string } | null,
  },
}));
vi.mock('@/components/providers', () => ({
  useAuth: () => ({ state: { loading: false, isAuthenticated: true, user: auth.user } }),
}));

function Probe() {
  const sso = useAdminSsoReauth();
  return <span data-testid="probe">{sso ? `${sso.provider.name}:${sso.confirmed}` : 'none'}</span>;
}

afterEach(() => {
  cleanup();
  window.history.replaceState(null, '', '/');
});

it('says a guarded change is confirmed when the provider sends the browser back', () => {
  window.history.replaceState(null, '', '/dashboard/admin/settings?reauth=admin_action');

  render(
    <AdminSsoReturn userId="u-admin">
      <Probe />
    </AdminSsoReturn>,
  );

  expect(screen.getByRole('status')).toHaveTextContent('Identity confirmed');
  expect(screen.getByTestId('probe')).toHaveTextContent('Okta:true');
  // Read once: the address no longer carries it.
  expect(window.location.search).toBe('');
});

it('offers nothing to an administrator who confirms with a password or a code', () => {
  auth.user = { ...auth.user, sso_reauth_provider: null };

  render(
    <AdminSsoReturn userId="u-admin">
      <Probe />
    </AdminSsoReturn>,
  );

  expect(screen.getByTestId('probe')).toHaveTextContent('none');
  expect(screen.queryByRole('status')).not.toBeInTheDocument();
});

it("ignores another page's confirmation", () => {
  auth.user = { ...auth.user, sso_reauth_provider: { id: 'okta', name: 'Okta' } };
  window.history.replaceState(null, '', '/dashboard/admin/settings?reauth=delete_account');

  render(
    <AdminSsoReturn userId="u-admin">
      <Probe />
    </AdminSsoReturn>,
  );

  expect(screen.getByTestId('probe')).toHaveTextContent('Okta:false');
  expect(screen.queryByRole('status')).not.toBeInTheDocument();
});
