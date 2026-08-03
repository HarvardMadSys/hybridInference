// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import LoginPage from './page';

let authState = {
  loading: false,
  isAuthenticated: true,
  user: null as { id: string; email: string; role: string } | null,
};

let query = new URLSearchParams();

const replaceMock = vi.fn();

vi.mock('@/components/providers', () => ({
  useAuth: () => ({ state: authState, login: vi.fn() }),
}));

vi.mock('next/navigation', () => ({
  useRouter: () => ({ replace: replaceMock, push: vi.fn() }),
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

  it.each(['https://evil.test/phish', '//evil.test/phish', '/\\evil.test/phish'])(
    'never follows an off-site next (%s)',
    (next) => {
      query = new URLSearchParams({ next });
      render(<LoginPage />);

      expect(replaceMock).toHaveBeenCalledWith('/dashboard');
    },
  );
});
