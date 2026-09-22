// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('next/script', () => ({
  default: () => null,
}));

vi.mock('@/lib/api/auth', () => ({
  signup: vi.fn(),
}));

import { signup, type SignupResponse } from '@/lib/api/auth';
import { SiteConfigProvider } from '@/components/providers/SiteConfigProvider';
import { buildTimeSiteConfig } from '@/config/site-config';
import SignupPage from './page';

const mockedSignup = vi.mocked(signup);

const CONSENT_CHECKBOXES = [
  /i confirm that i am at least 18 years old/i,
  /i agree to the terms of service/i,
  /i consent to participate in this research/i,
  /i understand and consent to the sharing and possible public release/i,
] as const;

function scrollTermsTo(fraction: number): void {
  const region = screen.getByRole('region', { name: 'Terms of Service' });
  Object.defineProperty(region, 'scrollHeight', { configurable: true, value: 1000 });
  Object.defineProperty(region, 'clientHeight', { configurable: true, value: 200 });
  Object.defineProperty(region, 'scrollTop', { configurable: true, value: 800 * fraction });
  fireEvent.scroll(region);
}

function checkAllConsents(): void {
  scrollTermsTo(1);
  for (const name of CONSENT_CHECKBOXES) {
    fireEvent.click(screen.getByRole('checkbox', { name }));
  }
}

function completeConsentStep(): void {
  checkAllConsents();
  fireEvent.click(screen.getByRole('button', { name: 'Continue' }));
}

function fillAccountForm(): void {
  fireEvent.change(screen.getByLabelText('Email'), {
    target: { value: 'admin@local.dev' },
  });
  fireEvent.change(screen.getByLabelText(/^Username/), {
    target: { value: 'Local Admin' },
  });
  fireEvent.change(screen.getByLabelText(/^Password/), {
    target: { value: 'LocalDemo1' },
  });
  fireEvent.change(screen.getByLabelText('Confirm Password'), {
    target: { value: 'LocalDemo1' },
  });
}

async function submitSignup(response: SignupResponse): Promise<void> {
  mockedSignup.mockResolvedValueOnce(response);
  render(<SignupPage />);

  completeConsentStep();
  fillAccountForm();
  fireEvent.click(screen.getByRole('button', { name: 'Sign Up' }));

  expect(await screen.findByText(response.message)).toBeInTheDocument();
}

describe('SignupPage', () => {
  it('keeps the login link after consent in classic mode', () => {
    render(<SignupPage />);
    completeConsentStep();
    expect(screen.getByRole('link', { name: 'Log In' })).toHaveAttribute('href', '/login');
  });

  afterEach(() => {
    cleanup();
    mockedSignup.mockReset();
  });

  describe('consent step', () => {
    it('shows the four consent checkboxes before any account fields', () => {
      render(<SignupPage />);

      for (const name of CONSENT_CHECKBOXES) {
        expect(screen.getByRole('checkbox', { name })).not.toBeChecked();
      }
      expect(screen.queryByLabelText('Email')).not.toBeInTheDocument();
      expect(screen.queryByRole('button', { name: 'Sign Up' })).not.toBeInTheDocument();
    });

    it('links to the full /terms page in a new tab', () => {
      render(<SignupPage />);

      const link = screen.getByRole('link', { name: /terms of service/i });
      expect(link).toHaveAttribute('href', '/terms');
      expect(link).toHaveAttribute('target', '_blank');
    });

    it('embeds the Terms of Service text in a scrollable region', () => {
      render(<SignupPage />);

      const region = screen.getByRole('region', { name: 'Terms of Service' });
      expect(region).toBeInTheDocument();
      expect(screen.getByRole('heading', { name: /logging and data use/i })).toBeInTheDocument();
      expect(screen.getByText(/you must be at least 18 years old/i)).toBeInTheDocument();
    });

    it('disables the Terms checkbox until the terms are scrolled to the bottom', () => {
      render(<SignupPage />);
      const checkbox = screen.getByRole('checkbox', { name: /i agree to the terms of service/i });

      expect(checkbox).toBeDisabled();
      expect(screen.getByText(/scroll to the end of the terms/i)).toBeInTheDocument();

      scrollTermsTo(0.5);
      expect(checkbox).toBeDisabled();

      scrollTermsTo(1);
      expect(checkbox).toBeEnabled();
      expect(screen.queryByText(/scroll to the end of the terms/i)).not.toBeInTheDocument();
    });

    it('keeps the Terms checkbox enabled after scrolling back up', () => {
      render(<SignupPage />);
      const checkbox = screen.getByRole('checkbox', { name: /i agree to the terms of service/i });

      scrollTermsTo(1);
      scrollTermsTo(0);

      expect(checkbox).toBeEnabled();
    });

    it('states that the service is for adults only', () => {
      render(<SignupPage />);

      expect(screen.getByText(/only to adults age 18 or older/i)).toBeInTheDocument();
    });

    it('describes what is logged for research and warns against sensitive data', () => {
      render(<SignupPage />);

      expect(screen.getByText(/prompts sent through/i)).toBeInTheDocument();
      expect(screen.getByText(/model responses/i)).toBeInTheDocument();
      expect(screen.getByText(/tool calls and tool outputs/i)).toBeInTheDocument();
      expect(
        screen.getByText(/do not submit passwords, credentials, confidential information/i),
      ).toBeInTheDocument();
    });

    it('explains public release, sanitization limits, and irreversibility', () => {
      render(<SignupPage />);

      expect(screen.getByText(/released as a research dataset/i)).toBeInTheDocument();
      expect(
        screen.getByText(/sanitization cannot guarantee removal of every sensitive/i),
      ).toBeInTheDocument();
      expect(
        screen.getByText(/may no longer be possible to withdraw or delete those copies/i),
      ).toBeInTheDocument();
    });

    it('keeps Continue disabled until every consent is checked', () => {
      render(<SignupPage />);
      const continueButton = screen.getByRole('button', { name: 'Continue' });

      expect(continueButton).toBeDisabled();

      // All but the last one.
      scrollTermsTo(1);
      for (const name of CONSENT_CHECKBOXES.slice(0, -1)) {
        fireEvent.click(screen.getByRole('checkbox', { name }));
      }
      expect(continueButton).toBeDisabled();

      fireEvent.click(
        screen.getByRole('checkbox', { name: CONSENT_CHECKBOXES[CONSENT_CHECKBOXES.length - 1] }),
      );
      expect(continueButton).toBeEnabled();
    });

    it('re-disables Continue when a consent is unchecked', () => {
      render(<SignupPage />);
      checkAllConsents();
      const continueButton = screen.getByRole('button', { name: 'Continue' });
      expect(continueButton).toBeEnabled();

      fireEvent.click(screen.getByRole('checkbox', { name: CONSENT_CHECKBOXES[0] }));

      expect(continueButton).toBeDisabled();
    });

    it('reveals the account form after Continue', () => {
      render(<SignupPage />);

      completeConsentStep();

      expect(screen.getByLabelText('Email')).toBeInTheDocument();
      expect(screen.getByRole('button', { name: 'Sign Up' })).toBeInTheDocument();
      expect(
        screen.queryByRole('checkbox', { name: CONSENT_CHECKBOXES[0] }),
      ).not.toBeInTheDocument();
    });
  });

  describe('account step', () => {
    it('renders Turnstile from the runtime branding value', () => {
      const runtimeConfig = {
        ...buildTimeSiteConfig,
        branding: {
          ...buildTimeSiteConfig.branding,
          turnstileSiteKey: 'runtime-turnstile-key',
        },
      };

      const { container } = render(
        <SiteConfigProvider initialConfig={runtimeConfig}>
          <SignupPage />
        </SiteConfigProvider>,
      );
      completeConsentStep();

      expect(container.querySelector('.cf-turnstile')).toHaveAttribute(
        'data-sitekey',
        'runtime-turnstile-key',
      );
    });

    it('sends accepted_tos: true once the consent step has been completed', async () => {
      await submitSignup({
        message: 'Account created successfully. You can now log in.',
        email: 'admin@local.dev',
        user_id: '01LOCALADMIN',
        requires_approval: false,
      });

      expect(mockedSignup).toHaveBeenCalledWith(
        expect.objectContaining({ email: 'admin@local.dev', accepted_tos: true }),
      );
    });

    it.each([
      [
        'active',
        'Account created successfully. You can now log in.',
        false,
        'Registration Successful!',
      ],
      [
        'pending approval',
        'Account created successfully. Your registration is pending admin approval.',
        true,
        'Registration Submitted',
      ],
      [
        'email verification',
        'Account created successfully. Please check your email to verify your account.',
        false,
        'Registration Successful!',
      ],
    ] as const)(
      'renders the backend message for a %s signup',
      async (_state, message, requiresApproval, heading) => {
        await submitSignup({
          message,
          email: 'admin@local.dev',
          user_id: '01LOCALADMIN',
          requires_approval: requiresApproval,
        });

        expect(screen.getByRole('heading', { name: heading })).toBeInTheDocument();
        expect(mockedSignup).toHaveBeenCalledTimes(1);
      },
    );
  });
});
