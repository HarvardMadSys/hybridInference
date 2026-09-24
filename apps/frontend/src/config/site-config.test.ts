import { describe, expect, it } from 'vitest';
import { buildTimeSiteConfig, resolveRuntimeSiteConfig } from './site-config';
import { SiteConfigLoadError } from './site-config-error';

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
    nav: [{ label: 'Runtime Project', url: 'https://project.example.test/' }],
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
    expect(resolved.branding.navLinks).toEqual([
      { label: 'Runtime Project', url: 'https://project.example.test/' },
    ]);
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

  it('keeps valid runtime branding when the distribution display name is empty', () => {
    const resolved = resolveRuntimeSiteConfig({
      ...runtimeDocument,
      distribution: { ...runtimeDocument.distribution, display_name: '' },
    });

    expect(resolved.branding.appName).toBe(buildTimeSiteConfig.branding.appName);
    expect(resolved.branding.appDescription).toBe('Runtime description');
    expect(resolved.branding.logoUrl).toBe('/site-assets/logo.svg');
    expect(resolved.branding.docsUrl).toBe('https://docs.example.test');
    expect(resolved.branding.team).toEqual(runtimeBranding.team);
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

  it('rejects unsafe or malformed branding without silently replacing operator settings', () => {
    expect(() =>
      resolveRuntimeSiteConfig({
        ...runtimeDocument,
        branding: {
          ...runtimeBranding,
          assets: { ...runtimeBranding.assets, logo_url: 'javascript:alert(1)' },
        },
      }),
    ).toThrow(SiteConfigLoadError);

    // A key the console does not declare is refused, not dropped. This document
    // is served to every visitor, so an undeclared field is either a typo or
    // something that should not be public, and the gateway's own model forbids
    // both — a console that quietly ignored it would only ever hide a
    // deployment misconfiguration.
    expect(() =>
      resolveRuntimeSiteConfig({
        ...runtimeDocument,
        branding: { ...runtimeBranding, internal_service_url: 'http://private.service' },
      }),
    ).toThrow(SiteConfigLoadError);

    expect(() =>
      resolveRuntimeSiteConfig({
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
      }),
    ).toThrow(SiteConfigLoadError);

    for (const apiBase of [
      'https://api.example.test?tenant=example',
      'https://api.example.test?',
      'https://api.example.test#completion',
    ]) {
      expect(() =>
        resolveRuntimeSiteConfig({
          ...runtimeDocument,
          branding: {
            ...runtimeBranding,
            example: { ...runtimeBranding.example, api_base: apiBase },
          },
        }),
      ).toThrow(SiteConfigLoadError);
    }

    for (const [key, value] of [
      ['docs_url', 'https://docs.example.test?language=en'],
      ['docs_url', 'https://docs.example.test#'],
      ['github_url', 'https://github.com/example/runtime?tab=readme'],
      ['github_url', 'https://github.com/example/runtime#readme'],
    ] as const) {
      expect(() =>
        resolveRuntimeSiteConfig({
          ...runtimeDocument,
          branding: {
            ...runtimeBranding,
            links: { ...runtimeBranding.links, [key]: value },
          },
        }),
      ).toThrow(SiteConfigLoadError);
    }
  });

  it('keeps runtime branding when a document predates the nav link field', () => {
    const { nav: _omitted, ...linksWithoutNav } = runtimeBranding.links;
    const resolved = resolveRuntimeSiteConfig({
      ...runtimeDocument,
      branding: { ...runtimeBranding, links: linksWithoutNav },
    });

    // The whole document must survive an older backend, not fall back to the
    // neutral build-time identity over one absent optional key.
    expect(resolved.branding.navLinks).toEqual([]);
    expect(resolved.branding.siteHost).toBe('runtime.example.test');
  });

  it('rejects unusable nav links', () => {
    for (const nav of [
      [{ label: 'Runtime Project', url: 'javascript:alert(1)' }],
      [{ label: 'Runtime Project', url: 'http://project.example.test/' }],
      [{ label: 'Runtime Project', url: '' }],
      [{ label: '', url: 'https://project.example.test/' }],
      [{ label: 'Runtime Project', url: 'https://project.example.test/', target: '_self' }],
    ]) {
      expect(() =>
        resolveRuntimeSiteConfig({
          ...runtimeDocument,
          branding: { ...runtimeBranding, links: { ...runtimeBranding.links, nav } },
        }),
      ).toThrow(SiteConfigLoadError);
    }
  });

  it.each([
    null,
    {},
    { distribution: {} },
    { ...runtimeDocument, schema_version: 2 },
    { ...runtimeDocument, features: {} },
    { ...runtimeDocument, features: { routers: [], public_signup: 'false', rag: false } },
  ])('rejects malformed or unsupported-version documents: %j', (document) => {
    expect(() => resolveRuntimeSiteConfig(document)).toThrow(SiteConfigLoadError);
  });
});

describe('example quickstart base', () => {
  it('publishes no example base of its own at build time', () => {
    // The developer home derives the base from the console's API origin, so
    // a build must not advertise a localhost default over that origin.
    expect(buildTimeSiteConfig.branding.exampleHidden).toBe(false);
  });

  it('keeps a published runtime base and does not hide the example', () => {
    const resolved = resolveRuntimeSiteConfig(runtimeDocument);

    expect(resolved.branding.exampleApiBase).toBe('https://api.example.test');
    expect(resolved.branding.exampleHidden).toBe(false);
  });

  it('treats an explicitly empty runtime base as the hidden example', () => {
    const resolved = resolveRuntimeSiteConfig({
      ...runtimeDocument,
      branding: { ...runtimeBranding, example: { ...runtimeBranding.example, api_base: '' } },
    });

    expect(resolved.branding.exampleApiBase).toBe('');
    expect(resolved.branding.exampleHidden).toBe(true);
  });
});

describe('unknown configuration keys', () => {
  // The endpoint envelope accepts unknown keys during rolling upgrades, while
  // the public branding document is strict so unsupported settings fail clearly.
  it('ignores an unknown top-level key and refuses an unknown branding key', () => {
    const resolved = resolveRuntimeSiteConfig({
      ...runtimeDocument,
      content: { locale: 'zh-CN', strings: { 'landing.hero.subtitle': '面向科研的推理服务' } },
    });
    expect(resolved.branding.appDescription).toBe(runtimeBranding.app_description);
    expect(resolved).not.toHaveProperty('content');

    expect(() =>
      resolveRuntimeSiteConfig({
        ...runtimeDocument,
        branding: {
          ...runtimeBranding,
          theme: { accent: '#0052D9', accent_dark: '#003CAB', accent_light: '#3B7BFF' },
        },
      }),
    ).toThrow(SiteConfigLoadError);
  });

  it('still refuses a document that is genuinely malformed elsewhere', () => {
    // Tolerating a key this console does not read is not the same as accepting
    // anything: the keys that *are* this endpoint's contract keep their rules.
    expect(() => resolveRuntimeSiteConfig({ schema_version: 99, distribution: {} })).toThrow(
      SiteConfigLoadError,
    );
    expect(() =>
      resolveRuntimeSiteConfig({
        ...runtimeDocument,
        features: { ...runtimeDocument.features, routers: 'fixed' },
      }),
    ).toThrow(SiteConfigLoadError);
  });
});

describe('unsupported runtime layout settings', () => {
  // Public-page layouts and design assets belong to build-time modules.
  // These keys are not part of the runtime branding schema.
  it('refuses unsupported presentation and hero-image settings', () => {
    for (const branding of [
      { ...runtimeBranding, presentation: { preset: 'custom', model_families: [] } },
      { ...runtimeBranding, assets: { ...runtimeBranding.assets, hero_image_url: '/a.png' } },
    ]) {
      expect(() => resolveRuntimeSiteConfig({ ...runtimeDocument, branding })).toThrow(
        SiteConfigLoadError,
      );
    }
  });
});
