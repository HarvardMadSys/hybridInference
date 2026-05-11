// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { AdminTabNav } from './AdminTabNav';

vi.mock('next/navigation', () => ({
  usePathname: () => '/dashboard/admin/settings',
}));

describe('AdminTabNav', () => {
  it('keeps routing settings out of the top-level admin navigation', () => {
    render(<AdminTabNav />);

    expect(screen.getByRole('link', { name: 'Settings' })).toHaveAttribute(
      'href',
      '/dashboard/admin/settings',
    );
    expect(screen.queryByRole('link', { name: 'Routing' })).not.toBeInTheDocument();
  });
});
