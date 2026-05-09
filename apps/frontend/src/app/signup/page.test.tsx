// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('next/script', () => ({
  default: () => null,
}));

import SignupPage from './page';

describe('SignupPage', () => {
  afterEach(() => {
    cleanup();
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
});
