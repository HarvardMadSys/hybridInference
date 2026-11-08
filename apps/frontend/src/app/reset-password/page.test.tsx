// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

// Next's search parameters follow the address, as its router does once the
// page takes the token out.
vi.mock('next/navigation', () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  usePathname: () => window.location.pathname,
  useSearchParams: () => new URLSearchParams(window.location.search),
}));
vi.mock('@/lib/api/auth', () => ({ resetPassword: vi.fn() }));

import { resetPassword } from '@/lib/api/auth';
import ResetPasswordPage from './page';

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  window.history.replaceState(null, '', '/');
});

describe('the password reset page', () => {
  // A live reset token in the address reaches the Referer of every request the
  // page makes, and the gateway logs Referers.
  it('takes the token out of the address, and still resets with it', async () => {
    vi.mocked(resetPassword).mockResolvedValueOnce({ message: 'ok' });
    window.history.replaceState(null, '', '/reset-password?token=reset-token');
    const { rerender } = render(<ResetPasswordPage />);

    await waitFor(() => expect(window.location.search).toBe(''));
    rerender(<ResetPasswordPage />);
    expect(screen.queryByText('Invalid or missing reset token')).toBeNull();

    fireEvent.change(screen.getByLabelText('New Password'), { target: { value: 'Valid1234' } });
    fireEvent.change(screen.getByLabelText('Confirm New Password'), {
      target: { value: 'Valid1234' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Reset Password' }));

    await waitFor(() => expect(resetPassword).toHaveBeenCalledWith('reset-token', 'Valid1234'));
  });

  it('says so when the link carried no token', async () => {
    window.history.replaceState(null, '', '/reset-password');

    render(<ResetPasswordPage />);

    expect(await screen.findByText('Invalid or missing reset token')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Reset Password' })).toBeDisabled();
  });
});
