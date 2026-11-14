// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import LoginPage from './page';
import { SiteConfigProvider } from '@/components/providers/SiteConfigProvider';
import { buildTimeSiteConfig } from '@/config/site-config';
import { APIError } from '@/lib/utils/errors';
import { AuthAppearanceProvider } from '@/site-ui/appearance';
import type { AuthAppearance } from '@/site-ui/contract';

let authState = {
  loading: false,
  isAuthenticated: true,
  user: null as { id: string; email: string | null; role: string } | null,
};

let query = new URLSearchParams();

const replaceMock = vi.fn();
const pushMock = vi.fn();
const loginMock = vi.fn();
const phoneLoginMock = vi.fn();
const switchAccountMock = vi.fn();
const removeAccountMock = vi.fn();
const completeExternalSignInMock = vi.fn();
let accountsState: Array<{
  id: string;
  email: string | null;
  login_name?: string | null;
  user_name?: string | null;
  role: string;
  active: boolean;
  signed_in: boolean;
  sso_provider?: { id: string; name: string } | null;
}> = [];

vi.mock('@/components/providers', () => ({
  useAuth: () => ({
    state: authState,
    login: loginMock,
    loginPhone: phoneLoginMock,
    completeExternalSignIn: completeExternalSignInMock,
    accounts: accountsState,
    switchAccount: switchAccountMock,
    removeAccount: removeAccountMock,
  }),
}));

// The requests single sign-on makes; what reads a code or builds an address
// stays real.
const sso = vi.hoisted(() => ({
  discoverSso: vi.fn(),
  getPendingSsoLink: vi.fn(),
  confirmSsoLink: vi.fn(),
  cancelSsoLink: vi.fn(),
}));
vi.mock('@/lib/api/sso', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/sso')>()),
  ...sso,
}));

// jsdom's location cannot be watched; the page leaves through this.
const navigateToMock = vi.hoisted(() => vi.fn());
vi.mock('@/lib/utils/navigation', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/utils/navigation')>()),
  navigateTo: navigateToMock,
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
  accountsState = [];
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
    expect(screen.queryByRole('button', { name: 'SMS code login' })).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Email or login name'), {
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
    fireEvent.click(screen.getByRole('button', { name: 'SMS code login' }));
    expect(screen.queryByLabelText('Email or login name')).not.toBeInTheDocument();
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

describe('email or login name', () => {
  function signIn(identifier: string, password = 'pw') {
    fireEvent.change(screen.getByLabelText('Email or login name'), {
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

    const field = screen.getByLabelText('Email or login name');
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
    ['x', 'Please enter a valid email address or login name'],
    ['has space', 'Please enter a valid email address or login name'],
    ['', 'Please enter your email or login name'],
  ])('refuses %j', async (identifier, message) => {
    render(<LoginPage />);

    signIn(identifier);

    expect(await screen.findByText(message)).toBeInTheDocument();
    expect(loginMock).not.toHaveBeenCalled();
  });
});

describe('accounts already signed in on this browser', () => {
  beforeEach(() => {
    authState.isAuthenticated = false;
    authState.user = null;
    accountsState = [
      {
        id: 'ada',
        email: 'ada@example.test',
        user_name: 'Ada',
        role: 'pro',
        active: false,
        signed_in: true,
      },
      {
        id: 'bob',
        email: 'bob@example.test',
        user_name: 'Bob',
        role: 'free',
        active: false,
        signed_in: false,
      },
    ];
    switchAccountMock.mockReset().mockResolvedValue(undefined);
    removeAccountMock.mockReset().mockResolvedValue(undefined);
  });

  it('are offered before the sign-in form', () => {
    render(<LoginPage />);

    expect(screen.getByRole('heading', { name: 'Choose an account' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Ada/ })).toBeInTheDocument();
    expect(screen.getByText(/bob@example\.test · Signed out/)).toBeInTheDocument();
    expect(screen.queryByLabelText('Password')).not.toBeInTheDocument();
  });

  it('continue as a signed-in account without a password', async () => {
    query = new URLSearchParams({ next: '/dashboard/settings' });
    render(<LoginPage />);

    fireEvent.click(screen.getByRole('button', { name: /Ada/ }));

    await waitFor(() => expect(switchAccountMock).toHaveBeenCalledWith('ada'));
    await waitFor(() => expect(pushMock).toHaveBeenCalledWith('/dashboard/settings'));
    expect(loginMock).not.toHaveBeenCalled();
  });

  it('send a signed-out account to the form with its address filled in', () => {
    render(<LoginPage />);

    fireEvent.click(screen.getByRole('button', { name: /^B/ }));

    expect(screen.getByLabelText('Email or login name')).toHaveValue('bob@example.test');
    expect(switchAccountMock).not.toHaveBeenCalled();
  });

  it('can each be removed from the browser', async () => {
    render(<LoginPage />);

    fireEvent.click(screen.getByRole('button', { name: 'Remove bob@example.test' }));

    await waitFor(() => expect(removeAccountMock).toHaveBeenCalledWith('bob'));
  });

  it('give way to the form for another account, and come back', () => {
    render(<LoginPage />);

    fireEvent.click(screen.getByRole('button', { name: 'Use another account' }));
    expect(screen.getByLabelText('Email or login name')).toHaveValue('');

    fireEvent.click(screen.getByRole('button', { name: 'Back to your accounts' }));
    expect(screen.getByRole('heading', { name: 'Choose an account' })).toBeInTheDocument();
  });

  it('do not stop a signed-in visitor from adding one', () => {
    authState.isAuthenticated = true;
    authState.user = { id: 'ada', email: 'ada@example.test', role: 'pro' };
    query = new URLSearchParams({ add: '1', next: '/dashboard' });
    render(<LoginPage />);

    expect(replaceMock).not.toHaveBeenCalled();
    expect(screen.getByRole('heading', { name: 'Add another account' })).toBeInTheDocument();
    expect(screen.getByLabelText('Email or login name')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(pushMock).toHaveBeenCalledWith('/dashboard');
  });
});

function renderLogin({ ssoOffered = true, phoneAuth = false } = {}) {
  return render(
    <SiteConfigProvider
      initialConfig={{
        ...buildTimeSiteConfig,
        features: { ...buildTimeSiteConfig.features, phoneAuth },
        sso: { enabled: ssoOffered },
      }}
    >
      <LoginPage />
    </SiteConfigProvider>,
  );
}

const ssoEntry = () => screen.queryByRole('button', { name: 'Log in with SSO' });

describe('the single sign-on entry', () => {
  beforeEach(() => {
    authState.isAuthenticated = false;
    authState.user = null;
  });

  it('is absent unless the gateway offers single sign-on', () => {
    // The default configuration is also what an older gateway resolves to.
    render(<LoginPage />);
    expect(ssoEntry()).not.toBeInTheDocument();
    cleanup();

    renderLogin({ ssoOffered: false });
    expect(ssoEntry()).not.toBeInTheDocument();
    expect(document.querySelector('[data-auth="sso"]')).toBeNull();
  });

  it('is one entry under the password form, after a divider', () => {
    renderLogin();

    const block = document.querySelector('[data-auth="sso"]')!;
    expect(block).toHaveTextContent('or');
    // One entry whichever provider answers: the page names no organization.
    expect(block.querySelectorAll('button')).toHaveLength(1);
    expect(block).toContainElement(ssoEntry());
    const form = screen.getByLabelText('Password').closest('form')!;
    expect(form.compareDocumentPosition(block) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('is under the SMS form as well', () => {
    renderLogin({ phoneAuth: true });
    fireEvent.click(screen.getByRole('button', { name: 'SMS code login' }));

    expect(screen.getByLabelText('Mobile number')).toBeInTheDocument();
    expect(ssoEntry()).toBeInTheDocument();
  });

  it('waits behind the account chooser, with the form', () => {
    accountsState = [
      { id: 'ada', email: 'ada@example.test', role: 'pro', active: false, signed_in: true },
    ];
    renderLogin();
    expect(ssoEntry()).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Use another account' }));
    expect(ssoEntry()).toBeInTheDocument();
  });

  it('is offered when adding another account', () => {
    authState.isAuthenticated = true;
    authState.user = { id: 'ada', email: 'ada@example.test', role: 'pro' };
    query = new URLSearchParams({ add: '1' });
    renderLogin();

    expect(ssoEntry()).toBeInTheDocument();
  });
});

describe('the single sign-on email step', () => {
  beforeEach(() => {
    authState.isAuthenticated = false;
    authState.user = null;
    sso.discoverSso.mockReset();
  });

  function openStep() {
    fireEvent.click(ssoEntry()!);
    return screen.getByLabelText('Work or school email');
  }

  function submitEmail(email: string) {
    fireEvent.change(openStep(), { target: { value: email } });
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }));
  }

  it('takes the place of the form, and gives it back', () => {
    renderLogin({ phoneAuth: true });
    openStep();

    // The step says what it is for, in place of the sign-in form's own line.
    expect(
      screen.getByText(
        'Enter your work or school email. We will send you to your organization to sign in.',
      ),
    ).toBeInTheDocument();

    expect(screen.queryByLabelText('Password')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'SMS code login' })).not.toBeInTheDocument();
    expect(ssoEntry()).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Continue' })).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Back to log in' }));
    expect(screen.getByLabelText('Password')).toBeInTheDocument();
    expect(screen.queryByLabelText('Work or school email')).not.toBeInTheDocument();
  });

  it('starts with an address already typed into the form', () => {
    renderLogin();
    fireEvent.change(screen.getByLabelText('Email or login name'), {
      target: { value: 'alice@example.edu' },
    });

    expect(openStep()).toHaveValue('alice@example.edu');
  });

  it('sends the browser to the provider that answers for the email', async () => {
    query = new URLSearchParams({ next: '/authorize?client_id=cloud-agent' });
    sso.discoverSso.mockResolvedValue('https://idp.example.edu/authorize?state=abc');
    renderLogin();

    submitEmail('  alice@example.edu ');

    await waitFor(() =>
      expect(navigateToMock).toHaveBeenCalledWith('https://idp.example.edu/authorize?state=abc'),
    );
    // The page it comes back to goes on to where the visitor was going.
    expect(sso.discoverSso).toHaveBeenCalledExactlyOnceWith(
      'alice@example.edu',
      '/authorize?client_id=cloud-agent',
    );
    // Leaving: the button stays busy rather than inviting a second attempt.
    expect(screen.getByRole('button', { name: 'Continue' })).toBeDisabled();
  });

  it('says so, and stays, when no provider answers for the email', async () => {
    sso.discoverSso.mockResolvedValue(null);
    renderLogin();

    submitEmail('alice@nowhere.test');

    expect(
      await screen.findByText(
        'Your organization has not set up single sign-on here. Use another sign-in method.',
      ),
    ).toBeInTheDocument();
    expect(screen.getByLabelText('Work or school email')).toHaveValue('alice@nowhere.test');
    expect(screen.getByRole('button', { name: 'Continue' })).toBeEnabled();
    expect(navigateToMock).not.toHaveBeenCalled();
  });

  it.each([
    [503, 'Single sign-on is unavailable right now. Use another sign-in method.'],
    [429, 'Too many sign-in attempts. Please try again later.'],
  ])('explains a %i from the gateway', async (status, message) => {
    sso.discoverSso.mockRejectedValue(new APIError('UNKNOWN_ERROR', 'raw gateway text', status));
    renderLogin();

    submitEmail('alice@example.edu');

    expect(await screen.findByRole('alert')).toHaveTextContent(message);
    expect(screen.getByLabelText('Work or school email')).toBeInTheDocument();
    expect(navigateToMock).not.toHaveBeenCalled();
  });

  it('asks for a real address before asking the gateway', async () => {
    renderLogin();

    submitEmail('not-an-email');

    expect(await screen.findByText('Please enter a valid email address')).toBeInTheDocument();
    expect(sso.discoverSso).not.toHaveBeenCalled();
  });
});

describe('single sign-on under a distribution\u2019s sign-in skin', () => {
  // The class names a distribution's module hands the shared forms.
  const SKIN: AuthAppearance = {
    form: 'x-form',
    field: 'x-field',
    label: 'x-label',
    labelRow: 'x-label-row',
    input: 'x-input',
    inputError: 'x-input-error',
    hint: 'x-hint',
    error: 'x-error',
    linkButton: 'x-link',
    submit: 'x-submit',
    notice: 'x-notice',
    noticeError: 'x-notice-error',
    noticeOk: 'x-notice-ok',
    consentBlock: 'x-consent',
    loading: 'x-loading',
    loadingWrap: 'x-loading-wrap',
  };

  function renderSkinned() {
    return render(
      <AuthAppearanceProvider value={SKIN}>
        <SiteConfigProvider initialConfig={{ ...buildTimeSiteConfig, sso: { enabled: true } }}>
          <LoginPage />
        </SiteConfigProvider>
      </AuthAppearanceProvider>,
    );
  }

  beforeEach(() => {
    authState.isAuthenticated = false;
    authState.user = null;
  });

  it('draws the entry in the skin\u2019s own link and small print', () => {
    renderSkinned();

    const block = document.querySelector<HTMLElement>('[data-auth="sso"]')!;
    expect(ssoEntry()).toHaveClass('x-link');
    expect(within(block).getByText('or')).toHaveClass('x-hint');
    // The rules beside the neutral divider are the console's colors, not the skin's.
    expect(block.querySelector('[aria-hidden="true"]')).toBeNull();
  });

  it('keeps the way back out of the form, in the row the page spaces itself', () => {
    renderSkinned();
    fireEvent.click(ssoEntry()!);

    const back = screen.getByRole('button', { name: 'Back to log in' });
    expect(back).toHaveClass('x-link');
    // A distribution's form class need not space its children.
    expect(back.closest('form')).toBeNull();
    expect(back.closest('[data-auth="secondary-actions"]')).toHaveClass('mt-4');
    expect(screen.getByRole('button', { name: 'Continue' })).toHaveClass('x-submit');
    expect(screen.getByLabelText('Work or school email')).toHaveClass('x-input');
  });

  it('does the same for declining a waiting identity', async () => {
    authState.isAuthenticated = true;
    authState.user = { id: 'alice', email: 'alice@example.edu', role: 'free' };
    query = new URLSearchParams({ sso: 'link' });
    sso.getPendingSsoLink.mockReset().mockResolvedValue({
      provider_id: 'example-univ',
      provider_name: 'Example University',
      email: 'alice@example.edu',
    });
    renderSkinned();

    const decline = await screen.findByRole('button', { name: 'Not now' });
    expect(decline).toHaveClass('x-link');
    expect(decline.closest('[data-auth="form"]')).toBeNull();
    expect(decline.closest('[data-auth="secondary-actions"]')).toHaveClass('mt-4');
  });
});

describe('a failed single sign-on (?sso_error)', () => {
  beforeEach(() => {
    authState.isAuthenticated = false;
    authState.user = null;
  });

  it.each([
    ['unavailable', 'Single sign-on is unavailable right now. Use another sign-in method.'],
    ['rate_limited', 'Too many sign-in attempts. Please try again later.'],
    ['cancelled', 'Single sign-on was cancelled.'],
    ['expired', 'The sign-in took too long. Start again.'],
    ['failed', 'Single sign-on failed. Try again later.'],
    [
      'email_untrusted',
      'We could not confirm the email of this organization account. Contact your administrator.',
    ],
    [
      'identity_conflict',
      'This account is already connected to a different identity from the same organization. Contact your administrator.',
    ],
    ['identity_in_use', 'This organization account is already connected to another account.'],
    [
      'email_in_use',
      'The email of this organization account already belongs to another account here. Sign in to that account to connect it.',
    ],
    [
      'no_account',
      'This organization account has no account here yet. Contact your administrator.',
    ],
    ['pending', 'Your registration is pending admin approval.'],
    ['suspended', 'This account is suspended.'],
    ['inactive', 'This account is not active. Please contact support.'],
    [
      'browser_full',
      'This browser is already signed in to the maximum number of accounts. Sign one of them out first.',
    ],
    // A code from a later gateway reads as a plain failure.
    ['something_new', 'Single sign-on failed. Try again later.'],
  ])('explains %s above the form', (code, message) => {
    query = new URLSearchParams({ sso_error: code });
    renderLogin();

    const notice = screen.getByRole('alert');
    expect(notice).toHaveTextContent(message);
    const form = screen.getByLabelText('Password').closest('form')!;
    expect(notice.compareDocumentPosition(form) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('is shown to a visitor who is signed in, rather than sending them on', () => {
    // browser_full can only happen to one: the chooser is where it is fixed.
    authState.isAuthenticated = true;
    authState.user = { id: 'ada', email: 'ada@example.test', role: 'pro' };
    accountsState = [
      { id: 'ada', email: 'ada@example.test', role: 'pro', active: true, signed_in: true },
    ];
    query = new URLSearchParams({ sso_error: 'browser_full' });
    renderLogin();

    expect(replaceMock).not.toHaveBeenCalled();
    expect(screen.getByRole('alert')).toHaveTextContent('maximum number of accounts');
    expect(screen.getByRole('button', { name: 'Remove ada@example.test' })).toBeInTheDocument();
  });
});

describe('coming back signed in (?sso=complete)', () => {
  beforeEach(() => {
    authState.isAuthenticated = false;
    authState.user = null;
    completeExternalSignInMock.mockReset().mockResolvedValue(undefined);
  });

  it('takes up the session, then goes on to where the visitor was going', async () => {
    query = new URLSearchParams({ sso: 'complete', next: '/dashboard/settings' });
    let finish: () => void = () => undefined;
    completeExternalSignInMock.mockReturnValue(
      new Promise<void>((resolve) => {
        finish = resolve;
      }),
    );
    renderLogin();

    // The loading state, never the form.
    expect(screen.getByText('Signing you in…')).toBeInTheDocument();
    expect(screen.getByRole('status')).toBeInTheDocument();
    expect(screen.queryByLabelText('Password')).not.toBeInTheDocument();
    expect(ssoEntry()).not.toBeInTheDocument();
    expect(completeExternalSignInMock).toHaveBeenCalled();
    expect(replaceMock).not.toHaveBeenCalled();

    finish();
    await waitFor(() => expect(replaceMock).toHaveBeenCalledWith('/dashboard/settings'));
    expect(loginMock).not.toHaveBeenCalled();
  });

  it('does not send an already signed-in tab on before the new session is taken up', async () => {
    // The tab may still show the account it had before the round trip.
    authState.isAuthenticated = true;
    authState.user = { id: 'before', email: 'before@example.test', role: 'pro' };
    query = new URLSearchParams({ sso: 'complete', next: '/dashboard/keys' });
    completeExternalSignInMock.mockReturnValue(new Promise<void>(() => undefined));
    renderLogin();

    expect(screen.getByText('Signing you in…')).toBeInTheDocument();
    expect(replaceMock).not.toHaveBeenCalled();
  });

  it.each(['https://evil.test/phish', '//evil.test/phish'])(
    'never follows an off-site next (%j)',
    async (next) => {
      query = new URLSearchParams({ sso: 'complete', next });
      renderLogin();

      await waitFor(() => expect(replaceMock).toHaveBeenCalledWith('/dashboard'));
    },
  );

  it('falls back to the form, with the reason, when the session cannot be taken up', async () => {
    query = new URLSearchParams({ sso: 'complete', next: '/dashboard/settings' });
    completeExternalSignInMock.mockRejectedValue(new Error('no session'));
    renderLogin();

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Single sign-on failed. Try again later.',
    );
    expect(screen.getByLabelText('Password')).toBeInTheDocument();
    expect(screen.queryByText('Signing you in…')).not.toBeInTheDocument();
    expect(replaceMock).not.toHaveBeenCalled();
  });
});

describe('an identity waiting for the account owner (?sso=link)', () => {
  const waiting = {
    provider_id: 'example-univ',
    provider_name: 'Example University',
    email: 'alice@example.edu',
  };
  const panel = () =>
    screen.queryByRole('heading', { name: 'Connect your Example University account?' });
  const linkNotice = () =>
    screen.queryByText(
      'An account already exists for this email. Sign in the way you usually do to connect your Example University account.',
    );

  beforeEach(() => {
    authState.isAuthenticated = false;
    authState.user = null;
    query = new URLSearchParams({ sso: 'link', next: '/dashboard/keys' });
    sso.getPendingSsoLink.mockReset().mockResolvedValue(waiting);
    sso.confirmSsoLink.mockReset().mockResolvedValue({ message: 'Single sign-on connected.' });
    sso.cancelSsoLink
      .mockReset()
      .mockResolvedValue({ message: 'Single sign-on was not connected.' });
    loginMock.mockReset().mockResolvedValue(undefined);
  });

  it('never sends a signed-in visitor on before the question is answered', async () => {
    authState.isAuthenticated = true;
    authState.user = { id: 'alice', email: 'alice@example.edu', role: 'free' };
    renderLogin();

    // Read from the cookie first; nothing is drawn, and nobody is sent on.
    expect(screen.getByRole('status')).toBeInTheDocument();
    expect(await screen.findByRole('heading', { name: /Connect your/ })).toBeInTheDocument();
    expect(replaceMock).not.toHaveBeenCalled();
  });

  it('says the sign-in took too long when nothing is waiting any more', async () => {
    sso.getPendingSsoLink.mockResolvedValue(null);
    renderLogin();

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'The sign-in took too long. Start again.',
    );
    expect(screen.getByLabelText('Email or login name')).toHaveValue('');
    expect(screen.getByLabelText('Password')).toBeInTheDocument();
    expect(panel()).not.toBeInTheDocument();
    expect(linkNotice()).not.toBeInTheDocument();
  });

  it('asks the owner directly when the browser is already using that account', async () => {
    // Compared the way the gateway compares it: without regard to case.
    authState.isAuthenticated = true;
    authState.user = { id: 'alice', email: 'Alice@Example.EDU', role: 'free' };
    renderLogin();

    expect(await screen.findByRole('heading', { name: /Connect your/ })).toBe(panel());
    expect(
      screen.getByText(
        'alice@example.edu will be able to sign in to this account through Example University.',
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        'If you did not create this account yourself, review its API keys after connecting.',
      ),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText('Password')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Connect' }));
    await waitFor(() => expect(pushMock).toHaveBeenCalledWith('/dashboard/keys'));
    expect(sso.confirmSsoLink).toHaveBeenCalledTimes(1);
    expect(sso.cancelSsoLink).not.toHaveBeenCalled();
  });

  it('drops the identity on "Not now", and goes on', async () => {
    authState.isAuthenticated = true;
    authState.user = { id: 'alice', email: 'alice@example.edu', role: 'free' };
    renderLogin();

    fireEvent.click(await screen.findByRole('button', { name: 'Not now' }));

    await waitFor(() => expect(pushMock).toHaveBeenCalledWith('/dashboard/keys'));
    expect(sso.cancelSsoLink).toHaveBeenCalledTimes(1);
    expect(sso.confirmSsoLink).not.toHaveBeenCalled();
  });

  it('asks a signed-out visitor to sign in the usual way first, then asks', async () => {
    renderLogin();

    expect(await screen.findByLabelText('Email or login name')).toHaveValue('alice@example.edu');
    expect(linkNotice()).toBeInTheDocument();
    expect(panel()).not.toBeInTheDocument();
    // A second round trip would replace the identity that is waiting.
    expect(ssoEntry()).not.toBeInTheDocument();

    loginMock.mockImplementation(async () => {
      authState = {
        ...authState,
        isAuthenticated: true,
        user: { id: 'alice', email: 'alice@example.edu', role: 'free' },
      };
    });
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'Secret-1' } });
    fireEvent.click(screen.getByRole('button', { name: 'Log In' }));

    // The sign-in itself is the usual one, and it does not leave the page.
    expect(await screen.findByRole('heading', { name: /Connect your/ })).toBe(panel());
    expect(loginMock).toHaveBeenCalledWith('alice@example.edu', 'Secret-1');
    expect(pushMock).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'Connect' }));
    await waitFor(() => expect(pushMock).toHaveBeenCalledWith('/dashboard/keys'));
  });

  it('asks the same of a visitor signed in to a different account', async () => {
    authState.isAuthenticated = true;
    authState.user = { id: 'bob', email: 'bob@example.test', role: 'free' };
    accountsState = [
      { id: 'bob', email: 'bob@example.test', role: 'free', active: true, signed_in: true },
    ];
    renderLogin();

    // The form, not the chooser: it is what the notice asks for.
    expect(await screen.findByLabelText('Email or login name')).toHaveValue('alice@example.edu');
    expect(linkNotice()).toBeInTheDocument();
    expect(panel()).not.toBeInTheDocument();
    expect(replaceMock).not.toHaveBeenCalled();
  });

  it('asks after an SMS sign-in too', async () => {
    phoneLoginMock.mockReset().mockImplementation(async () => {
      authState = {
        ...authState,
        isAuthenticated: true,
        user: { id: 'alice', email: 'alice@example.edu', role: 'free' },
      };
    });
    renderLogin({ phoneAuth: true });
    fireEvent.click(await screen.findByRole('button', { name: 'SMS code login' }));
    fireEvent.change(screen.getByLabelText('Mobile number'), { target: { value: '13812345678' } });
    fireEvent.change(screen.getByLabelText('Verification code'), { target: { value: '123456' } });
    fireEvent.click(screen.getByRole('button', { name: 'Log In' }));

    expect(await screen.findByRole('heading', { name: /Connect your/ })).toBe(panel());
    expect(pushMock).not.toHaveBeenCalled();
  });

  it.each([
    [
      409,
      'This single sign-on belongs to a different email address than the account you are signed in to.',
    ],
    [403, 'Account is not active.'],
  ])('shows why the gateway refused with %i', async (status, message) => {
    authState.isAuthenticated = true;
    authState.user = { id: 'alice', email: 'alice@example.edu', role: 'free' };
    // A 409 otherwise reads as "This email is already registered".
    sso.confirmSsoLink.mockRejectedValue(new APIError('USER_ALREADY_EXISTS', message, status));
    renderLogin();

    fireEvent.click(await screen.findByRole('button', { name: 'Connect' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(message);
    expect(pushMock).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Connect' })).toBeEnabled();
  });

  it('says the sign-in took too long when the identity expired before the answer', async () => {
    authState.isAuthenticated = true;
    authState.user = { id: 'alice', email: 'alice@example.edu', role: 'free' };
    sso.confirmSsoLink.mockRejectedValue(new APIError('UNKNOWN_ERROR', 'Nothing is waiting.', 404));
    renderLogin();

    fireEvent.click(await screen.findByRole('button', { name: 'Connect' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'The sign-in took too long. Start again.',
    );
    // Nothing left to connect or to drop; the visitor is signed in and goes on.
    expect(screen.queryByRole('button', { name: 'Connect' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Not now' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }));
    expect(pushMock).toHaveBeenCalledWith('/dashboard/keys');
  });
});

describe('a signed-out account that signs in through an identity provider', () => {
  beforeEach(() => {
    authState.isAuthenticated = false;
    authState.user = null;
    accountsState = [
      {
        id: 'carol',
        email: 'carol@example.edu',
        user_name: 'Carol',
        role: 'free',
        active: false,
        signed_in: false,
        sso_provider: { id: 'example-univ', name: 'Example University' },
      },
      {
        id: 'bob',
        email: 'bob@example.test',
        user_name: 'Bob',
        role: 'free',
        active: false,
        signed_in: false,
        sso_provider: null,
      },
    ];
  });

  it('signs in again at its provider, not through the password form', () => {
    query = new URLSearchParams({ next: '/dashboard/settings' });
    renderLogin();

    fireEvent.click(screen.getByRole('button', { name: /^C/ }));

    expect(navigateToMock).toHaveBeenCalledExactlyOnceWith(
      expect.stringMatching(/\/auth\/sso\/example-univ\/start\?return_to=%2Fdashboard%2Fsettings$/),
    );
    expect(screen.queryByLabelText('Password')).not.toBeInTheDocument();
  });

  it('leaves an account with a password on the form', () => {
    renderLogin();

    fireEvent.click(screen.getByRole('button', { name: /^B/ }));

    expect(screen.getByLabelText('Email or login name')).toHaveValue('bob@example.test');
    expect(navigateToMock).not.toHaveBeenCalled();
  });
});
