// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { buildTimeSiteConfig, type RuntimeSiteConfig } from '@/config/site-config';
import { SiteConfigProvider, useSiteConfig } from './SiteConfigProvider';

function Probe() {
  const { branding, features } = useSiteConfig();
  return (
    <div>
      <span>{branding.appName}</span>
      <span>{branding.contactEmail}</span>
      <span>{features.publicSignup ? 'signup-on' : 'signup-off'}</span>
      <span>{features.rag ? 'rag-on' : 'rag-off'}</span>
      <span>{features.agents ? 'agents-on' : 'agents-off'}</span>
    </div>
  );
}

const initialConfig: RuntimeSiteConfig = {
  ...buildTimeSiteConfig,
  branding: {
    ...buildTimeSiteConfig.branding,
    appName: 'Runtime Example',
    contactEmail: 'runtime@example.test',
  },
  features: { publicSignup: false, rag: false, agents: true },
};

describe('SiteConfigProvider', () => {
  afterEach(() => {
    cleanup();
  });

  it('publishes the server-provided value on the first render without a brand flash', () => {
    const fetchMock = vi.mocked(fetch);

    render(
      <SiteConfigProvider initialConfig={initialConfig}>
        <Probe />
      </SiteConfigProvider>,
    );

    expect(screen.getByText('Runtime Example')).toBeInTheDocument();
    expect(screen.queryByText(buildTimeSiteConfig.branding.appName)).not.toBeInTheDocument();
    expect(screen.getByText('runtime@example.test')).toBeInTheDocument();
    expect(screen.getByText('signup-off')).toBeInTheDocument();
    expect(screen.getByText('rag-off')).toBeInTheDocument();
    expect(screen.getByText('agents-on')).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('keeps the transition build fallback for isolated legacy consumers', () => {
    render(
      <SiteConfigProvider>
        <Probe />
      </SiteConfigProvider>,
    );

    expect(screen.getByText(buildTimeSiteConfig.branding.appName)).toBeInTheDocument();
    expect(screen.getByText('agents-off')).toBeInTheDocument();
  });
});
