// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { SiteFooter } from './SiteFooter';

vi.mock('@/components/ui/BuildInfo', () => ({
  BuildInfo: () => <span>Build info</span>,
}));

describe('SiteFooter', () => {
  afterEach(() => {
    cleanup();
  });

  it('includes a footer link to the terms page', () => {
    render(<SiteFooter />);

    expect(screen.getByRole('link', { name: /terms/i })).toHaveAttribute('href', '/terms');
  });
});
