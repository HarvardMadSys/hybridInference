// @vitest-environment jsdom
import { act } from '@testing-library/react';
import { hydrateRoot } from 'react-dom/client';
import { renderToString } from 'react-dom/server';
import { beforeEach, describe, expect, it, vi } from 'vitest';

/**
 * `<html lang>` per route: the module's `locale` on the routes the module
 * renders, English everywhere else — as the server renders the first response,
 * and as a client-side navigation re-renders it.
 */

const pathname = vi.hoisted(() => ({ value: '/' }));

/** Which components the stand-in module exports; `null` is "not exported". */
const module_ = vi.hoisted(() => ({
  value: {
    Landing: null as null | (() => null),
    AuthFrame: null as null | (() => null),
    legalText: null as null | object,
  },
}));

vi.mock('next/navigation', () => ({ usePathname: () => pathname.value }));

vi.mock('@/site-ui/module', () => ({
  SITE_UI_CLIENT: {
    descriptor: { siteUiApi: 1, id: 'stand-in', locale: 'fr' },
    get Landing() {
      return module_.value.Landing;
    },
    get AuthFrame() {
      return module_.value.AuthFrame;
    },
    get legalText() {
      return module_.value.legalText;
    },
  },
}));

import { SiteDocument } from './SiteDocument';

const ACCOUNT_PAGES = ['/login', '/signup', '/forgot-password', '/reset-password', '/verify-email'];
const CONSOLE_PAGES = ['/dashboard', '/dashboard/admin', '/team', '/chat', '/authorize', '/nope'];

/** A module that renders every public route it can. */
function exportEverything() {
  module_.value = { Landing: () => null, AuthFrame: () => null, legalText: {} };
}

function exportNothing() {
  module_.value = { Landing: null, AuthFrame: null, legalText: null };
}

/** The document the root layout renders, reduced to its two children. */
function document_(moduleLocale: string) {
  return (
    <SiteDocument moduleLocale={moduleLocale} className="h-full">
      {/* The App Router's root layout renders `<head>` itself; `next/head` is
          the Pages Router's. */}
      {/* eslint-disable-next-line @next/next/no-head-element */}
      <head />
      <body />
    </SiteDocument>
  );
}

/** The language the server writes into the first response for a path. */
function serverLanguage(path: string, moduleLocale = 'fr'): string | undefined {
  pathname.value = path;
  return /^<html lang="([^"]*)"/.exec(renderToString(document_(moduleLocale)))?.[1];
}

beforeEach(exportNothing);

describe('the document language, as the server renders it', () => {
  it('is the module’s on every route the module renders', () => {
    exportEverything();

    for (const path of ['/', ...ACCOUNT_PAGES, '/terms', '/login/']) {
      expect(serverLanguage(path), path).toBe('fr');
    }
  });

  it('is English on every console route, whatever the module renders', () => {
    exportEverything();

    for (const path of CONSOLE_PAGES) {
      expect(serverLanguage(path), path).toBe('en');
    }
  });

  it('is English on a public route whose export the module does not supply', () => {
    // A landing page alone: the account pages and the terms are the console's.
    module_.value = { Landing: () => null, AuthFrame: null, legalText: null };

    expect(serverLanguage('/')).toBe('fr');
    for (const path of [...ACCOUNT_PAGES, '/terms']) {
      expect(serverLanguage(path), path).toBe('en');
    }

    module_.value = { Landing: null, AuthFrame: () => null, legalText: null };
    expect(serverLanguage('/')).toBe('en');
    expect(serverLanguage('/login')).toBe('fr');
    expect(serverLanguage('/terms')).toBe('en');

    module_.value = { Landing: null, AuthFrame: null, legalText: {} };
    expect(serverLanguage('/terms')).toBe('fr');
    expect(serverLanguage('/signup')).toBe('en');
  });

  it('is English everywhere when the module declares no language, or there is none', () => {
    exportEverything();
    expect(serverLanguage('/', '')).toBe('en');

    exportNothing();
    for (const path of ['/', '/login', '/terms', '/dashboard']) {
      expect(serverLanguage(path), path).toBe('en');
    }
  });
});

describe('the document language, across client-side navigation', () => {
  it('follows the route without a reload', async () => {
    exportEverything();
    // The document as the server sent it for a console page, hydrated as the
    // browser does; each later render is the router's, with a new pathname.
    pathname.value = '/dashboard';
    document.documentElement.setAttribute('lang', 'en');
    document.documentElement.setAttribute('class', 'h-full');
    let root!: ReturnType<typeof hydrateRoot>;
    await act(async () => {
      root = hydrateRoot(document, document_('fr'));
    });
    expect(document.documentElement.lang).toBe('en');

    for (const [path, lang] of [
      ['/', 'fr'],
      ['/team', 'en'],
      ['/login', 'fr'],
      ['/terms', 'fr'],
      ['/dashboard/settings', 'en'],
    ] as const) {
      pathname.value = path;
      await act(async () => {
        root.render(document_('fr'));
      });
      expect(document.documentElement.lang, path).toBe(lang);
    }
  });
});
