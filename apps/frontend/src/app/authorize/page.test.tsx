// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import AuthorizePage from './page';

const REDIRECT = 'https://agents.staging.freeinference.org/auth/callback';

let authState = {
  loading: false,
  isAuthenticated: true,
  user: null as { id: string; email: string; role: string } | null,
};

let query = new URLSearchParams();

const replaceMock = vi.fn();
const createCodeMock = vi.fn();
const navigateToMock = vi.fn();

vi.mock('@/components/providers', () => ({
  useAuth: () => ({ state: authState }),
}));

vi.mock('next/navigation', () => ({
  useRouter: () => ({ replace: replaceMock, push: vi.fn() }),
  usePathname: () => '/authorize',
  useSearchParams: () => query,
}));

vi.mock('@/lib/api/identity', () => ({
  createAuthorizationCode: (...args: unknown[]) => createCodeMock(...args),
}));

// Keep internalPathOr real; only the full-page navigation is observed.
vi.mock('@/lib/utils/navigation', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/utils/navigation')>()),
  navigateTo: (url: string) => navigateToMock(url),
}));

function setQuery(params: Record<string, string>) {
  query = new URLSearchParams(params);
}

function validQuery(extra: Record<string, string> = {}) {
  setQuery({
    client_id: 'cloud-agent',
    redirect_uri: REDIRECT,
    code_challenge: 'challenge-value',
    ...extra,
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  authState = {
    loading: false,
    isAuthenticated: true,
    user: { id: 'user_1', email: 'murphy@example.test', role: 'pro' },
  };
  createCodeMock.mockResolvedValue({ code: 'code-123', expires_in: 60 });
});

afterEach(() => {
  cleanup();
});

describe('request validation', () => {
  it.each([
    ['client_id', { redirect_uri: REDIRECT, code_challenge: 'x' }],
    ['redirect_uri', { client_id: 'cloud-agent', code_challenge: 'x' }],
    ['code_challenge', { client_id: 'cloud-agent', redirect_uri: REDIRECT }],
  ])('a link missing %s gets an error, not a form', (_name, params) => {
    setQuery(params as Record<string, string>);
    render(<AuthorizePage />);

    expect(screen.getByRole('alert')).toHaveTextContent(/incomplete/i);
    expect(screen.queryByRole('button', { name: /continue/i })).not.toBeInTheDocument();
    expect(createCodeMock).not.toHaveBeenCalled();
    expect(replaceMock).not.toHaveBeenCalled();
  });

  it('an unknown client gets a plain answer, and its name is never rendered', () => {
    validQuery({ client_id: 'evil-app' });
    render(<AuthorizePage />);

    expect(screen.getByRole('alert')).toHaveTextContent(/does not know/i);
    expect(screen.queryByText(/evil-app/)).not.toBeInTheDocument();
    expect(createCodeMock).not.toHaveBeenCalled();
  });

  it('an unparseable redirect_uri is refused before anything else happens', () => {
    validQuery({ redirect_uri: 'not-a-url' });
    render(<AuthorizePage />);

    expect(screen.getByRole('alert')).toHaveTextContent(/invalid return address/i);
    expect(createCodeMock).not.toHaveBeenCalled();
  });

  it('an invalid link does not bounce an anonymous visitor to login', () => {
    authState = { loading: false, isAuthenticated: false, user: null };
    setQuery({ client_id: 'cloud-agent' });
    render(<AuthorizePage />);

    expect(screen.getByRole('alert')).toBeInTheDocument();
    expect(replaceMock).not.toHaveBeenCalled();
  });
});

describe('login round-trip', () => {
  it('sends an anonymous visitor to login with this URL as next', () => {
    authState = { loading: false, isAuthenticated: false, user: null };
    validQuery({ state: 'abc' });
    render(<AuthorizePage />);

    expect(replaceMock).toHaveBeenCalledWith(
      `/login?next=${encodeURIComponent(`/authorize?${query.toString()}`)}`,
    );
    expect(createCodeMock).not.toHaveBeenCalled();
  });

  it('waits for auth to resolve rather than redirecting a loading session', () => {
    authState = { loading: true, isAuthenticated: false, user: null };
    validQuery();
    render(<AuthorizePage />);

    expect(replaceMock).not.toHaveBeenCalled();
    expect(screen.queryByRole('button', { name: /continue/i })).not.toBeInTheDocument();
  });
});

describe('the consent step', () => {
  it('shows who is signing in to what, and nothing happens without the click', () => {
    validQuery();
    render(<AuthorizePage />);

    expect(screen.getByText(/murphy@example\.test/)).toBeInTheDocument();
    expect(screen.getByText(/agents\.staging\.freeinference\.org/)).toBeInTheDocument();
    expect(createCodeMock).not.toHaveBeenCalled();
    expect(navigateToMock).not.toHaveBeenCalled();
  });

  it('mints a code for exactly the requested scope and forwards code + state', async () => {
    validQuery({ state: 'req-state-1' });
    render(<AuthorizePage />);

    fireEvent.click(screen.getByRole('button', { name: /continue/i }));

    expect(createCodeMock).toHaveBeenCalledWith({
      client_id: 'cloud-agent',
      redirect_uri: REDIRECT,
      code_challenge: 'challenge-value',
      code_challenge_method: 'S256',
    });
    await waitFor(() => expect(navigateToMock).toHaveBeenCalledTimes(1));
    const target = new URL(navigateToMock.mock.calls[0][0]);
    expect(`${target.origin}${target.pathname}`).toBe(REDIRECT);
    expect(target.searchParams.get('code')).toBe('code-123');
    expect(target.searchParams.get('state')).toBe('req-state-1');
  });

  it('keeps the redirect_uri query the service registered, and omits absent state', async () => {
    validQuery({ redirect_uri: `${REDIRECT}?tenant=alpha` });
    render(<AuthorizePage />);

    fireEvent.click(screen.getByRole('button', { name: /continue/i }));

    await waitFor(() => expect(navigateToMock).toHaveBeenCalledTimes(1));
    const target = new URL(navigateToMock.mock.calls[0][0]);
    expect(target.searchParams.get('tenant')).toBe('alpha');
    expect(target.searchParams.get('code')).toBe('code-123');
    expect(target.searchParams.has('state')).toBe(false);
  });

  it('a refused mint shows the error and navigates nowhere', async () => {
    createCodeMock.mockRejectedValue(new Error('redirect_uri is not allowed'));
    validQuery();
    render(<AuthorizePage />);

    fireEvent.click(screen.getByRole('button', { name: /continue/i }));

    expect(await screen.findByRole('alert')).toHaveTextContent(/not allowed/i);
    expect(navigateToMock).not.toHaveBeenCalled();
    // The button recovers so the user can retry once the problem is fixed.
    expect(screen.getByRole('button', { name: /continue/i })).toBeEnabled();
  });
});
