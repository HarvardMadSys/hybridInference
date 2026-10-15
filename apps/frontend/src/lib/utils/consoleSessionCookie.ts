/**
 * The console's own session cookie, as the console's proxies see it.
 *
 * FastAPI issues a long-lived refresh cookie on `/`, so the browser attaches it
 * to every same-origin request — including the ones a route handler relays to
 * another service. That cookie mints console access tokens; no service behind a
 * proxy has a use for it, and each one that receives it is one more place it
 * can be logged or leaked. Server-only: the name comes from the runtime
 * environment, which the browser bundle never sees.
 */

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
