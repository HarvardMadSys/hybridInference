/**
 * Navigation helpers for auth round-trips.
 *
 * Anything that sends the browser to a caller-supplied destination after an
 * auth step goes through here, so the "is this ours?" decision exists exactly
 * once.
 */

/**
 * Return `raw` when it is a path inside this app, else `fallback`.
 *
 * Guards `?next=` round-trips: login must only ever return the user somewhere
 * within the app, never to wherever a crafted link pointed. A qualifying value
 * starts with exactly one `/` — a second slash or a backslash is how a "path"
 * smuggles in a host (browsers read `//evil.test` and `/\evil.test` as
 * protocol-relative URLs), and anything with a scheme is an absolute URL
 * outright, which `startsWith('/')` already refuses.
 */
export function internalPathOr(fallback: string, raw: string | null | undefined): string {
  if (!raw || !raw.startsWith('/')) return fallback;
  const second = raw.charAt(1);
  if (second === '/' || second === '\\') return fallback;
  return raw;
}

/**
 * Full-page navigation to an absolute URL.
 *
 * A wrapper because jsdom's `window.location` is unforgeable: tests cannot
 * observe `location.assign` directly, but they can mock this module.
 */
export function navigateTo(url: string): void {
  window.location.assign(url);
}
