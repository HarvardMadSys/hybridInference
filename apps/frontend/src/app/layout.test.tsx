import { renderToStaticMarkup } from 'react-dom/server';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { buildTimeSiteConfig } from '@/config/site-config';

const state = vi.hoisted(() => ({ siteConfig: undefined as unknown }));

vi.mock('next/font/google', () => ({
  Crimson_Text: () => ({ variable: 'runtime-font' }),
}));

vi.mock('next/script', () => ({
  default: ({ children, id }: { children?: React.ReactNode; id?: string }) => (
    <script id={id}>{children}</script>
  ),
}));

vi.mock('@/config/site-config.server', () => ({
  loadRuntimeSiteConfig: async () => state.siteConfig,
}));

vi.mock('@/components/providers', () => ({
  Providers: ({
    children,
    initialSiteConfig,
  }: {
    children: React.ReactNode;
    initialSiteConfig: typeof buildTimeSiteConfig;
  }) => <div data-initial-app-name={initialSiteConfig.branding.appName}>{children}</div>,
}));

vi.mock('@/components/ui/ErrorBoundary', () => ({
  ErrorBoundary: ({ children }: { children: React.ReactNode }) => children,
}));
vi.mock('@/components/ui/Header', () => ({ Header: () => <div>header</div> }));
vi.mock('@/components/ui/SiteFooter', () => ({ SiteFooter: () => <div>footer</div> }));

import RootLayout, { generateMetadata } from './layout';

function runtimeConfig(statcounterProjectId = '12345') {
  return {
    ...buildTimeSiteConfig,
    branding: {
      ...buildTimeSiteConfig.branding,
      appName: 'Runtime Console',
      appDescription: 'Runtime description',
      faviconUrl: '/site-assets/favicon.ico',
      statcounterProjectId,
      statcounterSecurityKey: 'runtime_key',
    },
  };
}

describe('RootLayout runtime identity', () => {
  beforeEach(() => {
    state.siteConfig = runtimeConfig();
  });

  it('uses runtime identity for metadata and the initial provider value', async () => {
    expect(await generateMetadata()).toEqual({
      title: 'Runtime Console',
      description: 'Runtime description',
      icons: { icon: '/site-assets/favicon.ico' },
    });

    const html = renderToStaticMarkup(await RootLayout({ children: <p>page</p> }));
    expect(html).toContain('data-initial-app-name="Runtime Console"');
  });

  it('renders analytics only when the runtime document enables it', async () => {
    const enabled = renderToStaticMarkup(await RootLayout({ children: null }));
    expect(enabled).toContain('var sc_project=12345');

    state.siteConfig = runtimeConfig('');
    const disabled = renderToStaticMarkup(await RootLayout({ children: null }));
    expect(disabled).not.toContain('statcounter-config');
    expect(disabled).not.toContain('c.statcounter.com');
  });
});
