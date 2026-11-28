// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { RoutingSectionNav } from './RoutingSectionNav';

let pathname = '/dashboard/admin/routing';

vi.mock('next/navigation', () => ({
  usePathname: () => pathname,
}));

function current() {
  const nav = screen.getByRole('navigation', { name: 'Routing sections' });
  return within(nav)
    .getAllByRole('link')
    .filter((link) => link.getAttribute('aria-current') === 'page')
    .map((link) => link.textContent);
}

describe('RoutingSectionNav', () => {
  afterEach(() => {
    cleanup();
  });

  it('links to the routes and to the model catalog', () => {
    render(<RoutingSectionNav />);

    const nav = screen.getByRole('navigation', { name: 'Routing sections' });
    expect(
      within(nav)
        .getAllByRole('link')
        .map((link) => [link.textContent, link.getAttribute('href')]),
    ).toEqual([
      ['Routes', '/dashboard/admin/routing'],
      ['Models', '/dashboard/admin/routing/models'],
    ]);
  });

  it('marks only the page being shown', () => {
    for (const [path, shown] of [
      ['/dashboard/admin/routing', 'Routes'],
      ['/dashboard/admin/routing/models', 'Models'],
      // An address below Models is still Models.
      ['/dashboard/admin/routing/models/anything', 'Models'],
    ]) {
      pathname = path;
      render(<RoutingSectionNav />);
      expect(current()).toEqual([shown]);
      cleanup();
    }
  });
});
