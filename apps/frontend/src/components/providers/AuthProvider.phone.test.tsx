// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/lib/api/auth', () => ({
  login: vi.fn(),
  loginPhone: vi.fn(),
  logout: vi.fn(),
  endAgentSession: vi.fn(),
}));
vi.mock('@/lib/api/user', () => ({ getMe: vi.fn() }));

import { loginPhone } from '@/lib/api/auth';
import { getMe } from '@/lib/api/user';
import { AuthProvider, useAuth } from './AuthProvider';

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

function Consumer() {
  const { state, loginPhone } = useAuth();
  return (
    <>
      <p>{state.loading ? 'loading' : state.isAuthenticated ? state.user?.phone : 'signed out'}</p>
      <button onClick={() => loginPhone('13812345678', '123456')}>Phone login</button>
      <p>{state.user?.email === null ? 'phone-only account' : state.user?.email}</p>
    </>
  );
}

describe('AuthProvider phone login', () => {
  it('refreshes the shared user state with a nullable email after phone authentication', async () => {
    vi.mocked(getMe).mockRejectedValueOnce(new Error('No session'));
    vi.mocked(getMe).mockResolvedValueOnce({
      id: 'phone-user',
      email: null,
      phone: '+8613812345678',
      phone_verified: true,
      has_password: false,
      user_name: 'Phone User',
      role: 'free',
      status: 'active',
      email_verified: false,
      is_admin: false,
      created_at: '2026-10-09',
    });
    vi.mocked(loginPhone).mockResolvedValueOnce({
      access_token: 'access',
      token_type: 'bearer',
      expires_in: 3600,
      user: {
        id: 'phone-user',
        email: null,
        phone: '+8613812345678',
        phone_verified: true,
        role: 'free',
        is_admin: false,
      },
    });
    render(
      <QueryClientProvider client={new QueryClient()}>
        <AuthProvider>
          <Consumer />
        </AuthProvider>
      </QueryClientProvider>,
    );
    expect(await screen.findByText('signed out')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Phone login' }));
    expect(await screen.findByText('+8613812345678')).toBeInTheDocument();
    expect(screen.getByText('phone-only account')).toBeInTheDocument();
    expect(loginPhone).toHaveBeenCalledWith({ phone: '13812345678', code: '123456' });
    await waitFor(() => expect(getMe).toHaveBeenCalledTimes(2));
  });
});
