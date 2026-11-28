/**
 * Where a sign-up should lead once the new account can sign in.
 *
 * `?next=` carries a destination through login and sign-up in the address. The
 * one step it cannot ride along is email verification: the link arrives by
 * mail, usually opened in a new tab, and the page it opens knows nothing of the
 * page that sent the visitor to sign up. So the destination is also kept in this
 * browser's storage when an account still has to verify its address, and the
 * verification page offers it back afterwards.
 *
 * Only an internal path is ever kept or returned, judged by the same rule as
 * login (`internalPathOr`), and only for as long as a verification link lives:
 * a destination from yesterday is not where anyone is going today. Storage can
 * be unavailable or refuse a write (private windows, quota, policy); the
 * destination is then simply not carried, which is how the console behaved
 * without this.
 */

import { internalPathOr } from '@/lib/utils/navigation';

/** Neutral, like the theme's: the console's own storage, whatever the deployment. */
export const AUTH_NEXT_STORAGE_KEY = 'hybridinference-auth-next';

/** A verification link's lifetime; a destination is not kept past it. */
export const AUTH_NEXT_LIFETIME_MS = 24 * 60 * 60 * 1000;

interface StoredNext {
  path: string;
  at: number;
}

function storage(): Storage | null {
  try {
    return typeof window === 'undefined' ? null : window.localStorage;
  } catch {
    return null;
  }
}

/** Keep `path` for the page a verification link opens, if it is an internal path. */
export function rememberAuthNext(path: string | null | undefined): void {
  const accepted = internalPathOr('', path);
  if (!accepted) return;
  const store = storage();
  if (!store) return;
  try {
    const value: StoredNext = { path: accepted, at: Date.now() };
    store.setItem(AUTH_NEXT_STORAGE_KEY, JSON.stringify(value));
  } catch {
    // Not carried this time; nothing else depends on it.
  }
}

/** Forget any kept destination. */
export function forgetAuthNext(): void {
  const store = storage();
  if (!store) return;
  try {
    store.removeItem(AUTH_NEXT_STORAGE_KEY);
  } catch {
    // Nothing to clean up that could be read back anyway.
  }
}

/**
 * The destination kept by {@link rememberAuthNext}, or `null` when there is
 * none, it is older than a verification link, or it is not an internal path.
 */
export function recallAuthNext(): string | null {
  const store = storage();
  if (!store) return null;
  let raw: string | null;
  try {
    raw = store.getItem(AUTH_NEXT_STORAGE_KEY);
  } catch {
    return null;
  }
  if (!raw) return null;
  let stored: Partial<StoredNext> | null = null;
  try {
    stored = JSON.parse(raw) as Partial<StoredNext>;
  } catch {
    stored = null;
  }
  const age =
    stored && typeof stored.at === 'number' ? Date.now() - stored.at : Number.POSITIVE_INFINITY;
  const path = stored && typeof stored.path === 'string' ? internalPathOr('', stored.path) : '';
  if (!path || age < 0 || age > AUTH_NEXT_LIFETIME_MS) {
    forgetAuthNext();
    return null;
  }
  return path;
}

/** `/login`, carrying `next` when it is an internal path; bare otherwise. */
export function loginHref(next: string | null | undefined): string {
  const accepted = internalPathOr('', next);
  return accepted ? `/login?next=${encodeURIComponent(accepted)}` : '/login';
}

/** `/signup`, carrying `next` when it is an internal path; bare otherwise. */
export function signupHref(next: string | null | undefined): string {
  const accepted = internalPathOr('', next);
  return accepted ? `/signup?next=${encodeURIComponent(accepted)}` : '/signup';
}
