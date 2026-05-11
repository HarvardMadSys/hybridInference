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

  it('includes an external status link', () => {
    render(<SiteFooter />);

    const docsLink = screen.getByRole('link', { name: 'Docs' });
    const statusLink = screen.getByRole('link', { name: 'Status' });
    const termsLink = screen.getByRole('link', { name: /terms/i });

    expect(statusLink).toHaveAttribute('href', 'https://status.freeinference.org/');
    expect(statusLink).toHaveAttribute('target', '_blank');
    expect(statusLink).toHaveAttribute('rel', 'noopener noreferrer');
    expect(
      docsLink.compareDocumentPosition(statusLink) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(
      statusLink.compareDocumentPosition(termsLink) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it('includes a footer link to the terms page', () => {
    render(<SiteFooter />);

    expect(screen.getByRole('link', { name: /terms/i })).toHaveAttribute('href', '/terms');
  });
});
