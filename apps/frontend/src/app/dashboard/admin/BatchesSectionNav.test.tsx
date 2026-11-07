// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { BatchesSectionNav } from './BatchesSectionNav';

let pathname = '/dashboard/admin/batches';

vi.mock('next/navigation', () => ({
  usePathname: () => pathname,
}));

function current() {
  const nav = screen.getByRole('navigation', { name: 'Batches sections' });
  return within(nav)
    .getAllByRole('link')
    .filter((link) => link.getAttribute('aria-current') === 'page')
    .map((link) => link.textContent);
}

describe('BatchesSectionNav', () => {
  afterEach(() => {
    cleanup();
  });

  it('links to the batch list, the batch requests and the model queue', () => {
    render(<BatchesSectionNav />);

    const nav = screen.getByRole('navigation', { name: 'Batches sections' });
    expect(
      within(nav)
        .getAllByRole('link')
        .map((link) => [link.textContent, link.getAttribute('href')]),
    ).toEqual([
      ['Batches', '/dashboard/admin/batches'],
      ['Requests', '/dashboard/admin/batches/requests'],
      ['Model queue', '/dashboard/admin/batches/queue'],
    ]);
  });

  it('marks only the page being shown', () => {
    for (const [path, shown] of [
      ['/dashboard/admin/batches', 'Batches'],
      ['/dashboard/admin/batches/requests', 'Requests'],
      // A batch's address under Requests is still Requests.
      ['/dashboard/admin/batches/requests/anything', 'Requests'],
      ['/dashboard/admin/batches/queue', 'Model queue'],
    ]) {
      pathname = path;
      render(<BatchesSectionNav />);
      expect(current()).toEqual([shown]);
      cleanup();
    }
  });
});
