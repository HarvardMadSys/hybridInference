import { describe, expect, it } from 'vitest';
import { buildTimeSiteConfig, resolveRuntimeSiteConfig } from './site-config';

const runtimeDocument = {
  distribution: {
    id: 'example',
    display_name: 'Example Inference',
    release: '2026.07',
  },
  site: {
    public_base_url: 'https://inference.example.test/',
    support_email: 'support@example.test',
    description: 'Example runtime description.',
  },
  features: {
    routers: ['fixed'],
    public_signup: false,
    rag: false,
  },
};

describe('resolveRuntimeSiteConfig', () => {
  it('overlays public manifest identity and feature flags', () => {
    const resolved = resolveRuntimeSiteConfig(runtimeDocument);

    expect(resolved.branding.appName).toBe('Example Inference');
    expect(resolved.branding.exampleApiBase).toBe('https://inference.example.test');
    expect(resolved.branding.siteHost).toBe('inference.example.test');
    expect(resolved.branding.contactEmail).toBe('support@example.test');
    expect(resolved.branding.appDescription).toBe('Example runtime description.');
    expect(resolved.distribution).toEqual({ id: 'example', release: '2026.07' });
    expect(resolved.features).toEqual({ publicSignup: false, rag: false });
  });

  it('keeps build-time values for the neutral empty document', () => {
    const resolved = resolveRuntimeSiteConfig({
      distribution: { id: 'neutral', display_name: '', release: '' },
      site: { public_base_url: '', support_email: '', description: '' },
      features: { routers: [], public_signup: null, rag: null },
    });

    expect(resolved.branding).toEqual(buildTimeSiteConfig.branding);
    expect(resolved.features).toEqual(buildTimeSiteConfig.features);
  });

  it('rejects malformed documents without changing the fallback', () => {
    expect(resolveRuntimeSiteConfig({ distribution: {} })).toBe(buildTimeSiteConfig);
  });
});
