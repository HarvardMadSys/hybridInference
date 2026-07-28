// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { SiteFooter } from './SiteFooter';

vi.mock('@/components/ui/BuildInfo', () => ({
  BuildInfo: () => <span>Build info</span>,
}));

// The footer renders whatever the active distribution supplies; these tests
// drive both shapes explicitly rather than depending on the build-time
// defaults, which are neutral (and therefore mostly empty) upstream.
const configuredBranding = {
  appName: 'Example Inference',
  orgName: 'Example Org',
  orgUrl: 'https://org.example.test',
  docsUrl: 'https://docs.example.test/',
  statusUrl: 'https://status.example.test/',
  githubUrl: 'https://github.com/example/repo',
  team: [{ name: 'A', affiliations: [] }],
};

let brandingOverride: Record<string, unknown> = configuredBranding;

vi.mock('@/components/providers/SiteConfigProvider', () => ({
  useBranding: () => brandingOverride,
}));

describe('SiteFooter', () => {
  afterEach(() => {
    cleanup();
    brandingOverride = configuredBranding;
  });

  it('includes an external status link', () => {
    render(<SiteFooter />);

    const docsLink = screen.getByRole('link', { name: 'Docs' });
    const statusLink = screen.getByRole('link', { name: 'Status' });
    const termsLink = screen.getByRole('link', { name: /terms/i });

    expect(statusLink).toHaveAttribute('href', 'https://status.example.test/');
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

  it('includes a footer link to the team page', () => {
    render(<SiteFooter />);

    expect(screen.getByRole('link', { name: /team/i })).toHaveAttribute('href', '/team');
  });

  it('omits links a deployment has not configured, without stranding separators', () => {
    brandingOverride = {
      ...configuredBranding,
      orgName: '',
      orgUrl: '',
      docsUrl: '',
      statusUrl: '',
      team: [],
    };

    const { container } = render(<SiteFooter />);

    expect(screen.queryByRole('link', { name: 'Docs' })).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Status' })).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /team/i })).not.toBeInTheDocument();
    // Copyright · Terms · GitHub · build info => exactly three separators.
    expect(container.querySelectorAll('[aria-hidden="true"]')).toHaveLength(3);
    expect(screen.getByRole('link', { name: /terms/i })).toBeInTheDocument();
  });
});
