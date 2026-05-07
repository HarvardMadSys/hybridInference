// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import HomePage from './page';

vi.mock('next/navigation', () => ({
  useRouter: () => ({ replace: vi.fn() }),
}));

vi.mock('@/components/providers', () => ({
  useAuth: () => ({
    state: {
      loading: false,
      isAuthenticated: false,
    },
  }),
}));

vi.mock('@/components/landing', () => ({
  CodeExample: () => <section aria-label="code example" />,
  Features: () => <section aria-label="features" />,
  Hero: () => <section aria-label="hero" />,
  HowItWorks: () => <section aria-label="how it works" />,
}));

describe('HomePage', () => {
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
});
