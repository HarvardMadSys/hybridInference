// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { SiteConfigProvider, useSiteConfig } from './SiteConfigProvider';

function Probe() {
  const { branding, features } = useSiteConfig();
  return (
    <div>
      <span>{branding.appName}</span>
      <span>{branding.contactEmail}</span>
      <span>{features.publicSignup ? 'signup-on' : 'signup-off'}</span>
      <span>{features.rag ? 'rag-on' : 'rag-off'}</span>
    </div>
  );
}

describe('SiteConfigProvider', () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    document.querySelector('meta[name="description"]')?.remove();
    document.title = '';
  });

  it('loads and publishes the runtime document from the configured API base', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        distribution: {
          id: 'example',
          display_name: 'Runtime Example',
          release: '1.0',
        },
        site: {
          public_base_url: 'https://runtime.example.test',
          support_email: 'runtime@example.test',
          description: 'Runtime site description.',
        },
        features: {
          routers: ['fixed'],
          public_signup: false,
          rag: false,
        },
      }),
    });
    vi.stubGlobal('fetch', fetchMock);
    document.title = 'FreeInference';
    const description = document.createElement('meta');
    description.name = 'description';
    description.content = 'Build-time description.';
    document.head.append(description);

    render(
      <SiteConfigProvider>
        <Probe />
      </SiteConfigProvider>,
    );

    expect(await screen.findByText('Runtime Example')).toBeInTheDocument();
    expect(screen.getByText('runtime@example.test')).toBeInTheDocument();
    expect(screen.getByText('signup-off')).toBeInTheDocument();
    expect(screen.getByText('rag-off')).toBeInTheDocument();
    expect(document.title).toBe('Runtime Example');
    expect(description.content).toBe('Runtime site description.');
    expect(fetchMock).toHaveBeenCalledWith(
      'https://freeinference.org/site-config',
      expect.objectContaining({ cache: 'no-store' }),
    );
  });
});
