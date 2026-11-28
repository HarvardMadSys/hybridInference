// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { APIError } from '@/lib/utils/errors';
import KeyAuthorizePage from './page';

const CALLBACK = 'https://agent.example.org/callback?state=abc';
const CHALLENGE = 'E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM';
const CANCEL_URL = 'https://agent.example.org/callback?state=abc&error=access_denied';
const REDIRECT_URL = 'https://agent.example.org/callback?state=abc&code=code-123';

let authState = {
  loading: false,
  isAuthenticated: true,
  user: null as { id: string; email: string | null; role: string } | null,
};

let accountsState: Array<{
  id: string;
  email: string | null;
  login_name?: string | null;
  role: string;
  active: boolean;
  signed_in: boolean;
  agent_access?: boolean | null;
}> = [];

let query = new URLSearchParams();

const replaceMock = vi.fn();
const checkMock = vi.fn();
const createCodeMock = vi.fn();
const navigateToMock = vi.fn();
const accountTokenMock = vi.fn();
const resendMock = vi.fn();
const getMeMock = vi.fn();
const rememberMock = vi.fn();

vi.mock('@/components/providers', () => ({
  useAuth: () => ({ state: authState, accounts: accountsState, accountsReady: true }),
}));

vi.mock('@/lib/api/auth', () => ({
  switchAccount: (...args: unknown[]) => accountTokenMock(...args),
  resendVerification: (...args: unknown[]) => resendMock(...args),
}));

vi.mock('@/lib/api/user', () => ({
  getMe: (...args: unknown[]) => getMeMock(...args),
}));

// The two requests are observed; reading the gateway's error type stays real.
vi.mock('@/lib/api/keyAuthorization', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/keyAuthorization')>()),
  checkKeyAuthorization: (...args: unknown[]) => checkMock(...args),
  createKeyAuthorizationCode: (...args: unknown[]) => createCodeMock(...args),
}));

vi.mock('@/lib/utils/authNext', () => ({
  rememberAuthNext: (...args: unknown[]) => rememberMock(...args),
}));

vi.mock('next/navigation', () => ({
  useRouter: () => ({ replace: replaceMock, push: vi.fn() }),
  usePathname: () => '/authorize/key',
  useSearchParams: () => query,
}));

// Keep internalPathOr real; only the full-page navigation is observed.
vi.mock('@/lib/utils/navigation', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/utils/navigation')>()),
  navigateTo: (url: string) => navigateToMock(url),
}));

const REQUEST = {
  callback_url: CALLBACK,
  code_challenge: CHALLENGE,
  code_challenge_method: 'S256',
};

function setQuery(params: Record<string, string>) {
  query = new URLSearchParams(params);
}

function validQuery(extra: Record<string, string> = {}) {
  setQuery({ ...REQUEST, ...extra });
}

function here(): string {
  return `/authorize/key?${query.toString()}`;
}

function refusal(
  status: number,
  type: string | null,
  message = 'Refused.',
  code = 'UNKNOWN_ERROR',
) {
  return new APIError(code, message, status, type ? { type } : undefined);
}

function me(extra: Record<string, unknown> = {}) {
  return {
    id: 'user_1',
    email: 'murphy@example.test',
    role: 'pro',
    status: 'active',
    email_verified: true,
    is_admin: false,
    created_at: '2026-11-01T00:00:00Z',
    ...extra,
  };
}

async function consentShown() {
  return screen.findByRole('button', { name: 'Allow' });
}

beforeEach(() => {
  vi.clearAllMocks();
  authState = {
    loading: false,
    isAuthenticated: true,
    user: { id: 'user_1', email: 'murphy@example.test', role: 'pro' },
  };
  accountsState = [];
  checkMock.mockResolvedValue({ app: 'agent.example.org', cancel_url: CANCEL_URL });
  createCodeMock.mockResolvedValue({
    code: 'code-123',
    redirect_url: REDIRECT_URL,
    expires_in: 180,
    app: 'agent.example.org',
  });
  getMeMock.mockResolvedValue(me());
  resendMock.mockResolvedValue({ message: 'sent' });
});

afterEach(() => {
  cleanup();
});

describe('a link the page refuses by itself', () => {
  it.each([
    ['callback_url', { code_challenge: CHALLENGE, code_challenge_method: 'S256' }],
    ['code_challenge', { callback_url: CALLBACK, code_challenge_method: 'S256' }],
    ['code_challenge_method', { callback_url: CALLBACK, code_challenge: CHALLENGE }],
  ])('without %s gets an error, not a form', (_name, params) => {
    setQuery(params);
    render(<KeyAuthorizePage />);

    expect(screen.getByRole('alert')).toHaveTextContent(/incomplete/i);
    expect(screen.queryByRole('button', { name: 'Allow' })).not.toBeInTheDocument();
    expect(checkMock).not.toHaveBeenCalled();
    expect(navigateToMock).not.toHaveBeenCalled();
    expect(replaceMock).not.toHaveBeenCalled();
  });

  it.each([
    ['a plain challenge method', { code_challenge_method: 'plain' }],
    ['a challenge that is not a SHA-256 digest', { code_challenge: 'short' }],
    ['a challenge with padding', { code_challenge: `${CHALLENGE.slice(0, 42)}=` }],
    ['a callback that is not an address', { callback_url: 'not-a-url' }],
  ])('with %s is not valid', (_name, extra) => {
    validQuery(extra);
    render(<KeyAuthorizePage />);

    expect(screen.getByRole('alert')).toHaveTextContent(/not valid/i);
    expect(checkMock).not.toHaveBeenCalled();
    expect(navigateToMock).not.toHaveBeenCalled();
  });

  it('does not bounce an anonymous visitor to login', () => {
    authState = { loading: false, isAuthenticated: false, user: null };
    setQuery({ callback_url: CALLBACK });
    render(<KeyAuthorizePage />);

    expect(screen.getByRole('alert')).toBeInTheDocument();
    expect(replaceMock).not.toHaveBeenCalled();
  });
});

describe('the gateway’s check of the request', () => {
  it('sends exactly the request the link carries', async () => {
    validQuery();
    render(<KeyAuthorizePage />);

    await consentShown();
    expect(checkMock).toHaveBeenCalledWith(REQUEST);
  });

  it('a callback the gateway refuses gets an error, and nothing leads to it', async () => {
    authState = { loading: false, isAuthenticated: false, user: null };
    checkMock.mockRejectedValue(refusal(400, 'invalid_request'));
    validQuery();
    render(<KeyAuthorizePage />);

    expect(await screen.findByRole('alert')).toHaveTextContent(/not valid for this deployment/i);
    expect(screen.queryByRole('button', { name: 'Cancel' })).not.toBeInTheDocument();
    expect(navigateToMock).not.toHaveBeenCalled();
    expect(replaceMock).not.toHaveBeenCalled();
  });

  it('says so when the deployment does not offer it', async () => {
    checkMock.mockRejectedValue(refusal(404, 'not_configured'));
    validQuery();
    render(<KeyAuthorizePage />);

    expect(await screen.findByRole('alert')).toHaveTextContent(
      /does not let applications create API keys/i,
    );
    expect(navigateToMock).not.toHaveBeenCalled();
  });

  it('offers to try again when the check could not be made', async () => {
    checkMock.mockRejectedValueOnce(new APIError('NETWORK_ERROR', 'Network request failed'));
    validQuery();
    render(<KeyAuthorizePage />);

    expect(await screen.findByRole('alert')).toHaveTextContent(/could not be checked/i);
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));

    await consentShown();
    expect(checkMock).toHaveBeenCalledTimes(2);
    expect(navigateToMock).not.toHaveBeenCalled();
  });
});

describe('the login round trip', () => {
  it('sends an anonymous visitor to login with this whole address as next', async () => {
    authState = { loading: false, isAuthenticated: false, user: null };
    validQuery();
    render(<KeyAuthorizePage />);

    await waitFor(() =>
      expect(replaceMock).toHaveBeenCalledWith(`/login?next=${encodeURIComponent(here())}`),
    );
    expect(decodeURIComponent(replaceMock.mock.calls[0][0].split('next=')[1])).toBe(
      `/authorize/key?callback_url=${encodeURIComponent(CALLBACK)}&code_challenge=${CHALLENGE}&code_challenge_method=S256`,
    );
    expect(createCodeMock).not.toHaveBeenCalled();
  });

  it('waits for the gateway to accept the request first', () => {
    authState = { loading: false, isAuthenticated: false, user: null };
    checkMock.mockReturnValue(new Promise(() => {}));
    validQuery();
    render(<KeyAuthorizePage />);

    expect(replaceMock).not.toHaveBeenCalled();
  });

  it('waits for the session to resolve rather than redirecting a loading one', async () => {
    authState = { loading: true, isAuthenticated: false, user: null };
    validQuery();
    render(<KeyAuthorizePage />);

    await waitFor(() => expect(checkMock).toHaveBeenCalled());
    expect(replaceMock).not.toHaveBeenCalled();
    expect(screen.queryByRole('button', { name: 'Allow' })).not.toBeInTheDocument();
  });
});

describe('the consent step', () => {
  it('names the application only by the host the gateway returned, and waits for a click', async () => {
    validQuery({ app_name: 'Totally Official Billing' });
    render(<KeyAuthorizePage />);

    await consentShown();
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent(
      'Allow agent.example.org to create an API key?',
    );
    expect(screen.queryByText(/Totally Official Billing/)).not.toBeInTheDocument();
    expect(screen.getByText(/murphy@example\.test/)).toBeInTheDocument();
    expect(screen.getByText(/counts against this account/)).toBeInTheDocument();
    expect(screen.getByText(/revoke the key at any time/)).toBeInTheDocument();
    expect(createCodeMock).not.toHaveBeenCalled();
    expect(navigateToMock).not.toHaveBeenCalled();
  });

  it('Allow asks for a code for exactly this request and goes where the gateway said', async () => {
    validQuery();
    render(<KeyAuthorizePage />);

    fireEvent.click(await consentShown());

    await waitFor(() => expect(navigateToMock).toHaveBeenCalledTimes(1));
    expect(createCodeMock).toHaveBeenCalledWith(REQUEST);
    expect(navigateToMock).toHaveBeenCalledWith(REDIRECT_URL);
    // Still busy while the page unloads: no second code.
    expect(screen.queryByRole('button', { name: 'Allow' })).not.toBeInTheDocument();
  });

  it('Cancel goes to the address the gateway built, and asks for no code', async () => {
    validQuery();
    render(<KeyAuthorizePage />);

    await consentShown();
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));

    expect(navigateToMock).toHaveBeenCalledWith(CANCEL_URL);
    expect(createCodeMock).not.toHaveBeenCalled();
  });

  it('a failed request shows the error, navigates nowhere and lets the visitor retry', async () => {
    createCodeMock.mockRejectedValueOnce(new APIError('SERVER_ERROR', 'Server error', 500));
    validQuery();
    render(<KeyAuthorizePage />);

    fireEvent.click(await consentShown());

    expect(await screen.findByRole('alert')).toBeInTheDocument();
    expect(navigateToMock).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Allow' })).toBeEnabled();
  });

  it('a request the gateway stops accepting leaves no way to its callback', async () => {
    createCodeMock.mockRejectedValueOnce(refusal(400, 'invalid_request'));
    validQuery();
    render(<KeyAuthorizePage />);

    fireEvent.click(await consentShown());

    expect(await screen.findByRole('alert')).toHaveTextContent(/not valid for this deployment/i);
    expect(screen.queryByRole('button', { name: 'Cancel' })).not.toBeInTheDocument();
    expect(navigateToMock).not.toHaveBeenCalled();
  });

  it('an account at the key ceiling is told to revoke one, and may try again', async () => {
    createCodeMock.mockRejectedValueOnce(refusal(409, 'key_limit_reached'));
    validQuery();
    render(<KeyAuthorizePage />);

    fireEvent.click(await consentShown());

    expect(await screen.findByRole('alert')).toHaveTextContent(/as many API keys as it may/i);
    expect(screen.getByRole('link', { name: 'Open API keys' })).toHaveAttribute(
      'href',
      '/dashboard/keys',
    );
    expect(navigateToMock).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'Allow' }));
    await waitFor(() => expect(navigateToMock).toHaveBeenCalledWith(REDIRECT_URL));
  });
});

describe('an account that may not create keys', () => {
  it('a suspended account is refused without an Allow', async () => {
    createCodeMock.mockRejectedValueOnce(
      refusal(403, null, 'Account is suspended. Please contact support.', 'ACCOUNT_SUSPENDED'),
    );
    validQuery();
    render(<KeyAuthorizePage />);

    fireEvent.click(await consentShown());

    expect(await screen.findByRole('alert')).toHaveTextContent(/suspended/i);
    expect(screen.queryByRole('button', { name: 'Allow' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(navigateToMock).toHaveBeenCalledWith(CANCEL_URL);
  });

  it('an account waiting for approval is told so before it agrees to anything', async () => {
    getMeMock.mockRejectedValue(
      refusal(403, null, 'Account is pending_approval. Please contact support.'),
    );
    validQuery();
    render(<KeyAuthorizePage />);

    expect(await screen.findByRole('alert')).toHaveTextContent(/waiting for an administrator/i);
    expect(screen.queryByRole('button', { name: 'Allow' })).not.toBeInTheDocument();
    expect(createCodeMock).not.toHaveBeenCalled();
  });
});

describe('an address still to verify', () => {
  it('is said on the page before Allow, with resend and re-check', async () => {
    getMeMock.mockResolvedValueOnce(me({ email_verified: false }));
    validQuery();
    render(<KeyAuthorizePage />);

    expect(await screen.findByText('Verify your email to continue')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Allow' })).not.toBeInTheDocument();
    expect(screen.getByText(/murphy@example\.test is yours/)).toBeInTheDocument();
    // The verification page can bring the visitor back here.
    expect(rememberMock).toHaveBeenCalledWith(here());

    fireEvent.click(screen.getByRole('button', { name: 'Resend verification email' }));
    await waitFor(() => expect(resendMock).toHaveBeenCalledWith('murphy@example.test'));
    expect(await screen.findByRole('status')).toHaveTextContent(
      /on its way to murphy@example\.test/,
    );

    // Still unverified: the page stays and says so.
    getMeMock.mockResolvedValueOnce(me({ email_verified: false }));
    fireEvent.click(screen.getByRole('button', { name: 'I have verified my email' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(/not verified yet/i);

    // Verified now: back to the consent step.
    getMeMock.mockResolvedValueOnce(me());
    fireEvent.click(screen.getByRole('button', { name: 'I have verified my email' }));
    await consentShown();
    expect(navigateToMock).not.toHaveBeenCalled();
  });

  it('counts the session gate’s unverified refusal as one too', async () => {
    getMeMock.mockRejectedValueOnce(
      refusal(
        403,
        null,
        'Email not verified. Please check your email for the verification link.',
        'EMAIL_NOT_VERIFIED',
      ),
    );
    validQuery();
    render(<KeyAuthorizePage />);

    expect(await screen.findByText('Verify your email to continue')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Allow' })).not.toBeInTheDocument();
  });

  it('says a failed resend rather than claiming it was sent', async () => {
    getMeMock.mockResolvedValueOnce(me({ email_verified: false }));
    resendMock.mockRejectedValueOnce(new APIError('RATE_LIMIT_EXCEEDED', 'Too many', 429));
    validQuery();
    render(<KeyAuthorizePage />);

    fireEvent.click(await screen.findByRole('button', { name: 'Resend verification email' }));

    expect(await screen.findByRole('alert')).toBeInTheDocument();
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });
});

describe('several accounts on the browser', () => {
  function account(id: string, extra: Partial<(typeof accountsState)[number]> = {}) {
    return {
      id,
      email: `${id}@example.test`,
      role: 'free',
      active: false,
      signed_in: true,
      agent_access: false,
      ...extra,
    };
  }

  beforeEach(() => {
    accountsState = [account('user_1', { active: true }), account('user_2')];
    accountTokenMock.mockResolvedValue({ access_token: 'token-for-user_2' });
  });

  it('asks which one, starting from the console’s, whatever their Cloud Agent access', async () => {
    validQuery();
    render(<KeyAuthorizePage />);

    await consentShown();
    expect(screen.getByText(/Choose the account agent\.example\.org gets/)).toBeInTheDocument();
    expect(screen.getByRole('radio', { name: /user_1@example\.test/ })).toBeChecked();
    expect(screen.getByRole('radio', { name: /user_2@example\.test/ })).toBeEnabled();
  });

  it('creates the key on another account with that account’s own token', async () => {
    validQuery();
    render(<KeyAuthorizePage />);

    await consentShown();
    fireEvent.click(screen.getByRole('radio', { name: /user_2@example\.test/ }));
    fireEvent.click(screen.getByRole('button', { name: 'Allow' }));

    await waitFor(() => expect(navigateToMock).toHaveBeenCalledWith(REDIRECT_URL));
    expect(accountTokenMock).toHaveBeenCalledWith('user_2', { activate: false });
    expect(createCodeMock).toHaveBeenCalledWith(REQUEST, 'token-for-user_2');
  });

  it('offers a signed-out account its own sign-in, coming back here', async () => {
    accountsState = [
      account('user_1', { active: true }),
      account('user_2'),
      account('user_3', { signed_in: false }),
    ];
    validQuery();
    render(<KeyAuthorizePage />);

    await consentShown();
    expect(screen.getByRole('radio', { name: /user_3@example\.test/ })).toBeDisabled();
    expect(screen.getByRole('link', { name: 'Sign in again' })).toHaveAttribute(
      'href',
      `/login?add=1&identifier=user_3%40example.test&next=${encodeURIComponent(here())}`,
    );
    expect(screen.getByRole('link', { name: 'Use another account' })).toHaveAttribute(
      'href',
      `/login?add=1&next=${encodeURIComponent(here())}`,
    );
  });

  it('asks another account to verify when the gateway says so, and retries for it', async () => {
    accountTokenMock.mockRejectedValueOnce(
      refusal(
        403,
        null,
        'Email not verified. Please verify your email to continue.',
        'EMAIL_NOT_VERIFIED',
      ),
    );
    validQuery();
    render(<KeyAuthorizePage />);

    await consentShown();
    fireEvent.click(screen.getByRole('radio', { name: /user_2@example\.test/ }));
    fireEvent.click(screen.getByRole('button', { name: 'Allow' }));

    expect(await screen.findByText('Verify your email to continue')).toBeInTheDocument();
    expect(screen.getByText(/user_2@example\.test is yours/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'I have verified my email' }));
    await waitFor(() => expect(navigateToMock).toHaveBeenCalledWith(REDIRECT_URL));
    expect(createCodeMock).toHaveBeenCalledWith(REQUEST, 'token-for-user_2');
  });

  it('offers the browser’s accounts to a tab with none active instead of login', async () => {
    authState = { loading: false, isAuthenticated: false, user: null };
    validQuery();
    render(<KeyAuthorizePage />);

    await consentShown();
    expect(replaceMock).not.toHaveBeenCalled();
    expect(screen.getByRole('radio', { name: /user_1@example\.test/ })).toBeChecked();
  });
});
