// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

/**
 * The five account pages, rendered for real and submitted with bad input.
 *
 * `AuthForm.test.tsx` shows what one field does in isolation. This file checks
 * the property a visitor actually depends on, on every page that has a form:
 * after a failed submit, each control is announced with its hint and its error,
 * and flagged invalid — whichever layout arranges the field. A page that passed
 * its control in some shape the field could not reach would fail here, not in a
 * unit test of the field.
 */

const navigation = vi.hoisted(() => ({ query: new URLSearchParams() }));

vi.mock('next/navigation', () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  usePathname: () => '/',
  useSearchParams: () => navigation.query,
}));

vi.mock('@/components/providers', () => ({
  useAuth: () => ({
    state: { loading: false, isAuthenticated: false, user: null },
    login: vi.fn(),
  }),
}));

vi.mock('@/lib/api/auth', () => ({
  signup: vi.fn(),
  verifyEmail: vi.fn(),
  forgotPassword: vi.fn(),
  resetPassword: vi.fn(),
  resendVerification: vi.fn(),
}));

vi.mock('react-hot-toast', () => ({
  default: { success: vi.fn(), error: vi.fn() },
}));

vi.mock('next/script', () => ({
  default: () => null,
}));

import { verifyEmail } from '@/lib/api/auth';
import { AUTH_DATA } from '@/components/auth/AuthForm';
import { AuthAppearanceProvider, NEUTRAL_AUTH_APPEARANCE } from '@/site-ui/appearance';
import type { AuthFieldLayoutProps } from '@/site-ui/contract';
import ForgotPasswordPage from '@/app/forgot-password/page';
import LoginPage from '@/app/login/page';
import ResetPasswordPage from '@/app/reset-password/page';
import SignupPage from '@/app/signup/page';
import VerifyEmailPage from '@/app/verify-email/page';

/**
 * A module's arrangement: messages first, the control two wrappers deep and the
 * label last. Nothing about where the nodes land may change what the control is
 * described by.
 */
function ModuleFieldLayout({
  htmlFor,
  label,
  control,
  hint,
  error,
  action,
  className,
}: AuthFieldLayoutProps) {
  return (
    <section className={className} data-auth="field">
      {error}
      {hint}
      <div>
        <div>{control}</div>
      </div>
      <label data-auth="label" htmlFor={htmlFor}>
        {label}
      </label>
      {action}
    </section>
  );
}

const LAYOUTS = [
  ['the default field layout', undefined],
  ['a module field layout', ModuleFieldLayout],
] as const;

/** What one control should announce after the failed submit. */
interface Expectation {
  label: string;
  description: string;
  invalid: boolean;
}

interface Scenario {
  page: string;
  /** Render the page, reach its form, enter bad input and submit it. */
  submit: (wrap: (page: React.ReactElement) => React.ReactElement) => Promise<void>;
  controls: Expectation[];
}

const PASSWORD_HINT = 'At least 8 characters with uppercase, lowercase, and numbers';

function type(label: string, value: string): void {
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
}

/** Scroll the embedded terms, tick every confirmation and continue. */
function completeConsentStep(): void {
  const region = screen.getByRole('region', { name: 'Terms of Service' });
  Object.defineProperty(region, 'scrollHeight', { configurable: true, value: 1000 });
  Object.defineProperty(region, 'clientHeight', { configurable: true, value: 200 });
  Object.defineProperty(region, 'scrollTop', { configurable: true, value: 800 });
  fireEvent.scroll(region);
  for (const checkbox of screen.getAllByRole('checkbox')) fireEvent.click(checkbox);
  fireEvent.click(screen.getByRole('button', { name: 'Continue' }));
}

const SCENARIOS: Scenario[] = [
  {
    page: '/login',
    async submit(wrap) {
      render(wrap(<LoginPage />));
      fireEvent.click(screen.getByRole('button', { name: 'Log In' }));
    },
    controls: [
      { label: 'Email', description: 'Please enter a valid email address', invalid: true },
      { label: 'Password', description: 'Please enter your password', invalid: true },
    ],
  },
  {
    page: '/signup',
    async submit(wrap) {
      render(wrap(<SignupPage />));
      completeConsentStep();
      type('Username', 'a');
      type('Password', 'short');
      type('Confirm Password', 'short');
      // Past each textarea's `maxLength`, which only limits what a person can
      // type: the schema is what rejects it.
      type('How did you find us?', 'x'.repeat(501));
      type('Use case', 'x'.repeat(2001));
      fireEvent.click(screen.getByRole('button', { name: 'Sign Up' }));
    },
    controls: [
      { label: 'Email', description: 'Please enter a valid email address', invalid: true },
      {
        label: 'Username',
        description:
          'This name is shown in your account and admin review. Username must be at least 2 characters',
        invalid: true,
      },
      {
        label: 'Password',
        description: `${PASSWORD_HINT}. Password must be at least 8 characters`,
        invalid: true,
      },
      { label: 'Confirm Password', description: '', invalid: false },
      {
        label: 'How did you find us?',
        description:
          'Optional, but helpful for improving outreach. Response cannot exceed 500 characters',
        invalid: true,
      },
      {
        label: 'Use case',
        description: 'Helps admins review signups faster. Use case cannot exceed 2000 characters',
        invalid: true,
      },
    ],
  },
  {
    page: '/forgot-password',
    async submit(wrap) {
      render(wrap(<ForgotPasswordPage />));
      fireEvent.click(screen.getByRole('button', { name: 'Send Reset Link' }));
    },
    controls: [
      { label: 'Email', description: 'Please enter a valid email address', invalid: true },
    ],
  },
  {
    page: '/reset-password',
    async submit(wrap) {
      navigation.query = new URLSearchParams({ token: 'reset-token' });
      render(wrap(<ResetPasswordPage />));
      type('New Password', 'Valid1234');
      type('Confirm New Password', 'Other1234');
      fireEvent.click(screen.getByRole('button', { name: 'Reset Password' }));
    },
    controls: [
      // Valid: described by its hint, and not flagged.
      { label: 'New Password', description: PASSWORD_HINT, invalid: false },
      { label: 'Confirm New Password', description: 'Passwords do not match', invalid: true },
    ],
  },
  {
    page: '/verify-email',
    async submit(wrap) {
      // An expired link: the page offers to send a new one.
      vi.mocked(verifyEmail).mockRejectedValueOnce(new Error('That link has expired.'));
      navigation.query = new URLSearchParams({ token: 'expired' });
      render(wrap(<VerifyEmailPage />));
      await screen.findByLabelText('Email');
      fireEvent.click(screen.getByRole('button', { name: 'Resend verification email' }));
    },
    // The resend field has no hint and no validation message of its own, so the
    // property here is the absence of a dangling reference.
    controls: [{ label: 'Email', description: '', invalid: false }],
  },
];

beforeEach(() => {
  navigation.query = new URLSearchParams();
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

/** Assert what each control announces; retried until a submit's messages land. */
function expectControls(controls: Expectation[]): void {
  for (const { label, description, invalid } of controls) {
    const control = screen.getByLabelText(label);
    if (description) {
      expect(control, label).toHaveAccessibleDescription(description);
    } else {
      expect(control, label).not.toHaveAccessibleDescription();
      expect(control, label).not.toHaveAttribute('aria-describedby');
    }
    if (invalid) {
      expect(control, label).toHaveAttribute('aria-invalid', 'true');
    } else {
      expect(control, label).not.toHaveAttribute('aria-invalid');
    }
  }
}

describe.each(LAYOUTS)('every account control, under %s', (_name, fieldLayout) => {
  const wrap = (page: React.ReactElement) =>
    fieldLayout ? (
      <AuthAppearanceProvider value={{ ...NEUTRAL_AUTH_APPEARANCE, fieldLayout }}>
        {page}
      </AuthAppearanceProvider>
    ) : (
      page
    );

  it.each(SCENARIOS)(
    'on $page is described by its hint and error after a failed submit',
    async ({ submit, controls }) => {
      await submit(wrap);

      await waitFor(() => expectControls(controls));
    },
  );
});

/**
 * The `data-auth` hooks the default pages render, against the ones `AUTH_DATA`
 * promises — in both directions. A promised hook that no page renders is a
 * selector a module's stylesheet would be written against nothing; a rendered
 * hook nobody promised is one a module could come to depend on without the
 * contract knowing it exists.
 */
describe('the data-auth hooks', () => {
  /** Every state of the default pages that renders a hook the others do not. */
  const STATES: Array<[string, () => Promise<void>]> = [
    ...SCENARIOS.map(({ page, submit, controls }): [string, () => Promise<void>] => [
      `${page}, after a failed submit`,
      async () => {
        await submit((element) => element);
        await waitFor(() => expectControls(controls));
      },
    ]),
    [
      '/signup, at the confirmations',
      async () => {
        render(<SignupPage />);
      },
    ],
    [
      '/verify-email, while the link is checked',
      async () => {
        vi.mocked(verifyEmail).mockReturnValueOnce(new Promise(() => undefined));
        navigation.query = new URLSearchParams({ token: 'pending' });
        render(<VerifyEmailPage />);
      },
    ],
  ];

  it('are exactly the ones AUTH_DATA promises', async () => {
    const rendered = new Set<string>();
    for (const [, enter] of STATES) {
      navigation.query = new URLSearchParams();
      await enter();
      for (const element of document.querySelectorAll('[data-auth]')) {
        rendered.add(element.getAttribute('data-auth') ?? '');
      }
      cleanup();
    }

    expect([...rendered].sort()).toEqual(Object.values(AUTH_DATA).sort());
  });

  it('marks a field\u2019s action once, around the action itself', () => {
    // The field wraps whatever action it is given; a page that also marked its
    // own link nested one hook inside another.
    render(<LoginPage />);

    const actions = document.querySelectorAll('[data-auth="field-action"]');
    expect(actions).toHaveLength(1);
    expect(actions[0]).toContainElement(screen.getByRole('link', { name: 'Forgot password?' }));
  });
});
