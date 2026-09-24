import type { PublicRoute } from './contract';

// Re-exported so a caller that deals in routes can name the type without also
// reaching for the contract module. `PublicRouteBoundary` is the main one: it
// switches on the key, and a `string` there is what let a `/`-vs-`'landing'`
// comparison compile.
export type { PublicRoute };

/**
 * Path → public route, for the routes a distribution's UI may take over.
 *
 * The host owns this mapping. A module cannot register a route, so it cannot
 * quietly extend its reach to `/dashboard` or claim a path it was not reviewed
 * for; adding a public route is a change to this table and therefore to the
 * shared repository.
 */
const PUBLIC_ROUTE_PATHS: Readonly<Record<string, PublicRoute>> = {
  '/': 'landing',
  '/login': 'login',
  '/signup': 'signup',
  '/forgot-password': 'forgot-password',
  '/reset-password': 'reset-password',
  '/verify-email': 'verify-email',
  '/terms': 'terms',
};

/**
 * Normalize a pathname for matching.
 *
 * One trailing slash is the same route. The query string and the fragment are
 * deliberately not part of the input at all — `/login?next=/team` is `/login`,
 * and `next` must survive to the shared controller untouched, which is why
 * this function never sees or returns it.
 */
export function normalizePublicPath(pathname: string): string {
  if (pathname === '/') return '/';
  return pathname.replace(/\/+$/, '') || '/';
}

/**
 * Which public route a pathname is, or `null` when it is not one.
 *
 * `null` is not a failure: it means "the shared console renders this path".
 * Matching is exact, so `/dashboard`, `/chat`, `/team`, `/authorize`,
 * `/agents` and an unknown `/not-a-page` all return `null` — the last one
 * because a prefix match would otherwise swallow every 404 into the home page.
 */
export function publicRouteFor(pathname: string): PublicRoute | null {
  return PUBLIC_ROUTE_PATHS[normalizePublicPath(pathname)] ?? null;
}

/** Every path a module can be asked to render, for tests and build checks. */
export function publicRoutePaths(): string[] {
  return Object.keys(PUBLIC_ROUTE_PATHS);
}

/**
 * The account routes. The shared controllers for these render a form and hand
 * it to the module's `AuthFrame`.
 */
export const AUTH_ROUTES = [
  'login',
  'signup',
  'forgot-password',
  'reset-password',
  'verify-email',
] as const satisfies readonly PublicRoute[];

export type AuthRoute = (typeof AUTH_ROUTES)[number];

export function isAuthRoute(route: PublicRoute): route is AuthRoute {
  return (AUTH_ROUTES as readonly PublicRoute[]).includes(route);
}
