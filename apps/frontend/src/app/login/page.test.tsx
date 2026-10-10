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
const loginMock = vi.fn();

vi.mock('@/components/providers', () => ({
  useAuth: () => ({ state: authState, login: loginMock }),
}));

vi.mock('next/navigation', () => ({
  useRouter: () => ({ replace: replaceMock, push: vi.fn() }),
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
