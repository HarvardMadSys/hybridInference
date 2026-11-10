/**
 * Restores Next's trailing-slash redirect for the console's own pages.
 *
 * `skipTrailingSlashRedirect` in next.config.js turns that redirect off
 * globally, because proxy targets own their path semantics. Next redirecting
 * a slash away while pgAdmin or the agent web app adds it back would bounce a
 * browser between the two indefinitely. Proxied paths therefore have to reach
 * their upstream exactly as the browser asked for them.
 *
 * Every other path keeps the behaviour it had before that flag: `/login/`
 * still lands on `/login`, so existing links and bookmarks are unaffected.
 *
 * **Which requests reach it** (`config.matcher` below) matters as much as what
 * it does, because Next copies the body of every request a middleware sees, and
 * hands the rest of the app that copy. The API and the proxies (`/v1`,
 * `/anthropic`, `/auth`, `/user`, `/admin`, `/internal`, `/health`, the public
 * site documents, `/pgadmin`, `/agents`) are not routed through it, so their
 * bodies stream to the gateway as they arrive -- before any key is checked --
 * instead of being held in this container's memory. A gateway path that ends in
 * a slash gets its redirect from next.config.js `redirects()` instead: a
 * matcher cannot single those out, because Next checks the matcher a second
 * time with the trailing slash removed, and a request matched only the first
 * time is answered with an empty 200.
 *
 * It also gives each page its Content-Security-Policy, whose script nonce has to
 * be new for every response: see src/lib/security/contentSecurityPolicy.ts.
 */
import { NextResponse, type NextRequest } from 'next/server';

import { config as env } from '@/config/env';
import { consolePagePolicy, makeNonce } from '@/lib/security/contentSecurityPolicy';

const PROXIED_PREFIXES = ['/agents', '/pgadmin'] as const;

export function middleware(request: NextRequest): NextResponse {
  const { pathname } = request.nextUrl;

  if (PROXIED_PREFIXES.some((prefix) => pathname === prefix || pathname.startsWith(`${prefix}/`))) {
    return NextResponse.next();
  }

  if (pathname.length > 1 && pathname.endsWith('/')) {
    // Built from request.url rather than nextUrl.clone(): NextURL remembers
    // that the incoming path ended in a slash and puts it back when the URL is
    // serialized, so cloning redirects the request to exactly where it already
    // is. A plain URL has no such memory.
    const url = new URL(request.url);
    url.pathname = pathname.replace(/\/+$/, '');
    return NextResponse.redirect(url, 308);
  }

  // A page. Next reads the nonce out of the policy on the request and writes
  // it onto every script it renders; the layout reads `x-nonce` for its own.
  const nonce = makeNonce();
  const policy = consolePagePolicy(nonce, {
    apiBase: env.apiBase,
    development: env.isDevelopment,
  });
  const headers = new Headers(request.headers);
  headers.set('x-nonce', nonce);
  headers.set('content-security-policy', policy);
  const response = NextResponse.next({ request: { headers } });
  response.headers.set('content-security-policy', policy);
  return response;
}

export const config = {
  // The console's pages: everything except Next's own build output, its static
  // assets, the gateway's paths and the proxied apps. Each segment matches
  // whole: `/authorize` and `/stats` are console pages. A literal string,
  // because Next reads this object at build time without running the module;
  // the gateway's segments are listed again in next.config.js `redirects()`.
  // src/middleware.test.ts checks the compiled matcher.
  matcher: [
    '/((?!(?:_next/static|_next/image|favicon\\.ico|v1|anthropic|internal|auth|user|admin|health|site-config|site-updates|public-stats|site-assets|pgadmin|agents)(?:/|$)).*)',
  ],
};
