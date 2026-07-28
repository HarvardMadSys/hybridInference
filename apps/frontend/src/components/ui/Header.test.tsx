// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { Header } from './Header';

const replace = vi.fn();
let authState = {
  isAuthenticated: false,
};

vi.mock('next/navigation', () => ({
  useRouter: () => ({ replace }),
}));

vi.mock('@/components/providers', () => ({
  useAuth: () => ({
    state: authState,
    logout: vi.fn(),
  }),
}));

const configuredBranding = {
  appName: 'Example Inference',
  orgName: 'Example Org',
  orgUrl: 'https://org.example.test',
  statusUrl: 'https://status.example.test/',
};

let brandingOverride: Record<string, unknown> = configuredBranding;

vi.mock('@/components/providers/SiteConfigProvider', () => ({
  useSiteConfig: () => ({ branding: brandingOverride, features: { rag: true } }),
}));

describe('Header', () => {
  afterEach(() => {
    cleanup();
  });

  beforeEach(() => {
    replace.mockClear();
    brandingOverride = configuredBranding;
    authState = {
      isAuthenticated: false,
    };
  });

  it('hides the dashboard link from guests', () => {
    render(<Header />);

    expect(screen.queryByRole('link', { name: /dashboard/i })).not.toBeInTheDocument();
  });

  it('shows a status link in the header', () => {
    render(<Header />);

    const statusLink = screen.getByRole('link', { name: 'Status' });

    expect(statusLink).toHaveAttribute('href', 'https://status.example.test/');
    expect(statusLink).toHaveAttribute('target', '_blank');
    expect(statusLink).toHaveAttribute('rel', 'noopener noreferrer');
  });

  it('hides the status link when the deployment has no status page', () => {
    brandingOverride = { ...configuredBranding, statusUrl: '' };

    render(<Header />);

    expect(screen.queryByRole('link', { name: 'Status' })).not.toBeInTheDocument();
  });

  it('shows a dashboard link in the header for authenticated users', () => {
    authState = {
      isAuthenticated: true,
    };

    render(<Header />);

    expect(screen.getByRole('link', { name: /dashboard/i })).toHaveAttribute('href', '/dashboard');
  });
});
