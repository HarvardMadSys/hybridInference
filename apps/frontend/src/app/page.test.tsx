// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import HomePage from './page';

let authState = {
  loading: false,
  isAuthenticated: false,
  user: null as { id: string; email: string; role: string; user_name?: string | null } | null,
};

const NOTICE = 'Requests are logged by this deployment for research purposes.';

let dataPolicyNotice = NOTICE;

vi.mock('@/config/branding', () => ({
  branding: {
    get dataPolicyNotice() {
      return dataPolicyNotice;
    },
  },
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
  UseCases: () => <section aria-label="use cases" />,
  Sponsors: () => <section aria-label="sponsors" />,
  Updates: () => <section aria-label="updates" />,
}));

vi.mock('@/components/ui/UpdatesBanner', () => ({
  UpdatesBanner: () => <section aria-label="updates banner" />,
}));

vi.mock('@/components/features/dashboard/DashboardView', () => ({
  DashboardView: () => <section aria-label="dashboard view">Dashboard</section>,
}));

describe('HomePage', () => {
  afterEach(() => {
    cleanup();
  });

  beforeEach(() => {
    authState = {
      loading: false,
      isAuthenticated: false,
      user: null,
    };
    dataPolicyNotice = NOTICE;
  });

  it('shows the no-guarantee notice before the data-policy notice', () => {
    const { container } = render(<HomePage />);

    const warrantyNotice = screen.getByText(/service is provided without guarantee/i);
    const loggingNotice = screen.getByText(new RegExp(NOTICE, 'i'));

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

    [/hero/i, /features/i, /use cases/i, /how it works/i, /code example/i, /sponsors/i].forEach(
      (pattern) => {
        expect(screen.getByLabelText(pattern)).toBeInTheDocument();
      },
    );
    expect(screen.queryByLabelText(/dashboard view/i)).not.toBeInTheDocument();
    expect(screen.getByText(/service is provided without guarantee/i)).toBeInTheDocument();
    expect(screen.getByText(new RegExp(NOTICE, 'i'))).toBeInTheDocument();
  });

  it('states no data policy when the deployment has not declared one', () => {
    // A deployment that configures nothing must not inherit someone else's
    // claim about what happens to its users' prompts.
    dataPolicyNotice = '';

    render(<HomePage />);

    expect(screen.getByText(/service is provided without guarantee/i)).toBeInTheDocument();
    expect(screen.queryByText(new RegExp(NOTICE, 'i'))).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: /terms of service/i })).toBeInTheDocument();
  });
});
