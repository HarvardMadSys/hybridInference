// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { AnalyticsSectionNav } from './AnalyticsSectionNav';

let pathname = '/dashboard/admin/analytics';

vi.mock('next/navigation', () => ({
  usePathname: () => pathname,
}));

function current() {
  const nav = screen.getByRole('navigation', { name: 'Analytics sections' });
  return within(nav)
    .getAllByRole('link')
    .filter((link) => link.getAttribute('aria-current') === 'page')
    .map((link) => link.textContent);
}

describe('AnalyticsSectionNav', () => {
  afterEach(() => {
    cleanup();
  });

  it('links to the overview, token usage and request origins', () => {
    render(<AnalyticsSectionNav />);

    const nav = screen.getByRole('navigation', { name: 'Analytics sections' });
    expect(
      within(nav)
        .getAllByRole('link')
        .map((link) => [link.textContent, link.getAttribute('href')]),
    ).toEqual([
      ['Overview', '/dashboard/admin/analytics'],
      ['Token usage', '/dashboard/admin/analytics/token-usage'],
      ['Request origins', '/dashboard/admin/analytics/geo'],
    ]);
  });

  it('marks only the page being shown', () => {
    pathname = '/dashboard/admin/analytics';
    render(<AnalyticsSectionNav />);
    expect(current()).toEqual(['Overview']);
    cleanup();

    pathname = '/dashboard/admin/analytics/token-usage';
    render(<AnalyticsSectionNav />);
    expect(current()).toEqual(['Token usage']);
    cleanup();

    pathname = '/dashboard/admin/analytics/geo';
    render(<AnalyticsSectionNav />);
    expect(current()).toEqual(['Request origins']);
  });
});
