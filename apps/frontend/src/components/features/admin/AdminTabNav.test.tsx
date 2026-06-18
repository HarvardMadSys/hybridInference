// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { AdminTabNav } from './AdminTabNav';

vi.mock('next/navigation', () => ({
  usePathname: () => '/dashboard/admin/settings',
}));

describe('AdminTabNav', () => {
  afterEach(() => {
    cleanup();
  });

  it('shows routing as a top-level admin tab', () => {
    render(<AdminTabNav />);

    expect(screen.getByRole('link', { name: 'Providers' })).toHaveAttribute(
      'href',
      '/dashboard/admin/providers',
    );
    expect(screen.getByRole('link', { name: 'Routing' })).toHaveAttribute(
      'href',
      '/dashboard/admin/routing',
    );
    expect(screen.getByRole('link', { name: 'Token Usage' })).toHaveAttribute(
      'href',
      '/dashboard/admin/token-usage',
    );
    expect(screen.getByRole('link', { name: 'Settings' })).toHaveAttribute(
      'href',
      '/dashboard/admin/settings',
    );
  });
});
