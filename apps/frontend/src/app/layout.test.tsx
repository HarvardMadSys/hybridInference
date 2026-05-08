// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { SiteFooter } from '@/components/ui/SiteFooter';

vi.mock('next/font/google', () => ({
  Crimson_Text: () => ({ variable: 'font-serif-variable' }),
}));

vi.mock('next/script', () => ({
  default: () => null,
}));

vi.mock('@/components/providers', () => ({
  Providers: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

vi.mock('@/components/ui/ErrorBoundary', () => ({
  ErrorBoundary: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

vi.mock('@/components/ui/Header', () => ({
  Header: () => <header>Header</header>,
}));

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
