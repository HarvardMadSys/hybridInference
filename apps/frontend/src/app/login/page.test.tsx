// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import LoginPage from './page';
import { SiteConfigProvider } from '@/components/providers/SiteConfigProvider';
import { buildTimeSiteConfig } from '@/config/site-config';

let authState = {
  loading: false,
  isAuthenticated: true,
  user: null as { id: string; email: string; role: string } | null,
};

let query = new URLSearchParams();

const replaceMock = vi.fn();
const pushMock = vi.fn();
const loginMock = vi.fn();
const phoneLoginMock = vi.fn();

vi.mock('@/components/providers', () => ({
  useAuth: () => ({ state: authState, login: loginMock, loginPhone: phoneLoginMock }),
}));

vi.mock('next/navigation', () => ({
  useRouter: () => ({ replace: replaceMock, push: pushMock }),
  useSearchParams: () => query,
}));

vi.mock('react-hot-toast', () => ({
  default: { success: vi.fn(), error: vi.fn() },
}));

beforeEach(() => {
  vi.clearAllMocks();
  authState = {
    loading: false,
    isAuthenticated: true,
    user: { id: 'user_1', email: 'murphy@example.test', role: 'pro' },
  };
  query = new URLSearchParams();
});

afterEach(() => {
  cleanup();
});

// The already-signed-in redirect is the leg of the ?next= round-trip that runs
// without any form interaction, so it pins the contract for both legs: where
// login sends the user afterwards.
describe('the ?next= round-trip', () => {
  it('returns a signed-in visitor to the internal path it came from', () => {
    query = new URLSearchParams({
      next: '/authorize?client_id=cloud-agent&code_challenge=x',
    });
    render(<LoginPage />);

    expect(replaceMock).toHaveBeenCalledWith('/authorize?client_id=cloud-agent&code_challenge=x');
  });

  it('falls back to the dashboard without a next', () => {
    render(<LoginPage />);

    expect(replaceMock).toHaveBeenCalledWith('/dashboard');
  });

  it.each([
    'https://evil.test/phish',
    '//evil.test/phish',
    '/\\evil.test/phish',
    // The URL parser strips \n before parsing, so this is //evil.test to the
    // browser. Arrives decoded from ?next=/%0A/evil.test/phish.
    '/\n/evil.test/phish',
  ])('never follows an off-site next (%j)', (next) => {
    query = new URLSearchParams({ next });
    render(<LoginPage />);

    expect(replaceMock).toHaveBeenCalledWith('/dashboard');
  });
});

describe('signup navigation', () => {
  it('keeps the email login route as the default', async () => {
    authState.isAuthenticated = false;
    loginMock.mockResolvedValueOnce(undefined);
    render(<LoginPage />);
    expect(
      screen.queryByRole('button', { name: 'Phone verification code' }),
    ).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Email or username'), {
      target: { value: 'email@example.test' },
    });
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'password' } });
    fireEvent.click(screen.getByRole('button', { name: 'Log In' }));
    await waitFor(() => expect(loginMock).toHaveBeenCalledWith('email@example.test', 'password'));
    expect(phoneLoginMock).not.toHaveBeenCalled();
  });

  it('logs in by phone through the auth provider and returns to the requested internal page', async () => {
    authState.isAuthenticated = false;
    query = new URLSearchParams({ next: '/authorize?client_id=cloud-agent' });
    phoneLoginMock.mockResolvedValueOnce(undefined);
    render(
      <SiteConfigProvider
        initialConfig={{
          ...buildTimeSiteConfig,
          features: { ...buildTimeSiteConfig.features, phoneAuth: true },
        }}
      >
        <LoginPage />
      </SiteConfigProvider>,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Phone verification code' }));
    expect(screen.queryByLabelText('Email')).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Mobile number'), { target: { value: '13812345678' } });
    fireEvent.change(screen.getByLabelText('Verification code'), { target: { value: '123456' } });
    fireEvent.click(screen.getByRole('button', { name: 'Log In' }));
    await waitFor(() => expect(phoneLoginMock).toHaveBeenCalledWith('13812345678', '123456'));
    await waitFor(() => expect(pushMock).toHaveBeenCalledWith('/authorize?client_id=cloud-agent'));
    expect(loginMock).not.toHaveBeenCalled();
  });
  it('offers exactly one signup entry', () => {
    // One, not two. The frame is chosen by the Site UI (see
    // `src/site-ui/`), and both the console's card and a module's frame are
    // handed `topbar`; a frame that also rendered its own copy would make this
    // two links, which is the regression this pins.
    authState.isAuthenticated = false;
    render(
      <SiteConfigProvider
        initialConfig={{
          ...buildTimeSiteConfig,
          branding: { ...buildTimeSiteConfig.branding },
        }}
      >
        <LoginPage />
      </SiteConfigProvider>,
    );
    expect(screen.getAllByRole('link', { name: 'Sign Up' })).toHaveLength(1);
  });

  it('does not advertise signup when registration is closed', () => {
    authState.isAuthenticated = false;
    render(
      <SiteConfigProvider
        initialConfig={{
          ...buildTimeSiteConfig,
          branding: {
            ...buildTimeSiteConfig.branding,
          },
          features: { ...buildTimeSiteConfig.features, publicSignup: false },
        }}
      >
        <LoginPage />
      </SiteConfigProvider>,
    );
    expect(screen.queryByRole('link', { name: 'Sign Up' })).not.toBeInTheDocument();
  });
});

describe('email or username', () => {
  function signIn(identifier: string, password = 'pw') {
    fireEvent.change(screen.getByLabelText('Email or username'), {
      target: { value: identifier },
    });
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: password } });
    fireEvent.click(screen.getByRole('button', { name: 'Log In' }));
  }

  beforeEach(() => {
    authState.isAuthenticated = false;
    authState.user = null;
    loginMock.mockReset().mockResolvedValue(undefined);
  });

  it('is one text field, not an email field', () => {
    render(<LoginPage />);

    const field = screen.getByLabelText('Email or username');
    expect(field).toHaveAttribute('type', 'text');
    expect(field).toHaveAttribute('autocomplete', 'username');
  });

  it('signs in with the login name of an account that has no email', async () => {
    render(<LoginPage />);

    signIn('  Admin.ops ');

    // Trimmed, and sent in the request's `email` field by the controller.
    await waitFor(() => expect(loginMock).toHaveBeenCalledWith('Admin.ops', 'pw'));
  });

  it('still signs in with an email address', async () => {
    render(<LoginPage />);

    signIn('user@example.test');

    await waitFor(() => expect(loginMock).toHaveBeenCalledWith('user@example.test', 'pw'));
  });

  it.each([
    ['not-an-email@', 'Please enter a valid email address'],
    ['x', 'Please enter a valid email address or username'],
    ['has space', 'Please enter a valid email address or username'],
    ['', 'Please enter your email or username'],
  ])('refuses %j', async (identifier, message) => {
    render(<LoginPage />);

    signIn(identifier);

    expect(await screen.findByText(message)).toBeInTheDocument();
    expect(loginMock).not.toHaveBeenCalled();
  });
});
