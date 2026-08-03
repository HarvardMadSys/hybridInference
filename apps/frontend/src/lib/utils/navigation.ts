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
 * within the app, never to wherever a crafted link pointed. Character checks
 * are not enough on their own — the URL parser strips ASCII tab, newline and
 * CR *before* parsing, so `/\n/evil.test` reads as a path to a character test
 * and as protocol-relative `//evil.test` to the browser. So, two layers:
 *
 * 1. Refuse anything the parser would rewrite — control characters, and space
 *    with them since no path in this app contains one — rather than modelling
 *    the rewrite.
 * 2. Prove the value cannot escape the origin: resolve it against a sentinel
 *    and require that origin to survive. This is the check that covers
 *    `//host`, `/\host`, and whatever parser quirk of that family comes next.
 */
export function internalPathOr(fallback: string, raw: string | null | undefined): string {
  if (!raw || !raw.startsWith('/')) return fallback;
  if (/[\u0000-\u0020]/.test(raw)) return fallback;
  try {
    if (new URL(raw, 'https://sentinel.invalid').origin !== 'https://sentinel.invalid') {
      return fallback;
    }
  } catch {
    return fallback;
  }
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
