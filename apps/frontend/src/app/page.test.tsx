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

describe('HomePage', () => {
  it('shows the no-guarantee notice before the prompt logging notice', () => {
    const { container } = render(<HomePage />);

    const warrantyNotice = screen.getByText(/service is provided without guarantee/i);
    const loggingNotice = screen.getByText(/all prompts and responses are logged/i);

    expect(warrantyNotice).toBeInTheDocument();
    expect(loggingNotice).toBeInTheDocument();
    expect(
      warrantyNotice.compareDocumentPosition(loggingNotice) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(container).toHaveTextContent(/provided without guarantee/i);
  });
});
