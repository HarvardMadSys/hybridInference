/**
 * The console's own cookies, as the console's proxies see them.
 *
 * FastAPI issues a long-lived refresh cookie on `/`, so the browser attaches it
 * to every same-origin request — including the ones a route handler relays to
 * another service. That cookie mints console access tokens; no service behind a
 * proxy has a use for it, and each one that receives it is one more place it
 * can be logged or leaked. Server-only: the name comes from the runtime
 * environment, which the browser bundle never sees.
 *
 * The other direction matters as much. pgAdmin and the agent web app are served
 * on this origin, so a cookie either one sets is a cookie of this origin: one
 * named like the console's replaces it (a session swapped for someone else's,
 * or cleared), and one on `/` rides along on every console and API request.
 * `scopeProxiedSetCookie` keeps an app's cookies to the app.
 */
import { LOCALE_COOKIE } from '@/lib/i18n/choice';

/** Session cookies issued by FastAPI: the default name and the deployment's. */
export function consoleSessionCookieNames(): Set<string> {
  return new Set(['refresh_token', process.env.REFRESH_TOKEN_COOKIE_NAME || 'refresh_token']);
}

/** Return a Cookie header without the console session, or null if nothing remains. */
export function withoutConsoleSession(cookie: string | null): string | null {
  if (!cookie) return null;
  const consoleSessions = consoleSessionCookieNames();
  const kept = cookie
    .split(';')
    .map((part) => part.trim())
    .filter((part) => {
      const separator = part.indexOf('=');
      const name = (separator === -1 ? part : part.slice(0, separator)).trim();
      return part.length > 0 && !consoleSessions.has(name);
    });
  return kept.length > 0 ? kept.join('; ') : null;
}

/**
 * Every cookie the console and its gateway set on this origin, lower-cased:
 * the session (both names, as above), the device cookie the gateway names after
 * it (serving/utils/device_cookie.py), and the reader's language.
 */
function consoleOwnedCookieNames(): Set<string> {
  const session = process.env.REFRESH_TOKEN_COOKIE_NAME || 'refresh_token';
  return new Set(
    ['refresh_token', session, 'refresh_token_device', `${session}_device`, LOCALE_COOKIE].map(
      (name) => name.toLowerCase(),
    ),
  );
}

/** `__Host-` cookies must keep `Path=/`; a browser rejects one that does not. */
const HOST_PREFIX = '__host-';

function withinPrefix(path: string, prefix: string): boolean {
  return path === prefix || path.startsWith(`${prefix}/`);
}

/**
 * A proxied app's `Set-Cookie`, confined to the app, or null to drop it.
 *
 * - A cookie named like one of the console's is dropped, whatever its path: an
 *   app behind a proxy has no business setting, replacing or clearing those.
 *   So is a nameless one, which some browsers match against any name.
 * - `Domain` is removed, so the cookie stays on this host and never spreads to
 *   sibling subdomains.
 * - `Path` is narrowed to `pathPrefix` unless it already lies inside it, so the
 *   cookie is not sent with the console's own or the gateway's requests.
 *
 * The one exception is a `__Host-` cookie, which browsers accept only with
 * `Path=/`: narrowing it would discard it, and with it the agent's sign-in.
 * Its path is left alone. Its name cannot be one of the console's, which the
 * first rule already guarantees, and the prefix itself forbids `Domain`.
 */
export function scopeProxiedSetCookie(setCookie: string, pathPrefix: string): string | null {
  const [pair, ...attributes] = setCookie.split(';');
  const separator = pair.indexOf('=');
  const name = (separator === -1 ? '' : pair.slice(0, separator)).trim();
  if (!name || consoleOwnedCookieNames().has(name.toLowerCase())) return null;

  const kept: string[] = [];
  let path: string | null = null;
  for (const raw of attributes) {
    const attribute = raw.trim();
    if (!attribute) continue;
    const equals = attribute.indexOf('=');
    const key = (equals === -1 ? attribute : attribute.slice(0, equals)).trim().toLowerCase();
    if (key === 'domain') continue;
    if (key === 'path') {
      // The last Path attribute is the one a browser applies.
      path = equals === -1 ? '' : attribute.slice(equals + 1).trim();
      continue;
    }
    kept.push(attribute);
  }

  if (name.toLowerCase().startsWith(HOST_PREFIX)) {
    if (path !== null) kept.unshift(`Path=${path}`);
  } else {
    kept.unshift(`Path=${path !== null && withinPrefix(path, pathPrefix) ? path : pathPrefix}`);
  }
  return [pair.trim(), ...kept].join('; ');
}
