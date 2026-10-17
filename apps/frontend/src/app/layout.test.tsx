import { renderToStaticMarkup } from 'react-dom/server';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { buildTimeSiteConfig } from '@/config/site-config';

const state = vi.hoisted(() => ({
  siteConfig: undefined as unknown,
  error: null as Error | null,
  locale: 'en' as 'en' | 'zh-CN',
}));

vi.mock('next/font/google', () => ({
  Crimson_Text: () => ({ variable: 'runtime-font' }),
}));

vi.mock('next/script', () => ({
  default: ({ children, id }: { children?: React.ReactNode; id?: string }) => (
    <script id={id}>{children}</script>
  ),
}));

vi.mock('@/config/site-config.server', () => ({
  loadRuntimeSiteConfig: async () => {
    if (state.error) throw state.error;
    return state.siteConfig;
  },
}));

// The request's language comes from its cookie and `Accept-Language`, which
// only exist inside a request; `src/lib/i18n/choice.test.ts` covers that
// resolution, and this file what the layout does with its answer.
vi.mock('@/lib/i18n/request.server', () => ({
  requestLocale: async () => state.locale,
  catalogFor: (locale: string) =>
    locale === 'zh-CN' ? { 'chrome.header.stats': { source: 'Stats', text: '统计' } } : undefined,
}));

vi.mock('@/components/providers', () => ({
  Providers: ({
    children,
    initialSiteConfig,
    locale,
    catalog,
  }: {
    children: React.ReactNode;
    initialSiteConfig: typeof buildTimeSiteConfig;
    locale: string;
    catalog?: object;
  }) => (
    <div
      data-initial-app-name={initialSiteConfig.branding.appName}
      data-locale={locale}
      data-catalog-slots={catalog ? Object.keys(catalog).length : 0}
    >
      {children}
    </div>
  ),
}));

vi.mock('@/components/ui/ErrorBoundary', () => ({
  ErrorBoundary: ({ children }: { children: React.ReactNode }) => children,
}));
// The Site UI boundary and the chrome decision are separate components with
// their own coverage (`src/site-ui/*.test.tsx`); this file is about what the
// ROOT layout renders, so both are reduced to their children here.
vi.mock('@/site-ui/SiteUiBoundary', () => ({
  SiteUiBoundary: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}));
vi.mock('@/site-ui/PublicRouteBoundary', () => ({
  PublicRouteBoundary: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}));
// The document language is `SiteDocument`'s decision, with its own tests; here
// it is the element, showing what the layout handed it.
vi.mock('@/site-ui/SiteDocument', () => ({
  SiteDocument: ({
    moduleLocale,
    consoleLocale,
    className,
    children,
  }: {
    moduleLocale: string;
    consoleLocale: string;
    className?: string;
    children: React.ReactNode;
  }) => (
    <html
      data-module-locale={moduleLocale}
      data-console-locale={consoleLocale}
      className={className}
    >
      {children}
    </html>
  ),
}));

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
    state.error = null;
    state.siteConfig = runtimeConfig();
    state.locale = 'en';
  });

  it('propagates configuration failures to the global error boundary before rendering the app', async () => {
    state.error = new Error('configuration unavailable');

    await expect(generateMetadata()).rejects.toBe(state.error);
    await expect(RootLayout({ children: <p>signup form</p> })).rejects.toBe(state.error);
  });

  it('uses runtime identity for metadata and the initial provider value', async () => {
    expect(await generateMetadata()).toEqual({
      title: 'Runtime Console',
      description: 'Runtime description',
      icons: { icon: '/site-assets/favicon.ico' },
    });

    const html = renderToStaticMarkup(await RootLayout({ children: <p>page</p> }));
    expect(html).toContain('data-initial-app-name="Runtime Console"');
    // The document element gets the server module's locale to decide with; the
    // neutral module declares none.
    expect(html).toContain(
      '<html data-module-locale="" data-console-locale="en" class="h-full runtime-font">',
    );
    // English is the source: no catalog crosses to the client.
    expect(html).toContain('data-locale="en" data-catalog-slots="0"');
  });

  it("hands the request's language, and only that language's catalog, to the document and the providers", async () => {
    state.locale = 'zh-CN';

    const html = renderToStaticMarkup(await RootLayout({ children: <p>page</p> }));
    expect(html).toContain('data-console-locale="zh-CN"');
    expect(html).toContain('data-locale="zh-CN" data-catalog-slots="1"');
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
