// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// Where the visitor was going before signing up: a consent page, say.
const NEXT =
  '/authorize/key?callback_url=https%3A%2F%2Fagent.example.org%2Fcallback%3Fstate%3Dabc&code_challenge=E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM&code_challenge_method=S256';

let searchParams = new URLSearchParams();
const rememberMock = vi.fn();

vi.mock('next/script', () => ({
  default: () => null,
}));

vi.mock('next/navigation', async (importOriginal) => ({
  ...(await importOriginal<typeof import('next/navigation')>()),
  useSearchParams: () => searchParams,
}));

vi.mock('@/lib/api/auth', () => ({
  signup: vi.fn(),
  signupPhone: vi.fn(),
  sendPhoneCode: vi.fn(),
}));

// The links are built for real; only keeping the destination is observed.
vi.mock('@/lib/utils/authNext', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/utils/authNext')>()),
  rememberAuthNext: (...args: unknown[]) => rememberMock(...args),
}));

import { signup } from '@/lib/api/auth';
import { SiteConfigProvider } from '@/components/providers/SiteConfigProvider';
import { buildTimeSiteConfig } from '@/config/site-config';
import SignupPage from './page';

const CONSENT_CHECKBOXES = [
  /i confirm that i am at least 18 years old/i,
  /i agree to the terms of service/i,
  /i consent to participate in this research/i,
  /i understand and consent to the sharing and possible public release/i,
] as const;

function completeConsentStep(): void {
  const region = screen.getByRole('region', { name: 'Terms of Service' });
  Object.defineProperty(region, 'scrollHeight', { configurable: true, value: 1000 });
  Object.defineProperty(region, 'clientHeight', { configurable: true, value: 200 });
  Object.defineProperty(region, 'scrollTop', { configurable: true, value: 800 });
  fireEvent.scroll(region);
  for (const name of CONSENT_CHECKBOXES) {
    fireEvent.click(screen.getByRole('checkbox', { name }));
  }
  fireEvent.click(screen.getByRole('button', { name: 'Continue' }));
}

function fillAccountForm(): void {
  fireEvent.change(screen.getByLabelText('Email'), { target: { value: 'new@example.test' } });
  fireEvent.change(screen.getByLabelText(/^Nickname/), { target: { value: 'New Person' } });
  fireEvent.change(screen.getByLabelText(/^Password/), { target: { value: 'LocalDemo1' } });
  fireEvent.change(screen.getByLabelText('Confirm Password'), {
    target: { value: 'LocalDemo1' },
  });
}

const withNext = `/login?next=${encodeURIComponent(NEXT)}`;

beforeEach(() => {
  vi.mocked(signup).mockReset();
  rememberMock.mockReset();
  searchParams = new URLSearchParams();
  window.scrollTo = vi.fn() as unknown as typeof window.scrollTo;
});

afterEach(() => {
  cleanup();
});

describe('signing up on the way somewhere', () => {
  it('carries next on the login link of both steps', () => {
    searchParams = new URLSearchParams({ next: NEXT });
    render(<SignupPage />);

    expect(screen.getByRole('link', { name: 'Log In' })).toHaveAttribute('href', withNext);

    completeConsentStep();

    expect(screen.getByRole('link', { name: 'Log In' })).toHaveAttribute('href', withNext);
  });

  it('sends the new account to login with next, and keeps next for the verification page', async () => {
    searchParams = new URLSearchParams({ next: NEXT });
    vi.mocked(signup).mockResolvedValueOnce({
      message: 'Account created successfully. Please check your email to verify your account.',
      email: 'new@example.test',
      user_id: 'user_new',
      requires_approval: false,
    });
    render(<SignupPage />);

    completeConsentStep();
    fillAccountForm();
    fireEvent.click(screen.getByRole('button', { name: 'Sign Up' }));

    expect(await screen.findByRole('link', { name: 'Back to Login' })).toHaveAttribute(
      'href',
      withNext,
    );
    expect(rememberMock).toHaveBeenCalledWith(NEXT);
  });

  it.each([
    ['no next', {}],
    ['an off-site next', { next: '//evil.test/phish' }],
    ['a full URL', { next: 'https://evil.test/phish' }],
  ])('keeps every login link bare with %s', (_name, params) => {
    searchParams = new URLSearchParams(params as Record<string, string>);
    render(<SignupPage />);

    expect(screen.getByRole('link', { name: 'Log In' })).toHaveAttribute('href', '/login');
    completeConsentStep();
    expect(screen.getByRole('link', { name: 'Log In' })).toHaveAttribute('href', '/login');
  });

  it('carries next when registration is closed, too', () => {
    searchParams = new URLSearchParams({ next: NEXT });
    render(
      <SiteConfigProvider
        initialConfig={{
          ...buildTimeSiteConfig,
          features: { ...buildTimeSiteConfig.features, publicSignup: false },
        }}
      >
        <SignupPage />
      </SiteConfigProvider>,
    );

    expect(screen.getByRole('link', { name: 'Sign in' })).toHaveAttribute('href', withNext);
  });
});
