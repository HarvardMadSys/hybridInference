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
import SignupPage from './page';

const mockedSignup = vi.mocked(signup);

async function submitSignup(response: SignupResponse): Promise<void> {
  mockedSignup.mockResolvedValueOnce(response);
  render(<SignupPage />);

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
  fireEvent.click(screen.getByRole('checkbox', { name: /i agree to the terms of service/i }));
  fireEvent.click(screen.getByRole('button', { name: 'Sign Up' }));

  expect(await screen.findByText(response.message)).toBeInTheDocument();
}

describe('SignupPage', () => {
  afterEach(() => {
    cleanup();
    mockedSignup.mockReset();
  });

  it('renders a required terms agreement checkbox linked to /terms', () => {
    render(<SignupPage />);

    expect(
      screen.getByRole('checkbox', { name: /i agree to the terms of service/i }),
    ).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /terms of service/i })).toHaveAttribute(
      'href',
      '/terms',
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
