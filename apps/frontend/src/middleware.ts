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
 * instead of being held in this container's memory. Its one job on those paths
 * is the redirect, so an API path ending in a slash still reaches it: this is
 * the only place that answers `/v1/models/` with a redirect a client can follow.
 * Left to the gateway, the redirect would name the gateway's internal address.
 */
import { NextResponse, type NextRequest } from 'next/server';

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

  return NextResponse.next();
}

export const config = {
  // Literal strings, because Next reads this object at build time without
  // running the module: a constant or a template with a placeholder fails the
  // build. The gateway's first segments therefore appear twice -- keep the two
  // lists the same. Each segment matches whole: `/authorize` and `/stats` are
  // console pages. src/middleware.test.ts checks the compiled matchers.
  matcher: [
    // The console's pages: everything except Next's own build output, its
    // static assets, the gateway's paths and the proxied apps.
    '/((?!(?:_next/static|_next/image|favicon\\.ico|v1|anthropic|internal|auth|user|admin|health|site-config|site-updates|public-stats|site-assets|pgadmin|agents)(?:/|$)).*)',
    // A gateway path that ends in a slash, for the redirect alone.
    '/((?:v1|anthropic|internal|auth|user|admin|health|site-config|site-updates|public-stats|site-assets)(?:/.*)?/)',
  ],
};
