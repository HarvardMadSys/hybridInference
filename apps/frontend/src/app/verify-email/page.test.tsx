// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

// Next's search parameters follow the address, as its router does once the
// page takes the token out.
vi.mock('next/navigation', () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  usePathname: () => window.location.pathname,
  useSearchParams: () => new URLSearchParams(window.location.search),
}));
vi.mock('@/lib/api/auth', () => ({ verifyEmail: vi.fn(), resendVerification: vi.fn() }));
vi.mock('react-hot-toast', () => ({ default: { success: vi.fn(), error: vi.fn() } }));

import { verifyEmail } from '@/lib/api/auth';
import VerifyEmailPage from './page';

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  window.history.replaceState(null, '', '/');
});

describe('the email verification page', () => {
  it('takes the token out of the address and verifies with it once', async () => {
    vi.mocked(verifyEmail).mockResolvedValueOnce({ message: 'Email verified successfully!' });
    window.history.replaceState(null, '', '/verify-email?token=verify-token');
    const { rerender } = render(<VerifyEmailPage />);

    await waitFor(() => expect(window.location.search).toBe(''));
    rerender(<VerifyEmailPage />);

    expect(await screen.findByText('Email verified successfully!')).toBeInTheDocument();
    expect(screen.queryByText('Missing verification token')).toBeNull();
    expect(verifyEmail).toHaveBeenCalledTimes(1);
    expect(verifyEmail).toHaveBeenCalledWith('verify-token');
  });

  it('says so when the link carried no token', async () => {
    window.history.replaceState(null, '', '/verify-email');

    render(<VerifyEmailPage />);

    expect(await screen.findByText('Missing verification token')).toBeInTheDocument();
    expect(verifyEmail).not.toHaveBeenCalled();
  });
});
