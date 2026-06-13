// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { Header } from './Header';

const replace = vi.fn();
let authState = {
  isAuthenticated: false,
};

vi.mock('next/navigation', () => ({
  useRouter: () => ({ replace }),
}));

vi.mock('@/components/providers', () => ({
  useAuth: () => ({
    state: authState,
    logout: vi.fn(),
  }),
}));

describe('Header', () => {
  afterEach(() => {
    cleanup();
  });

  beforeEach(() => {
    replace.mockClear();
    authState = {
      isAuthenticated: false,
    };
  });

  it('hides the dashboard link from guests', () => {
    render(<Header />);

    expect(screen.queryByRole('link', { name: /dashboard/i })).not.toBeInTheDocument();
  });

  it('shows a status link in the header', () => {
    render(<Header />);

    const statusLink = screen.getByRole('link', { name: 'Status' });

    expect(statusLink).toHaveAttribute('href', 'https://status.staging.freeinference.org/');
    expect(statusLink).toHaveAttribute('target', '_blank');
    expect(statusLink).toHaveAttribute('rel', 'noopener noreferrer');
  });

  it('shows a dashboard link in the header for authenticated users', () => {
    authState = {
      isAuthenticated: true,
    };

    render(<Header />);

    expect(screen.getByRole('link', { name: /dashboard/i })).toHaveAttribute('href', '/dashboard');
  });
});
