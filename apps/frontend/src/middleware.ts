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
  // Everything except Next's own build output and static assets, which never
  // carry a trailing slash and should not pay for a middleware hop.
  matcher: ['/((?!_next/static|_next/image|favicon.ico).*)'],
};
