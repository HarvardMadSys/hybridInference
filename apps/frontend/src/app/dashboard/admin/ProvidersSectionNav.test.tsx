// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ProvidersSectionNav } from './ProvidersSectionNav';

let pathname = '/dashboard/admin/providers';

vi.mock('next/navigation', () => ({
  usePathname: () => pathname,
}));

const revealMock = vi.hoisted(() => vi.fn());
vi.mock('@/components/ui/revealHorizontally', () => ({ revealHorizontally: revealMock }));

function current() {
  const nav = screen.getByRole('navigation', { name: 'Providers sections' });
  return within(nav)
    .getAllByRole('link')
    .filter((link) => link.getAttribute('aria-current') === 'page')
    .map((link) => link.textContent);
}

describe('ProvidersSectionNav', () => {
  afterEach(() => {
    cleanup();
    revealMock.mockClear();
  });

  it('links to Overview, which holds the registry, keys and quotas, and to Performance', () => {
    render(<ProvidersSectionNav />);

    const nav = screen.getByRole('navigation', { name: 'Providers sections' });
    expect(
      within(nav)
        .getAllByRole('link')
        .map((link) => [link.textContent, link.getAttribute('href')]),
    ).toEqual([
      ['Overview', '/dashboard/admin/providers'],
      ['Performance', '/dashboard/admin/providers/performance'],
    ]);
  });

  it('marks only the page being shown', () => {
    for (const [path, shown] of [
      ['/dashboard/admin/providers', 'Overview'],
      ['/dashboard/admin/providers/performance', 'Performance'],
      // An address below a section is still that section.
      ['/dashboard/admin/providers/performance/anything', 'Performance'],
    ]) {
      pathname = path;
      render(<ProvidersSectionNav />);
      expect(current()).toEqual([shown]);
      cleanup();
    }
  });

  it('brings the section shown into view on a narrow screen', () => {
    pathname = '/dashboard/admin/providers/performance';
    render(<ProvidersSectionNav />);

    const nav = screen.getByRole('navigation', { name: 'Providers sections' });
    expect(revealMock).toHaveBeenCalledWith(
      nav,
      within(nav).getByRole('link', { name: 'Performance' }),
    );
  });
});
