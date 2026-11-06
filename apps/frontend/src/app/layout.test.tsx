import { renderToStaticMarkup } from 'react-dom/server';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { buildTimeSiteConfig } from '@/config/site-config';

const state = vi.hoisted(() => ({
  siteConfig: undefined as unknown,
  error: null as Error | null,
  locale: 'en' as 'en' | 'zh-CN',
  nonce: 'request-nonce' as string | null,
  cookies: [] as string[],
}));

// What src/middleware.ts hands the layout, and the cookies the request carried.
vi.mock('next/headers', () => ({
  headers: async () => new Headers(state.nonce ? { 'x-nonce': state.nonce } : {}),
  cookies: async () => ({ has: (name: string) => state.cookies.includes(name) }),
}));

// The analytics component decides by route on its own (PublicAnalytics.test.tsx);
// here it shows what the layout handed it.
vi.mock('@/components/analytics/PublicAnalytics', () => ({
  PublicAnalytics: (props: {
    projectId: string;
    securityKey: string;
    nonce?: string;
    signedOut: boolean;
  }) => (
    <i
      data-analytics={props.projectId}
      data-analytics-key={props.securityKey}
      data-analytics-nonce={props.nonce}
      data-signed-out={String(props.signedOut)}
    />
  ),
}));

vi.mock('next/font/google', () => ({
  Crimson_Text: () => ({ variable: 'runtime-font' }),
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
    state.nonce = 'request-nonce';
    state.cookies = [];
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
    expect(enabled).toContain('data-analytics="12345" data-analytics-key="runtime_key"');

    state.siteConfig = runtimeConfig('');
    const disabled = renderToStaticMarkup(await RootLayout({ children: null }));
    expect(disabled).not.toContain('data-analytics');
  });

  // The page's policy runs no script without this response's nonce, the
  // theme script included: without it the page renders in the wrong theme.
  it("puts the response's nonce on its own script and hands it to the analytics", async () => {
    const html = renderToStaticMarkup(await RootLayout({ children: null }));

    expect(html).toContain('<script id="console-theme" nonce="request-nonce">');
    expect(html).toContain('data-analytics-nonce="request-nonce"');
  });

  it('tells the analytics whether the request came with a console session', async () => {
    expect(renderToStaticMarkup(await RootLayout({ children: null }))).toContain(
      'data-signed-out="true"',
    );

    state.cookies = ['refresh_token'];
    expect(renderToStaticMarkup(await RootLayout({ children: null }))).toContain(
      'data-signed-out="false"',
    );
  });
});
