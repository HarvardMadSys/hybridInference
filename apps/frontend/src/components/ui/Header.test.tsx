// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

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
  beforeEach(() => {
    replace.mockClear();
    authState = {
      isAuthenticated: false,
    };
  });

  it('shows a dashboard link in the header', () => {
    render(<Header />);

    expect(screen.getByRole('link', { name: /dashboard/i })).toHaveAttribute('href', '/dashboard');
  });
});
