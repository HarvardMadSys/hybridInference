import { isLocale, matchAcceptLanguage, SOURCE_LOCALE, type Locale } from './locales';

/**
 * A reader's language choice: where it is kept, and how the browser's copy and
 * the account's copy are reconciled.
 *
 * Only a choice the reader made is ever stored. The language picked from the
 * browser's `Accept-Language` is worked out again on every request and never
 * written down, so "there is a record" means "the reader chose" — and a reader
 * who never chose keeps following their system language.
 */

/** The cookie the server reads on every request. */
export const LOCALE_COOKIE = 'hi_locale';

/** A year: the choice outlives a session, and every new choice renews it. */
const LOCALE_COOKIE_MAX_AGE_SECONDS = 365 * 24 * 60 * 60;

export interface LocaleChoice {
  locale: Locale;
  /** When the reader chose, in epoch milliseconds. */
  chosenAt: number;
  /**
   * The account the choice was made in, or `null` for one made signed out.
   *
   * A browser holds one active account at a time and can switch between
   * several, so a choice must say whose it is: only a signed-out choice, or the
   * active account's own, may ever be written into the active account.
   */
  owner: string | null;
  /**
   * True once `owner`'s account has stored the choice. A signed-out choice is
   * never stored; an owned one that is not stored is waiting to be (its first
   * attempt failed, or has not answered yet).
   */
  stored: boolean;
}

/** The account's copy, as `/user/me` returns it. */
export interface AccountLocaleChoice {
  value: string;
  chosen_at: string;
}

/** An account id, as the cookie carries it: opaque, and never containing `|`. */
const OWNER_PATTERN = /^[A-Za-z0-9_-]{1,64}$/;

/**
 * `zh-CN|1760700000000|a` for a signed-out choice; `…|p|<account>` for an
 * account's choice not yet stored, `…|s|<account>` for a stored one.
 */
export function encodeLocaleChoice(choice: LocaleChoice): string {
  const head = `${choice.locale}|${Math.trunc(choice.chosenAt)}`;
  if (choice.owner === null) return `${head}|a`;
  return `${head}|${choice.stored ? 's' : 'p'}|${choice.owner}`;
}

/** The choice in a cookie value, or `null` for anything this build did not write. */
export function decodeLocaleChoice(value: string | null | undefined): LocaleChoice | null {
  if (!value) return null;
  let decoded: string;
  try {
    decoded = decodeURIComponent(value);
  } catch {
    return null;
  }
  const [locale, chosenAt, state, owner, ...rest] = decoded.split('|');
  if (rest.length > 0 || !isLocale(locale)) return null;
  const at = Number(chosenAt);
  if (!Number.isSafeInteger(at) || at <= 0) return null;
  if (state === 'a' && owner === undefined) {
    return { locale, chosenAt: at, owner: null, stored: false };
  }
  if ((state === 'p' || state === 's') && owner !== undefined && OWNER_PATTERN.test(owner)) {
    return { locale, chosenAt: at, owner, stored: state === 's' };
  }
  return null;
}

/** Whether two records are the same choice, in the same state. */
export function sameChoice(a: LocaleChoice | null, b: LocaleChoice | null): boolean {
  if (a === null || b === null) return a === b;
  return (
    a.locale === b.locale &&
    a.chosenAt === b.chosenAt &&
    a.owner === b.owner &&
    a.stored === b.stored
  );
}

/** The account's choice, or `null` when it holds none this build can show. */
export function accountChoice(
  stored: AccountLocaleChoice | null | undefined,
): Pick<LocaleChoice, 'locale' | 'chosenAt'> | null {
  if (!stored || !isLocale(stored.value)) return null;
  const at = Date.parse(stored.chosen_at);
  if (!Number.isFinite(at)) return null;
  return { locale: stored.value, chosenAt: at };
}

/**
 * The language a request renders in: the reader's choice, else the best match
 * for their browser's languages, else English.
 */
export function resolveRequestLocale({
  cookie,
  acceptLanguage,
}: {
  cookie: string | null | undefined;
  acceptLanguage: string | null | undefined;
}): Locale {
  return decodeLocaleChoice(cookie)?.locale ?? matchAcceptLanguage(acceptLanguage) ?? SOURCE_LOCALE;
}

/** What to do once the signed-in account's choice is known. */
export type LocaleSyncPlan =
  | { kind: 'none' }
  /** Write this browser choice — signed out, or the account's own — into the account. */
  | { kind: 'store'; choice: LocaleChoice }
  /** Make the account's choice the browser's. */
  | { kind: 'adopt'; locale: Locale; chosenAt: number };

/**
 * Reconcile the browser's choice with the signed-in account's.
 *
 * The most recent choice wins, within one limit: a choice belongs to the
 * account it was made in. A choice made signed out, or made in this account
 * and not yet stored, is written into the account when it is newer than the
 * account's. Anything else — this account's stored choice, or any choice of
 * another account, stored or not — is never written here; the account's own
 * choice is shown when it has one, and the browser's is left alone when it has
 * none.
 *
 * `account` is a copy read at some point, and may be older than a choice this
 * account has stored since. Such a copy never takes the browser back: only an
 * account choice at least as recent as this account's stored one is adopted.
 */
export function planLocaleSync(
  browser: LocaleChoice | null,
  account: Pick<LocaleChoice, 'locale' | 'chosenAt'> | null,
  userId: string,
): LocaleSyncPlan {
  const unstoredHere =
    browser !== null && !browser.stored && (browser.owner === null || browser.owner === userId);
  if (unstoredHere && (!account || browser.chosenAt > account.chosenAt)) {
    return { kind: 'store', choice: browser };
  }
  if (!account) return { kind: 'none' };
  if (browser && browser.owner === userId && browser.stored) {
    // The account stored this choice, so it holds this one or a newer one. A
    // copy of the account that is older than it was read before the store —
    // a profile this tab loaded earlier, or one another tab has since moved
    // past — and is not a reason to go back.
    if (browser.chosenAt > account.chosenAt) return { kind: 'none' };
    if (browser.chosenAt === account.chosenAt && browser.locale === account.locale) {
      return { kind: 'none' };
    }
  }
  return { kind: 'adopt', locale: account.locale, chosenAt: account.chosenAt };
}

/** The `Set-Cookie`-style string `document.cookie` takes for a choice. */
export function localeCookieString(choice: LocaleChoice, secure: boolean): string {
  return [
    `${LOCALE_COOKIE}=${encodeURIComponent(encodeLocaleChoice(choice))}`,
    'Path=/',
    `Max-Age=${LOCALE_COOKIE_MAX_AGE_SECONDS}`,
    'SameSite=Lax',
    ...(secure ? ['Secure'] : []),
  ].join('; ');
}

/** Read the choice from a `document.cookie` string. */
export function choiceFromCookieHeader(cookieHeader: string): LocaleChoice | null {
  for (const part of cookieHeader.split(';')) {
    const [name, ...value] = part.trim().split('=');
    if (name === LOCALE_COOKIE) return decodeLocaleChoice(value.join('='));
  }
  return null;
}
