import { describe, expect, it } from 'vitest';
import { buildTimeSiteConfig, type RuntimeSiteConfig } from './site-config';
import { pageMetadata, rootMetadata } from './site-metadata';

const runtimeConfig: RuntimeSiteConfig = {
  ...buildTimeSiteConfig,
  branding: {
    ...buildTimeSiteConfig.branding,
    appName: 'Runtime Console',
    appDescription: 'Runtime description',
    faviconUrl: '/site-assets/favicon.ico',
  },
};

describe('runtime site metadata', () => {
  it('uses the runtime identity and favicon for the root document', () => {
    expect(rootMetadata(runtimeConfig)).toEqual({
      title: 'Runtime Console',
      description: 'Runtime description',
      icons: { icon: '/site-assets/favicon.ico' },
    });
  });

  it('uses the runtime application name for page titles', () => {
    expect(pageMetadata(runtimeConfig, 'Team', 'Runtime team')).toEqual({
      title: 'Team | Runtime Console',
      description: 'Runtime team',
    });
  });
});
