/**
 * The Content-Security-Policy of the console's pages.
 *
 * Scripts run only with this response's nonce. Next puts it on every script it
 * writes -- the framework's chunks, the inline payload that hydrates a page,
 * `next/script` -- once it finds it in the request's own policy, which
 * src/middleware.ts adds; the console's one hand-written inline script reads it
 * from the `x-nonce` request header. `'strict-dynamic'` extends that trust to the
 * scripts those load in turn (Next's later chunks, Turnstile, the analytics
 * counter) and to nothing else, so a script an attacker manages to put on a page
 * -- or the HTML of an injected one -- does not run. The two hosts beside it are
 * only for browsers too old to understand `'strict-dynamic'`, which ignore it.
 *
 * Everything else is held to what the pages use:
 *
 * - `img-src` allows any HTTPS image, because a deployment's logo, sponsors and
 *   team photos come from hosts it chooses at runtime. Text nobody here wrote --
 *   model output -- never renders an image at all (see
 *   src/lib/utils/markdownImages.ts), which is what an image-borne leak needs.
 * - `style-src` allows inline styles: React writes `style` attributes into
 *   server-rendered HTML, and a nonce cannot cover an attribute.
 * - `connect-src` adds the API's origin when the build points the browser at a
 *   gateway on another origin (NEXT_PUBLIC_API_BASE; see src/config/env.ts).
 *
 * Only pages carry it. The API's responses are not documents, and pgAdmin and
 * the agent web app, served under /pgadmin and /agents, are other applications
 * with their own scripts: the middleware does not run for any of them.
 */

/** Hosts a page loads scripts, frames or beacons from. */
const TURNSTILE = 'https://challenges.cloudflare.com';
const STATCOUNTER_SCRIPT = 'https://www.statcounter.com';
const STATCOUNTER_ANY = 'https://*.statcounter.com';

/** A fresh, unguessable nonce: 128 random bits, base64-encoded. */
export function makeNonce(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return btoa(String.fromCharCode(...bytes));
}

/** The origin of an absolute API base, or null for a same-origin one. */
function apiOrigin(apiBase: string): string | null {
  if (!apiBase) return null;
  try {
    const { origin, protocol } = new URL(apiBase);
    return protocol === 'http:' || protocol === 'https:' ? origin : null;
  } catch {
    return null;
  }
}

export interface PolicyOptions {
  /** The API base the browser bundle calls: `config.apiBase` in src/config/env.ts. */
  apiBase: string;
  /** `next dev` evaluates code and reloads over a websocket; a build does neither. */
  development: boolean;
}

/** The policy for one page response, with that response's nonce. */
export function consolePagePolicy(nonce: string, { apiBase, development }: PolicyOptions): string {
  const api = apiOrigin(apiBase);
  const directives: Record<string, string[]> = {
    'default-src': ["'self'"],
    'script-src': [
      "'self'",
      `'nonce-${nonce}'`,
      "'strict-dynamic'",
      TURNSTILE,
      STATCOUNTER_SCRIPT,
      ...(development ? ["'unsafe-eval'"] : []),
    ],
    'style-src': ["'self'", "'unsafe-inline'"],
    'img-src': ["'self'", 'data:', 'blob:', 'https:'],
    'font-src': ["'self'", 'data:'],
    'connect-src': [
      "'self'",
      ...(api ? [api] : []),
      TURNSTILE,
      STATCOUNTER_ANY,
      ...(development ? ['ws:', 'wss:'] : []),
    ],
    'frame-src': ["'self'", TURNSTILE],
    'worker-src': ["'self'", 'blob:'],
    'object-src': ["'none'"],
    'base-uri': ["'self'"],
    'form-action': ["'self'"],
    'frame-ancestors': ["'none'"],
  };
  return Object.entries(directives)
    .map(([name, values]) => `${name} ${values.join(' ')}`)
    .join('; ');
}
