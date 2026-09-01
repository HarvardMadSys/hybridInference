import { describe, expect, it } from 'vitest';
import { buildTimeSiteConfig, resolveRuntimeSiteConfig } from './site-config';

const runtimeBranding = {
  app_description: 'Runtime description',
  site_host: 'runtime.example.test',
  organization: {
    name: 'Runtime Org',
    url: 'https://org.example.test',
    tagline: 'Runtime tagline',
  },
  links: {
    docs_url: 'https://docs.example.test',
    status_url: 'https://status.example.test',
    github_url: 'https://github.com/example/runtime/',
  },
  example: {
    api_base: 'https://api.example.test/',
    api_key_env_var: 'RUNTIME_API_KEY',
    model: 'runtime-model',
  },
  analytics: {
    statcounter_project_id: '12345',
    statcounter_security_key: 'safe_key',
  },
  signup: {
    turnstile_site_key: 'turnstile-site-key',
    fast_track_domain: 'example.edu',
    fast_track_org: 'Example University',
  },
  storage_key_prefix: 'runtime-example',
  data_policy_notice: 'Runtime data policy.',
  assets: {
    logo_url: '/site-assets/logo.svg',
    favicon_url: 'https://assets.example.test/favicon.ico',
  },
  team: [
    {
      name: 'Ada Example',
      affiliations: ['Example University'],
      badge: 'Lead',
      image: '/site-assets/team/ada.webp',
      website: 'https://ada.example.test',
    },
  ],
  sponsors: [
    {
      name: 'Example Labs',
      alt: 'Example Labs logo',
      src: 'https://assets.example.test/sponsor.svg',
      class_name: 'h-10',
      width: 200,
      height: 50,
    },
  ],
};

const runtimeDocument = {
  schema_version: 1,
  distribution: {
    id: 'example',
    display_name: 'Example Inference',
    release: '2026.07',
  },
  site: {
    public_base_url: 'https://inference.example.test/',
    support_email: 'support@example.test',
  },
  features: {
    routers: ['fixed'],
    public_signup: false,
    rag: false,
  },
  branding: runtimeBranding,
};

describe('resolveRuntimeSiteConfig', () => {
  it('maps the complete v1 runtime branding document', () => {
    const resolved = resolveRuntimeSiteConfig(runtimeDocument);

    expect(resolved.branding).toMatchObject({
      appName: 'Example Inference',
      appDescription: 'Runtime description',
      siteHost: 'runtime.example.test',
      orgName: 'Runtime Org',
      orgUrl: 'https://org.example.test',
      orgTagline: 'Runtime tagline',
      docsUrl: 'https://docs.example.test',
      statusUrl: 'https://status.example.test',
      githubUrl: 'https://github.com/example/runtime',
      commitUrlBase: 'https://github.com/example/runtime/commit',
      contactEmail: 'support@example.test',
      exampleApiBase: 'https://api.example.test',
      exampleApiKeyEnvVar: 'RUNTIME_API_KEY',
      exampleModel: 'runtime-model',
      statcounterProjectId: '12345',
      statcounterSecurityKey: 'safe_key',
      turnstileSiteKey: 'turnstile-site-key',
      fastTrackDomain: 'example.edu',
      fastTrackOrg: 'Example University',
      storageKeyPrefix: 'runtime-example',
      dataPolicyNotice: 'Runtime data policy.',
      logoUrl: '/site-assets/logo.svg',
      faviconUrl: 'https://assets.example.test/favicon.ico',
    });
    expect(resolved.branding.team).toEqual(runtimeBranding.team);
    expect(resolved.branding.sponsors).toEqual([
      {
        name: 'Example Labs',
        alt: 'Example Labs logo',
        src: 'https://assets.example.test/sponsor.svg',
        className: 'h-10',
        width: 200,
        height: 50,
      },
    ]);
    expect(resolved.distribution).toEqual({ id: 'example', release: '2026.07' });
    expect(resolved.features).toEqual({ publicSignup: false, rag: false, agents: false });
  });

  it('preserves build-time branding exactly when v1 branding is null', () => {
    const resolved = resolveRuntimeSiteConfig({ ...runtimeDocument, branding: null });

    expect(resolved.branding).toBe(buildTimeSiteConfig.branding);
    expect(resolved.distribution.id).toBe('example');
    expect(resolved.features.publicSignup).toBe(false);
  });

  it('accepts the backend neutral v1 shape while retaining every fallback brand value', () => {
    const resolved = resolveRuntimeSiteConfig({
      ...runtimeDocument,
      distribution: { id: 'neutral', display_name: '', release: '' },
      site: { public_base_url: '', support_email: '' },
      features: { routers: [], public_signup: null, rag: null },
      branding: null,
    });

    expect(resolved.branding).toBe(buildTimeSiteConfig.branding);
    expect(resolved.distribution).toEqual({ id: 'neutral', release: '' });
    expect(resolved.features).toEqual(buildTimeSiteConfig.features);
  });

  it('preserves legacy identity and disabled features during a rolling upgrade', () => {
    const {
      schema_version: _schemaVersion,
      branding: _branding,
      ...legacyDocument
    } = runtimeDocument;

    const resolved = resolveRuntimeSiteConfig(legacyDocument);

    expect(resolved.branding).toMatchObject({
      appName: 'Example Inference',
      exampleApiBase: 'https://inference.example.test',
      siteHost: 'inference.example.test',
      contactEmail: 'support@example.test',
    });
    expect(resolved.distribution).toEqual({ id: 'example', release: '2026.07' });
    expect(resolved.features).toEqual({ publicSignup: false, rag: false, agents: false });
  });

  it('keeps legacy feature gates when its optional identity URL is malformed', () => {
    const {
      schema_version: _schemaVersion,
      branding: _branding,
      ...legacyDocument
    } = runtimeDocument;
    const resolved = resolveRuntimeSiteConfig({
      ...legacyDocument,
      site: { ...legacyDocument.site, public_base_url: 'javascript:alert(1)' },
    });

    expect(resolved.branding.exampleApiBase).toBe(buildTimeSiteConfig.branding.exampleApiBase);
    expect(resolved.branding.siteHost).toBe(buildTimeSiteConfig.branding.siteHost);
    expect(resolved.branding.appName).toBe('Example Inference');
    expect(resolved.features).toEqual({ publicSignup: false, rag: false, agents: false });
  });

  it('preserves build-time branding when a branding field is unsafe or malformed', () => {
    const resolved = resolveRuntimeSiteConfig({
      ...runtimeDocument,
      branding: {
        ...runtimeBranding,
        assets: { ...runtimeBranding.assets, logo_url: 'javascript:alert(1)' },
      },
    });

    expect(resolved.branding).toBe(buildTimeSiteConfig.branding);

    const withUnknownField = resolveRuntimeSiteConfig({
      ...runtimeDocument,
      branding: { ...runtimeBranding, internal_service_url: 'http://private.service' },
    });
    expect(withUnknownField.branding).toBe(buildTimeSiteConfig.branding);
    expect(JSON.stringify(withUnknownField)).not.toContain('private.service');

    const withUncompiledSponsorClass = resolveRuntimeSiteConfig({
      ...runtimeDocument,
      branding: {
        ...runtimeBranding,
        sponsors: [
          {
            ...runtimeBranding.sponsors[0],
            class_name: 'absolute bg-[url(javascript:alert(1))]',
          },
        ],
      },
    });
    expect(withUncompiledSponsorClass.branding).toBe(buildTimeSiteConfig.branding);
  });

  it('rejects malformed or unsupported-version documents without changing the fallback', () => {
    expect(resolveRuntimeSiteConfig({ distribution: {} })).toBe(buildTimeSiteConfig);
    expect(resolveRuntimeSiteConfig({ ...runtimeDocument, schema_version: 2 })).toBe(
      buildTimeSiteConfig,
    );
  });
});
