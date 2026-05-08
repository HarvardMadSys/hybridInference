// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import HomePage from './page';

const replace = vi.fn();
let authState = {
  loading: false,
  isAuthenticated: false,
  user: null as { id: string; email: string; role: string; user_name?: string | null } | null,
};

vi.mock('next/navigation', () => ({
  useRouter: () => ({ replace }),
}));

vi.mock('@/components/providers', () => ({
  useAuth: () => ({
    state: authState,
  }),
}));

vi.mock('@/components/providers/AuthProvider', () => ({
  hasRole: (userRole: string | undefined, required: string) => userRole === required,
}));

vi.mock('@/components/landing', () => ({
  CodeExample: () => <section aria-label="code example" />,
  Features: () => <section aria-label="features" />,
  Hero: () => <section aria-label="hero" />,
  HowItWorks: () => <section aria-label="how it works" />,
}));

vi.mock('@/components/features/dashboard/DashboardView', () => ({
  DashboardView: () => <section aria-label="dashboard view">Dashboard</section>,
}));

describe('HomePage', () => {
  afterEach(() => {
    cleanup();
  });

  beforeEach(() => {
    replace.mockClear();
    authState = {
      loading: false,
      isAuthenticated: false,
      user: null,
    };
  });

  it('shows the no-guarantee notice before the prompt logging notice', () => {
    const { container } = render(<HomePage />);

    const warrantyNotice = screen.getByText(/service is provided without guarantee/i);
    const loggingNotice = screen.getByText(/all prompts and responses are logged/i);

    expect(warrantyNotice).toBeInTheDocument();
    expect(loggingNotice).toBeInTheDocument();
    expect(warrantyNotice.compareDocumentPosition(loggingNotice)).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING,
    );
    expect(container).toHaveTextContent(/provided without guarantee/i);
  });

  it('shows the public homepage for authenticated users without redirecting', () => {
    authState = {
      loading: false,
      isAuthenticated: true,
      user: {
        id: 'user-1',
        email: 'user@example.com',
        role: 'free',
        user_name: 'Test User',
      },
    };

    render(<HomePage />);

    expect(screen.getByLabelText(/hero/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/features/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/how it works/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/code example/i)).toBeInTheDocument();
    expect(screen.queryByLabelText(/dashboard view/i)).not.toBeInTheDocument();
    expect(screen.getByText(/service is provided without guarantee/i)).toBeInTheDocument();
    expect(screen.getByText(/all prompts and responses are logged/i)).toBeInTheDocument();
    expect(replace).not.toHaveBeenCalled();
  });
});
